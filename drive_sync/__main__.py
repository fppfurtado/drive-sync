"""Entrypoint executável: `python -m drive_sync` ou `drive-sync`."""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from .config import default_config_path, load_config
from .daemon import SyncDaemon
from .logging_setup import setup_logging
from .status import render_status
from .watcher import WatchLimitError


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="drive-sync",
        description="Sincronização bidirecional de pastas locais com Proton Drive.",
    )
    p.add_argument(
        "-c", "--config",
        type=Path,
        default=None,
        help=f"Caminho do config.yaml (padrão: {default_config_path()})",
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="Apenas valida o arquivo de configuração e sai.",
    )
    mode.add_argument(
        "--once",
        action="store_true",
        help="Roda uma única passada de sincronização e encerra (não fica como daemon).",
    )
    mode.add_argument(
        "--status",
        action="store_true",
        help="Imprime snapshot do estado das pastas (último sync, inicialização) e sai.",
    )
    return p


async def _run_once(daemon: SyncDaemon) -> None:
    """Executa _process_folder para cada pasta enabled, sequencialmente, e sai."""
    for f in daemon.cfg.folders:
        if f.enabled:
            await daemon._process_folder(f)  # noqa: SLF001


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        cfg = load_config(args.config)
    except (FileNotFoundError, ValueError) as exc:
        print(f"erro: {exc}", file=sys.stderr)
        return 2

    if args.status:
        print(render_status(cfg))
        return 0

    log = setup_logging(cfg.logging)
    log.info("drive-sync iniciando — config: %s", cfg.source_path)
    log.info("Tarefas habilitadas: %s",
             ", ".join(f.name for f in cfg.folders if f.enabled) or "(nenhuma)")

    if args.check:
        print("Configuração OK.")
        return 0

    daemon = SyncDaemon(cfg)

    try:
        if args.once:
            asyncio.run(_run_once(daemon))
        else:
            asyncio.run(daemon.run())
    except KeyboardInterrupt:
        log.info("Interrompido pelo usuário.")
    except WatchLimitError as exc:
        # Fatal deliberado (ADR-013): esgotamento inotify SEM periodic
        # full-sync habilitado — sai limpo, sem traceback no journal.
        log.critical("Encerrando: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
