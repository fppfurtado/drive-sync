"""Setup de logging com RotatingFileHandler + compressão automática.

A rotação por tamanho garante que o arquivo nunca cresça indefinidamente.
Quando um backup é fechado, ele é comprimido com gzip para economizar espaço.
"""
from __future__ import annotations

import gzip
import logging
import shutil
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import LoggingConfig


class CompressingRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler que comprime o arquivo rotacionado para .gz."""

    def doRollover(self) -> None:  # noqa: N802 (assinatura herdada)
        super().doRollover()
        # Após a rotação, o arquivo de backup mais novo é "<file>.1".
        rotated = Path(f"{self.baseFilename}.1")
        if rotated.exists():
            gz_path = rotated.with_suffix(rotated.suffix + ".gz")
            try:
                with rotated.open("rb") as src, gzip.open(gz_path, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                rotated.unlink()
            except OSError:
                # Falha ao comprimir não deve derrubar a aplicação;
                # mantém o arquivo descomprimido.
                pass


def setup_logging(cfg: LoggingConfig) -> logging.Logger:
    """Configura o logger raiz da aplicação e devolve-o."""
    cfg.file.parent.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = CompressingRotatingFileHandler(
        filename=str(cfg.file),
        maxBytes=cfg.max_bytes,
        backupCount=cfg.backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)

    handlers: list[logging.Handler] = [file_handler]
    if cfg.console:
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        handlers.append(console)

    root = logging.getLogger("drive_sync")
    root.setLevel(cfg.level)
    # Limpa handlers anteriores (importante em recargas/teste).
    root.handlers.clear()
    for h in handlers:
        root.addHandler(h)
    root.propagate = False
    return root
