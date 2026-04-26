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
import logging
import threading
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .config import AppConfig, FolderConfig

log = logging.getLogger(__name__)


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
        for folder in self._enabled_folders:
            if not folder.local_path.exists():
                log.warning(
                    "Pasta %s (%s) não existe; criando.",
                    folder.name,
                    folder.local_path,
                )
                folder.local_path.mkdir(parents=True, exist_ok=True)

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

    def stop(self) -> None:
        self.observer.stop()
        self.observer.join(timeout=5)
