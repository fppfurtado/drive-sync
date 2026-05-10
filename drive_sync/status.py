"""Snapshot observacional do estado do daemon (CLI --status).

Lê markers do bisync (`~/.cache/rclone/bisync/`) e config para mostrar
pastas configuradas, inicialização e última sincronização. Não cobre
fila/inflight/erros — exigiriam log parsing ou IPC (fora do escopo v1).
"""
from __future__ import annotations

import hashlib
import os
from datetime import datetime
from pathlib import Path

from .config import AppConfig
from .sync_engine import remote_uri_for


HEADER = (
    "# drive-sync status v1 — formato textual não-estável, "
    "use --json quando disponível"
)


def _default_bisync_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return Path(base) / "rclone" / "bisync"


def _marker_name(local: Path, remote: str) -> str:
    # Duplicado de sync_engine._state_marker_for (private). Extrair para
    # módulo compartilhado só quando aparecer um 3º caller.
    key = hashlib.sha1(f"{local}|{remote}".encode()).hexdigest()[:16]
    return f"drive-sync.{key}.initialized"


def _sanitize_for_lst(s: str) -> str:
    # rclone bisync substitui '/', ':' e ' ' por '_' nos nomes dos .lst.
    # Encoding exato pode mudar entre versões — matching é por prefixo.
    return s.replace("/", "_").replace(":", "_").replace(" ", "_").lstrip("_")


def _last_sync_mtime(local: Path, remote: str, bisync_dir: Path) -> float | None:
    if not bisync_dir.exists():
        return None
    # Casamos só `.lst` (não `.lst-new`/`.lst-err`): `.lst` é o listing final
    # persistido após bisync OK; `.lst-new` é transitório durante run em curso.
    prefix = f"{_sanitize_for_lst(str(local))}..{_sanitize_for_lst(remote)}.path"
    mtimes = [
        f.stat().st_mtime
        for f in bisync_dir.iterdir()
        if f.name.startswith(prefix) and f.name.endswith(".lst")
    ]
    return max(mtimes) if mtimes else None


def _format_mtime(mtime: float | None) -> str:
    if mtime is None:
        return "never"
    return datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")


def _contract_home(p: Path) -> str:
    home = Path(os.path.expanduser("~"))
    try:
        return f"~/{p.relative_to(home)}"
    except ValueError:
        return str(p)


def render_status(cfg: AppConfig, bisync_dir: Path | None = None) -> str:
    bisync_dir = bisync_dir or _default_bisync_dir()
    rows: list[tuple[str, str, str, str]] = []
    for folder in cfg.folders:
        if not folder.enabled:
            continue
        remote = remote_uri_for(folder, cfg)
        marker = bisync_dir / _marker_name(folder.local_path, remote)
        initialized = "yes" if marker.exists() else "no"
        last = _format_mtime(_last_sync_mtime(folder.local_path, remote, bisync_dir))
        rows.append((_contract_home(folder.local_path), initialized, last, remote))

    headers = ("Folder", "Initialized", "Last sync", "Remote")
    widths = [
        max(len(h), max((len(r[i]) for r in rows), default=0))
        for i, h in enumerate(headers)
    ]
    lines = [HEADER, ""]
    lines.append("  ".join(h.ljust(w) for h, w in zip(headers, widths)))
    lines.append("  ".join("-" * w for w in widths))
    for row in rows:
        lines.append("  ".join(c.ljust(w) for c, w in zip(row, widths)))
    return "\n".join(lines)
