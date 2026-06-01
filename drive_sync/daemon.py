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
import time
from pathlib import Path

from .config import AppConfig, FolderConfig
from .git_handler import GitHandler
from .notifier import Notifier
from .sync_engine import AuthDegradedError, RcloneEngine, remote_uri_for
from .watcher import FilesystemWatcher

log = logging.getLogger(__name__)


class SyncDaemon:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=cfg.watcher.queue_size)
        self.engine = RcloneEngine(cfg)
        self.git = GitHandler(cfg.git)
        self._notifier = Notifier()
        self._semaphore = asyncio.Semaphore(cfg.watcher.max_concurrent_jobs)
        self._stop_event = asyncio.Event()
        self._inflight: set[str] = set()  # tarefas com job já rodando — evita duplicação
        self._inflight_lock = asyncio.Lock()
        # Pause-on-failure de auth: workers drenam fila enquanto setado.
        self._degraded = asyncio.Event()
        self._degraded_reason: str | None = None
        # Cooldown por folder (ADR-004): janela conta from-start; tasks diferidas
        # re-enfileiram no fim da janela. Sem persistência cross-restart.
        self._last_sync_at: dict[str, float] = {}
        self._cooldown_scheduled: set[str] = set()
        self._cooldown_tasks: set[asyncio.Task[None]] = set()
        # Staleness per-folder (ADR-005 + ADR-007): dual-clock.
        # Wall-clock alimenta o reason ("sem sucesso há X.Xh"); monotonic gate
        # do _check_folder_staleness (suspend-aware — relógio congela com o
        # processo, evita falso-positivo de degraded após suspend > threshold).
        self._last_successful_sync_at: dict[str, float] = {}
        self._last_successful_sync_at_mono: dict[str, float] = {}
        self._degraded_folders: dict[str, str] = {}
        self._daemon_start_time: float = time.time()
        self._daemon_start_monotonic: float = time.monotonic()
        # Classify_repos state per folder (ADR-008): {folder_name: {repo_subpath: mode}}.
        # Vazio no primeiro ciclo pós-restart → flip detection silente.
        self._last_classification: dict[str, dict[str, str]] = {}
        # Resolvido em start():
        self._watcher: FilesystemWatcher | None = None

    def _enter_degraded(self, reason: str) -> None:
        """Transiciona para estado degraded — idempotente.

        Atômico entre tasks por ser síncrono (sem await entre is_set e set).
        Inserir await aqui exige asyncio.Lock para preservar o invariante.
        """
        if self._degraded.is_set():
            return
        self._degraded_reason = reason
        self._degraded.set()
        self._notifier.degraded(reason)

    # ------------------------------------------------------------------
    # Roteamento de uma tarefa: três modos possíveis.
    # ------------------------------------------------------------------
    def _is_bundle_flow(self, folder: FolderConfig) -> bool:
        """Bundle flow folder-level (ADR-008). Modo `auto` despacha per-repo em _process_auto."""
        return folder.git_handling == "bundle"

    def _mark_success(self, folder: FolderConfig) -> None:
        """Atualiza relógios duais + limpa degraded — invariante ADR-005/ADR-007."""
        self._last_successful_sync_at[folder.name] = time.time()
        self._last_successful_sync_at_mono[folder.name] = time.monotonic()
        was_degraded = self._degraded_folders.pop(folder.name, None) is not None
        if was_degraded:
            self._notifier.send_status(self._compose_status_payload())

    async def _process_folder(self, folder: FolderConfig) -> bool:
        """Processa uma tarefa de sincronização, fim a fim. Retorna True em sucesso."""
        if folder.git_handling == "skip":
            log.info("[%s] [FOLDER_SKIP] git_handling=skip — ciclo pulado.", folder.name)
            # Marca como sucesso para não disparar staleness (ADR-005) — folder
            # skipped intencionalmente não é "sem sucesso há Xh".
            self._mark_success(folder)
            return True

        log.info("[%s] Iniciando job (modo=%s).", folder.name, folder.git_handling)

        if folder.git_handling == "auto":
            success = await self._process_auto(folder)
        elif folder.git_handling == "bundle":
            await self._sync_git_folder(folder)
            success = True
        else:  # "plain"
            success = await self.engine.bisync_folder(folder)

        if success:
            self._mark_success(folder)
        return success

    async def _process_auto(self, folder: FolderConfig) -> bool:
        """Dispatch auto-detect (ADR-008): classify + bundle per-repo + bisync do restante.

        Sucesso agregado por AND: falha em qualquer bundle individual OU no bisync
        do conteúdo restante derruba o ciclo do folder, permitindo que staleness
        (ADR-005) acione após threshold. Evita o cenário "repo local-only sem
        backup" cego — diferente do modo `bundle` legado em _sync_git_folder que
        engole falhas individuais (não regredido por simetria; comportamento
        legado preexistente).

        - repos com remote → skip (vira --exclude no bisync)
        - repos sem remote → bundle (sincronizado via _bundle_single_repo)
        - repo_overrides ganham precedência total sobre auto-detect
        - quando `folder.local_path` é ele próprio um repo (subpath=""), pula bisync
          do restante — o folder inteiro é o repo, já tratado individualmente
        """
        from .git_handler import classify_repos, detect_repo_mode_flips

        classifications = classify_repos(folder, self.cfg.git)

        # Flip detection (per ADR-008 §Mitigações): primeiro ciclo pós-restart silente.
        prev = self._last_classification.get(folder.name, {})
        for repo_subpath, old_mode, new_mode in detect_repo_mode_flips(
            folder.name, prev, classifications
        ):
            self._notifier.repo_mode_flip(folder.name, repo_subpath, old_mode, new_mode)
        self._last_classification[folder.name] = {
            c.repo_subpath: c.mode for c in classifications
        }

        # Bundle cada repo classificado como bundle.
        all_success = True
        for c in classifications:
            if c.mode == "bundle":
                if not await self._bundle_single_repo(folder, c.repo_path):
                    all_success = False

        # Quando o folder inteiro é um repo (root classificado), pular bisync do restante.
        if any(c.repo_subpath == "" for c in classifications):
            return all_success

        # Bisync conteúdo não-repo, excluindo todos os repos classificados.
        extra_excludes = [f"/{c.repo_subpath}/**" for c in classifications if c.repo_subpath]
        bisync_ok = await self.engine.bisync_folder(folder, extra_excludes=extra_excludes)
        return all_success and bisync_ok

    # ------------------------------------------------------------------
    # Fluxo para pastas Git: empacota e sobe somente o(s) bundle(s).
    # ------------------------------------------------------------------
    async def _sync_git_folder(self, folder: FolderConfig) -> None:
        """Itera repos sob folder.local_path e bundla cada um (modo `bundle`).

        Modo `auto` usa _bundle_single_repo direto a partir da classificação.
        """
        from .git_handler import find_git_repos, is_git_repo

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
            await self._bundle_single_repo(folder, repo)

    async def _bundle_single_repo(self, folder: FolderConfig, repo: Path) -> bool:
        """Bundle de um repo específico (compartilhado entre `bundle` e `auto`).

        Retorna True em sucesso. Modo `auto` agrega via AND para alimentar
        staleness ADR-005; modo `bundle` legado ignora o retorno (comportamento
        preexistente em _sync_git_folder).
        """
        from .git_handler import (
            bundle_path_for,
            create_bundle,
            repo_last_modified,
            restore_from_bundle,
            should_replace_bundle,
        )

        root = folder.local_path
        rel = repo.relative_to(root) if repo != root else Path(repo.name)
        local_bundle = bundle_path_for(
            repo, root, self.cfg.git.bundles_dir / folder.fs_key, self.cfg.git.bundle_suffix
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
            return restore_from_bundle(local_bundle, repo)
        # Local é mais novo (ou empate) → regenera bundle e sobe.
        if tmp_remote.exists():
            tmp_remote.unlink()
        if should_replace_bundle(repo, local_bundle):
            if not create_bundle(repo, local_bundle, self.cfg.git.bundle_all):
                return False
        return await self.engine.upload_bundle(local_bundle, folder, remote_rel)

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

            if self._degraded.is_set():
                self.queue.task_done()
                continue

            folder = next((f for f in self.cfg.folders if f.name == folder_name), None)
            if folder is None or not folder.enabled:
                self.queue.task_done()
                continue

            if self._is_in_cooldown(folder):
                self._maybe_schedule_deferred(folder)
                self.queue.task_done()
                continue

            # Evita duas execuções concorrentes da mesma tarefa.
            async with self._inflight_lock:
                if folder.name in self._inflight:
                    log.debug("[%s] Já em execução — descartando duplicata.", folder.name)
                    self.queue.task_done()
                    continue
                self._inflight.add(folder.name)

            # Janela conta from-start (ADR-004).
            if folder.cooldown_seconds > 0:
                self._last_sync_at[folder.name] = asyncio.get_running_loop().time()

            try:
                async with self._semaphore:
                    await self._process_folder(folder)
            except AuthDegradedError as exc:
                self._enter_degraded(f"{exc.kind} (Code={exc.code}) — tail: {exc.stderr_tail}")
            except Exception as exc:  # noqa: BLE001
                log.exception("[%s] Erro inesperado: %s", folder.name, exc)
            finally:
                async with self._inflight_lock:
                    self._inflight.discard(folder.name)
                self.queue.task_done()

    def _is_in_cooldown(self, folder: FolderConfig) -> bool:
        if folder.cooldown_seconds <= 0:
            return False
        last = self._last_sync_at.get(folder.name)
        if last is None:
            return False
        return (asyncio.get_running_loop().time() - last) < folder.cooldown_seconds

    def _maybe_schedule_deferred(self, folder: FolderConfig) -> None:
        if folder.name in self._cooldown_scheduled:
            log.debug("[%s] Cooldown já agendado — descartando evento.", folder.name)
            return
        last = self._last_sync_at[folder.name]
        delay = (last + folder.cooldown_seconds) - asyncio.get_running_loop().time()
        log.info("[%s] Cooldown ativo — sync diferida em %.0fs.", folder.name, delay)
        self._cooldown_scheduled.add(folder.name)
        task = asyncio.create_task(self._schedule_deferred_enqueue(folder.name, delay))
        self._cooldown_tasks.add(task)
        task.add_done_callback(self._cooldown_tasks.discard)

    async def _schedule_deferred_enqueue(self, folder_name: str, delay: float) -> None:
        await asyncio.sleep(delay)
        self._cooldown_scheduled.discard(folder_name)
        await self.queue.put(folder_name)

    def _compose_status_payload(self) -> str:
        """Compõe STATUS sd_notify com precedência auth > folder (ADR-005)."""
        if self._degraded.is_set():
            return f"STATUS=degraded: {self._degraded_reason}"
        if self._degraded_folders:
            ordered = ", ".join(
                f"{name} ({self._degraded_folders[name]})"
                for name in sorted(self._degraded_folders)
            )
            return f"STATUS=degraded folders: {ordered}"
        return "STATUS="

    def _check_folder_staleness(self) -> None:
        """Marca como degraded pastas sem sucesso há > threshold (ADR-005 + ADR-007).

        Gate: monotonic (suspend congela com o processo); reason: min(wall, mono)
        — wall dá "horas reais", monotonic caps gap de cadência do periodic.
        """
        threshold = self.cfg.watcher.folder_staleness_threshold_seconds
        if threshold <= 0:
            return
        now_wall = time.time()
        now_mono = time.monotonic()
        changed = False
        for f in self.cfg.folders:
            if not f.enabled or f.name in self._degraded_folders:
                continue
            last_mono = self._last_successful_sync_at_mono.get(
                f.name, self._daemon_start_monotonic
            )
            elapsed_mono = now_mono - last_mono
            if elapsed_mono > threshold:
                last_wall = self._last_successful_sync_at.get(f.name, self._daemon_start_time)
                elapsed_wall = now_wall - last_wall
                reason = f"sem sucesso há {min(elapsed_wall, elapsed_mono) / 3600:.1f}h"
                self._degraded_folders[f.name] = reason
                self._notifier.folder_degraded(f.name, reason)
                changed = True
        if changed:
            self._notifier.send_status(self._compose_status_payload())

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
            if self._degraded.is_set():
                continue
            self._check_folder_staleness()
            log.info("Sincronização periódica de rede de segurança disparada.")
            for f in self.cfg.folders:
                if f.enabled:
                    await self.queue.put(f.name)

    async def _auth_probe_loop(self) -> None:
        if not self.cfg.health_check.enabled:
            return
        interval = self.cfg.health_check.interval_seconds
        if interval <= 0:
            return
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
                return  # stop solicitado
            except asyncio.TimeoutError:
                pass
            # Skip enquanto degraded: backend sabidamente quebrado não traz
            # informação nova e gasta rate-limit (pode aprofundar CAPTCHA gate).
            if self._degraded.is_set():
                continue
            try:
                await self.engine.auth_probe()
            except AuthDegradedError as exc:
                self._enter_degraded(f"{exc.kind} (Code={exc.code}) — tail: {exc.stderr_tail}")

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
        probe = asyncio.create_task(self._auth_probe_loop())

        # Última linha antes do bloqueio: mover invalida o gatilho de revisão da ADR-003.
        self._notifier.ready()

        await self._stop_event.wait()
        log.info("Shutdown solicitado — parando watcher e workers.")

        if self._watcher:
            self._watcher.stop()
        for w in workers:
            w.cancel()
        periodic.cancel()
        probe.cancel()
        for task in list(self._cooldown_tasks):
            task.cancel()
        await asyncio.gather(
            *workers, periodic, probe, *self._cooldown_tasks, return_exceptions=True
        )
        log.info("Daemon finalizado.")
