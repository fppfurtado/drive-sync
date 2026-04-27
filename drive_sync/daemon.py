"""Daemon principal — entrypoint que executa indefinidamente.

Modelo:
- 1 thread do watchdog (thread separada por design da lib) posta nomes de
  tarefas em uma asyncio.Queue.
- N coroutines "worker" consomem a fila, com semáforo limitando a
  concorrência (configurável). Cada job é independente e não bloqueia
  os outros — atende ao requisito de pasta grande não atrapalhar pasta pequena.
- 1 coroutine "periodic" agenda full-syncs preventivos.
- O processo trata SIGTERM/SIGINT para shutdown limpo (importante para systemd).
"""
from __future__ import annotations

import asyncio
import logging
import signal
from pathlib import Path

from .config import AppConfig, FolderConfig
from .git_handler import GitHandler
from .sync_engine import RcloneEngine, remote_uri_for
from .watcher import FilesystemWatcher

log = logging.getLogger(__name__)


class SyncDaemon:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=cfg.watcher.queue_size)
        self.engine = RcloneEngine(cfg)
        self.git = GitHandler(cfg.git)
        self._semaphore = asyncio.Semaphore(cfg.watcher.max_concurrent_jobs)
        self._stop_event = asyncio.Event()
        self._inflight: set[str] = set()  # tarefas com job já rodando — evita duplicação
        self._inflight_lock = asyncio.Lock()
        # Resolvido em start():
        self._watcher: FilesystemWatcher | None = None

    # ------------------------------------------------------------------
    # Roteamento de uma tarefa: três modos possíveis.
    # ------------------------------------------------------------------
    def _is_bundle_flow(self, folder: FolderConfig) -> bool:
        """Só entra no fluxo de bundle se o usuário pediu explicitamente.

        Em `bisync` (padrão) e `off`, o worktree inteiro é sincronizado via
        rclone bisync — apenas com diferentes níveis de exclude automático.
        """
        return folder.git_mode == "bundle"

    async def _process_folder(self, folder: FolderConfig) -> None:
        """Processa uma tarefa de sincronização, fim a fim."""
        log.info("[%s] Iniciando job (modo=%s).", folder.name, folder.git_mode)

        if self._is_bundle_flow(folder):
            await self._sync_git_folder(folder)
        else:
            # Modos "bisync" e "off" caem aqui — diferença está só nos excludes.
            await self.engine.bisync_folder(folder)

    # ------------------------------------------------------------------
    # Fluxo para pastas Git: empacota e sobe somente o(s) bundle(s).
    # ------------------------------------------------------------------
    async def _sync_git_folder(self, folder: FolderConfig) -> None:
        """Para cada repo dentro da pasta:
        1. Verifica se o bundle remoto é mais novo (e baixa, se for).
        2. Restaura o repo a partir do bundle (se não existir local).
        3. Recria/atualiza o bundle a partir do worktree local.
        4. Sobe o bundle.
        """
        from .git_handler import (
            bundle_path_for,
            create_bundle,
            find_git_repos,
            is_git_repo,
            repo_last_modified,
            restore_from_bundle,
            should_replace_bundle,
        )

        root = folder.local_path
        # Quando a pasta-raiz é um repo, ela própria entra na lista; quando não,
        # find_git_repos devolve só os subprojetos. Combinamos ambos os casos.
        repos: list[Path] = []
        if self.cfg.git.recursive_detection:
            repos = find_git_repos(root, self.cfg.git.max_recursion_depth)
        elif is_git_repo(root):
            repos = [root]

        if not repos:
            log.info("[%s] Sem repos Git aqui — caindo no fluxo bisync comum.", folder.name)
            await self.engine.bisync_folder(folder)
            return

        for repo in repos:
            rel = repo.relative_to(root) if repo != root else Path(repo.name)
            local_bundle = bundle_path_for(
                repo, root, self.cfg.git.bundles_dir / folder.name, self.cfg.git.bundle_suffix
            )
            remote_rel = str(rel.with_suffix(rel.suffix + self.cfg.git.bundle_suffix))

            # 1. Baixa o bundle remoto, se for mais novo (rclone --update cuida disso).
            #    Salva num caminho temporário para comparar.
            tmp_remote = local_bundle.with_suffix(local_bundle.suffix + ".incoming")
            await self.engine.download_bundle_if_newer(folder, remote_rel, tmp_remote)

            # Decide qual lado é o mais atualizado.
            local_repo_mtime = repo_last_modified(repo)
            remote_bundle_mtime = tmp_remote.stat().st_mtime if tmp_remote.exists() else 0.0
            local_bundle_mtime = local_bundle.stat().st_mtime if local_bundle.exists() else 0.0

            log.debug(
                "[%s] %s — repo_mtime=%.0f remote_bundle=%.0f local_bundle=%.0f",
                folder.name, repo, local_repo_mtime, remote_bundle_mtime, local_bundle_mtime,
            )

            if remote_bundle_mtime > local_repo_mtime and remote_bundle_mtime > local_bundle_mtime:
                # Nuvem é mais nova → restaura a partir dela.
                log.info("[%s] Bundle remoto mais novo para %s — restaurando.", folder.name, rel)
                tmp_remote.replace(local_bundle)
                restore_from_bundle(local_bundle, repo)
            else:
                # Local é mais novo (ou empate) → regenera bundle e sobe.
                if tmp_remote.exists():
                    tmp_remote.unlink()
                if should_replace_bundle(repo, local_bundle):
                    if not create_bundle(repo, local_bundle, self.cfg.git.bundle_all):
                        continue
                await self.engine.upload_bundle(local_bundle, folder, remote_rel)

    # ------------------------------------------------------------------
    # Workers e scheduler
    # ------------------------------------------------------------------
    async def _worker(self, worker_id: int) -> None:
        log.debug("Worker #%d iniciado.", worker_id)
        while not self._stop_event.is_set():
            try:
                folder_name = await asyncio.wait_for(self.queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            folder = next((f for f in self.cfg.folders if f.name == folder_name), None)
            if folder is None or not folder.enabled:
                self.queue.task_done()
                continue

            # Evita duas execuções concorrentes da mesma tarefa.
            async with self._inflight_lock:
                if folder.name in self._inflight:
                    log.debug("[%s] Já em execução — descartando duplicata.", folder.name)
                    self.queue.task_done()
                    continue
                self._inflight.add(folder.name)

            try:
                async with self._semaphore:
                    await self._process_folder(folder)
            except Exception as exc:  # noqa: BLE001
                log.exception("[%s] Erro inesperado: %s", folder.name, exc)
            finally:
                async with self._inflight_lock:
                    self._inflight.discard(folder.name)
                self.queue.task_done()

    async def _periodic_full_sync(self) -> None:
        interval = self.cfg.watcher.periodic_full_sync_seconds
        if interval <= 0:
            return
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
                return  # stop solicitado
            except asyncio.TimeoutError:
                pass
            log.info("Sincronização periódica de rede de segurança disparada.")
            for f in self.cfg.folders:
                if f.enabled:
                    await self.queue.put(f.name)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def run(self) -> None:
        # Atraso inicial dá tempo da rede subir (NetworkManager pós-boot).
        if self.cfg.watcher.startup_delay_seconds > 0:
            log.info("Aguardando %ds antes de iniciar (rede).", self.cfg.watcher.startup_delay_seconds)
            await asyncio.sleep(self.cfg.watcher.startup_delay_seconds)

        loop = asyncio.get_running_loop()

        # Sinais — shutdown limpo.
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._stop_event.set)

        self._watcher = FilesystemWatcher(self.cfg, loop, self.queue)
        self._watcher.start()

        # Sync inicial de todas as pastas.
        for f in self.cfg.folders:
            if f.enabled:
                await self.queue.put(f.name)

        workers = [
            asyncio.create_task(self._worker(i))
            for i in range(self.cfg.watcher.max_concurrent_jobs)
        ]
        periodic = asyncio.create_task(self._periodic_full_sync())

        await self._stop_event.wait()
        log.info("Shutdown solicitado — parando watcher e workers.")

        if self._watcher:
            self._watcher.stop()
        for w in workers:
            w.cancel()
        periodic.cancel()
        await asyncio.gather(*workers, periodic, return_exceptions=True)
        log.info("Daemon finalizado.")
