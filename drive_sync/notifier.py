"""Sinalização para o operador quando o daemon transita de/para estados notáveis.

Roda via subprocess (`systemd-notify`, `notify-send`) em vez de adicionar libs PyPI —
ambos os binários estão na base do Fedora. Cada canal é best-effort e isolado:
falha em um (ex.: sessão headless sem DISPLAY) não afeta os outros nem o daemon.
"""
from __future__ import annotations

import logging
import os
import subprocess

log = logging.getLogger(__name__)


class Notifier:
    def degraded(self, reason: str) -> None:
        """Dispara os três canais — log estruturado + sd_notify + notify-send."""
        log.critical("[AUTH_DEGRADED] %s", reason)
        self._systemd_notify([f"--status=degraded: {reason}"])
        self._notify_send("drive-sync degraded", reason)

    def ready(self) -> None:
        """Emite READY=1 ao systemd — exigido sob Type=notify (ADR-003)."""
        self._systemd_notify(["--ready"])

    def _systemd_notify(self, args: list[str]) -> None:
        if not os.environ.get("NOTIFY_SOCKET"):
            return
        try:
            subprocess.run(["systemd-notify", *args], check=False, timeout=5)
        except (OSError, subprocess.SubprocessError) as exc:
            log.debug("systemd-notify %s falhou: %s", args, exc)

    def _notify_send(self, summary: str, body: str) -> None:
        if not os.environ.get("DISPLAY") and not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
            return
        try:
            subprocess.run(
                ["notify-send", "--urgency=critical", summary, body],
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.debug("notify-send falhou: %s", exc)
