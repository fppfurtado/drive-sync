"""Sinalização para o operador quando o daemon transita de/para estados notáveis.

`notify-send` roda via subprocess (binário base do Fedora). `sd_notify` escreve
direto no `NOTIFY_SOCKET` via socket Unix stdlib — sob `NotifyAccess=main`
(ADR-003), o sender precisa ser o MainPID do daemon; um subprocess child seria
rejeitado silenciosamente pelo systemd. Sem libs PyPI: o protocolo é literal
(`READY=1\\n`, `STATUS=<msg>\\n`). Cada canal é best-effort e isolado: falha em
um (ex.: sessão headless sem DISPLAY) não afeta os outros nem o daemon.
"""
from __future__ import annotations

import logging
import os
import socket
import subprocess

log = logging.getLogger(__name__)

_SOCKET_TIMEOUT_SECONDS = 1.0


class Notifier:
    def degraded(self, reason: str) -> None:
        """Dispara os três canais — log estruturado + sd_notify + notify-send."""
        log.critical("[AUTH_DEGRADED] %s", reason)
        self._systemd_notify(f"STATUS=degraded: {reason}")
        self._notify_send("drive-sync degraded", reason)

    def folder_degraded(self, folder: str, reason: str) -> None:
        """Sinaliza folder degradado (ADR-005). STATUS agregada é responsabilidade do daemon."""
        log.critical("[FOLDER_DEGRADED] %s: %s", folder, reason)
        self._notify_send(f"drive-sync — {folder} degraded", reason)

    def watcher_degraded(self, reason: str) -> None:
        """Sinaliza watcher desligado por esgotamento inotify — poll-only (#20).

        STATUS agregada é responsabilidade do daemon (mesmo padrão de ADR-005).
        """
        log.critical("[WATCHER_DEGRADED] %s", reason)
        self._notify_send("drive-sync — watcher degraded", reason)

    def watchdog_alert(self, summary: str) -> None:
        """Alerta do watchdog externo (#19/ADR-014) — re-emitido a cada ciclo com problema.

        Sem sd_notify: o watchdog é processo/unit próprio, não o MainPID do daemon
        (NotifyAccess=main o rejeitaria); o journal da unit oneshot é a superfície.
        """
        log.critical("[WATCHDOG_ALERT] %s", summary)
        self._notify_send("drive-sync watchdog", summary)

    def repo_mode_flip(self, folder: str, repo_subpath: str, old_mode: str, new_mode: str) -> None:
        """Sinaliza flip de mode em repo descoberto (ADR-008).

        Evento informativo (não muda STATUS=, não pausa workers) — operador
        percebe na hora que classificação mudou e pode auditar lixo no Proton.
        """
        target = repo_subpath or "<root>"
        body = f"{folder}/{target}: {old_mode}→{new_mode}"
        self._notify_send("drive-sync: repo mode flip", body)

    def send_status(self, payload: str) -> None:
        """Permite ao daemon emitir STATUS composta diretamente (ADR-005)."""
        self._systemd_notify(payload)

    def ready(self) -> None:
        """Emite READY=1 ao systemd — exigido sob Type=notify (ADR-003)."""
        self._systemd_notify("READY=1")

    def _systemd_notify(self, payload: str) -> None:
        addr = os.environ.get("NOTIFY_SOCKET")
        if not addr:
            return
        # Prefixo '@' em NOTIFY_SOCKET indica abstract namespace no Linux:
        # converter para NUL byte antes do sendto.
        if addr.startswith("@"):
            addr = "\0" + addr[1:]
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
                sock.settimeout(_SOCKET_TIMEOUT_SECONDS)
                sock.sendto(payload.encode("utf-8") + b"\n", addr)
        except OSError as exc:
            log.debug("sd_notify %r falhou: %s", payload, exc)

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
