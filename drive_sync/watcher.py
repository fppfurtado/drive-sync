"""Observa o filesystem e enfileira jobs de sincronização.

Características importantes:

1. Debounce: alterações em rajada (ex.: salvar um arquivo grande gera
   centenas de eventos) viram um único job, disparado X segundos após
   o último evento.

2. Deduplicação de subpastas configuradas: se /home/user/A e /home/user/A/B
   estão ambas no config, alterações dentro de B não disparam o job de A
   *além* do job de B — caso contrário, B seria sincronizado duas vezes
   (como sub de A e como tarefa própria).

3. Os jobs são posts em uma asyncio.Queue; o "scheduler" do daemon
   consome essa fila com N workers em paralelo.
"""
from __future__ import annotations

import asyncio
import errno
import logging
import threading
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .config import AppConfig, FolderConfig

log = logging.getLogger(__name__)


class WatchLimitError(RuntimeError):
    """Esgotamento de recursos inotify (ENOSPC watches / EMFILE instances).

    Condição de ambiente, não bug: o watcher não pode subir, mas o daemon
    pode continuar em poll-only via periodic full-sync (#20).
    """


_WATCH_LIMIT_ERRNOS = frozenset({errno.ENOSPC, errno.EMFILE})


# ---------------------------------------------------------------------------
# Resolução de qual tarefa "dona" cobre um caminho — usada para dedupe
# ---------------------------------------------------------------------------
def owning_folder(path: Path, folders: list[FolderConfig]) -> FolderConfig | None:
    """Retorna a tarefa cujo local_path é o ancestral *mais específico* de path.

    Ex.: folders = [A=~/X, B=~/X/Y]
         path=~/X/Y/file.txt → devolve B (mais específico).
         path=~/X/file.txt   → devolve A.
    """
    best: FolderConfig | None = None
    best_len = -1
    for f in folders:
        try:
            path.relative_to(f.local_path)
        except ValueError:
            continue
        n = len(f.local_path.parts)
        if n > best_len:
            best_len = n
            best = f
    return best


# ---------------------------------------------------------------------------
# Handler do watchdog que apenas posta na fila (com debounce por tarefa)
# ---------------------------------------------------------------------------
class _DebouncingHandler(FileSystemEventHandler):
    def __init__(
        self,
        folder: FolderConfig,
        all_folders: list[FolderConfig],
        loop: asyncio.AbstractEventLoop,
        queue: asyncio.Queue[str],
        dedupe_subpaths: bool,
    ):
        super().__init__()
        self.folder = folder
        self.all_folders = all_folders
        self.loop = loop
        self.queue = queue
        self.dedupe = dedupe_subpaths
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            # Eventos de diretório também contam — criação de pasta vazia
            # é um caso real.
            pass
        try:
            evt_path = Path(event.src_path).resolve()
        except (OSError, RuntimeError):
            return

        # --- dedupe: se outra tarefa "mais específica" cobre este path,
        # esta tarefa NÃO deve reagir.
        if self.dedupe:
            owner = owning_folder(evt_path, self.all_folders)
            if owner is not None and owner.name != self.folder.name:
                # Ignora — quem reage é a tarefa "dona".
                return

        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self.folder.debounce_seconds, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self) -> None:
        log.debug("Debounce expirou para tarefa %r — enfileirando.", self.folder.name)
        # Posta na asyncio.Queue de forma thread-safe.
        asyncio.run_coroutine_threadsafe(self.queue.put(self.folder.name), self.loop)


# ---------------------------------------------------------------------------
# Watcher de alto nível (gerencia 1 Observer com vários handlers)
# ---------------------------------------------------------------------------
class FilesystemWatcher:
    def __init__(
        self,
        cfg: AppConfig,
        loop: asyncio.AbstractEventLoop,
        queue: asyncio.Queue[str],
    ):
        self.cfg = cfg
        self.loop = loop
        self.queue = queue
        self.observer = Observer()
        self._enabled_folders = [f for f in cfg.folders if f.enabled]

    def start(self) -> None:
        """Agenda watches recursivos e sobe o Observer.

        Pré-condição: os local_paths existem — materialização é setup do
        daemon (`_ensure_local_paths`), compartilhado com o modo poll-only.
        O try cobre APENAS a superfície inotify (schedule/start): um ENOSPC
        de outra origem (ex.: disco cheio) não pode ser classificado como
        esgotamento de watches.

        Raises:
            WatchLimitError: esgotamento inotify (ENOSPC/EMFILE) no setup —
                watches parciais já criados são liberados (best-effort)
                antes do raise (sem watcher parcial: daria falsa sensação
                de sync em tempo real por folder).
        """
        try:
            for folder in self._enabled_folders:
                handler = _DebouncingHandler(
                    folder=folder,
                    all_folders=self._enabled_folders,
                    loop=self.loop,
                    queue=self.queue,
                    dedupe_subpaths=self.cfg.dedupe.skip_subpaths_of_configured_folders,
                )
                self.observer.schedule(handler, str(folder.local_path), recursive=True)
                log.info(
                    "Observando %s (debounce=%ds)",
                    folder.local_path,
                    folder.debounce_seconds,
                )

            self.observer.start()
        except OSError as exc:
            if exc.errno not in _WATCH_LIMIT_ERRNOS:
                raise
            self._teardown_partial()
            raise WatchLimitError(
                f"inotify esgotado ({errno.errorcode.get(exc.errno, exc.errno)}: {exc})"
            ) from exc

    def _teardown_partial(self) -> None:
        """Libera watches/emitters criados antes do esgotamento — best-effort.

        Passo a passo: falha num passo não pula os seguintes — pular o stop()
        deixaria emitters vivos enfileirando eventos com o daemon em poll-only.
        WARNING (não DEBUG): teardown incompleto significa watches retidos que
        o operador precisa saber que só um restart libera.
        """
        try:
            self.observer.unschedule_all()
        except Exception as exc:  # noqa: BLE001
            log.warning("Teardown parcial: unschedule_all falhou: %s", exc)
        try:
            self.observer.stop()
            if self.observer.is_alive():
                self.observer.join(timeout=5)
        except Exception as exc:  # noqa: BLE001
            log.warning("Teardown parcial: stop do observer falhou: %s", exc)

    def stop(self) -> None:
        self.observer.stop()
        self.observer.join(timeout=5)
