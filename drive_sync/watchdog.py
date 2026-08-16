"""Watchdog dead-man's-switch externo ao daemon (#19 / ADR-014).

Roda como oneshot via `drive-sync-watchdog.timer` (30min). Cobre os dois
modos de silêncio que o daemon não cobre de dentro:

1. Daemon morto/parado (crash-loop #20) — processo morto não se auto-sinaliza.
2. Sinal perdido (#19) — notify-send de transição perdido uma vez = silêncio
   eterno; aqui o alerta é RE-EMITIDO a cada ciclo enquanto o problema durar.

Sinal, não ação: nenhuma auto-remediação (consistente com ADR-003 sem
auto-resume). Três superfícies: notify-send crítico + stdout→journal da unit
+ exit 1 (oneshot failed aparece em `systemctl --user --failed`).
"""
from __future__ import annotations

import subprocess
import time

from .config import AppConfig
from .notifier import Notifier
from .status import success_marker_for

_SERVICE = "drive-sync.service"


def _service_facts() -> dict[str, str]:
    """Lê ActiveState/StatusText/ActiveEnterTimestampMonotonic via systemctl show.

    Falha do systemctl → dict vazio (caller trata como problema: watchdog sem
    visibilidade do serviço é ele próprio um estado alarmável).
    """
    result = subprocess.run(
        [
            "systemctl", "--user", "show", _SERVICE,
            "-p", "ActiveState", "-p", "StatusText",
            "-p", "ActiveEnterTimestampMonotonic",
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return {}
    facts: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition("=")
        facts[key] = value
    return facts


def _service_uptime_seconds(facts: dict[str, str]) -> float | None:
    """Uptime via monotonic do systemd (mesmo CLOCK_MONOTONIC do time.monotonic)."""
    raw = facts.get("ActiveEnterTimestampMonotonic", "")
    if not raw.isdigit() or raw == "0":
        return None
    return time.monotonic() - int(raw) / 1_000_000


def collect_problems(cfg: AppConfig, facts: dict[str, str] | None = None) -> list[str]:
    """Avalia os 3 checks do plano e retorna as descrições dos problemas."""
    problems: list[str] = []
    if facts is None:
        facts = _service_facts()

    if not facts:
        problems.append("systemctl --user show falhou — sem visibilidade do serviço")
        return problems

    active_state = facts.get("ActiveState", "unknown")
    if active_state != "active":
        problems.append(f"serviço {active_state}")
        # Serviço fora do ar domina: frescor de marker seria ruído derivado.
        return problems

    status_text = facts.get("StatusText", "")
    if status_text.startswith("degraded"):
        problems.append(f"STATUS {status_text}")

    threshold = cfg.watcher.folder_staleness_threshold_seconds
    if threshold <= 0:
        return problems  # opt-out herdado do ADR-005

    uptime = _service_uptime_seconds(facts)
    now = time.time()
    for f in cfg.folders:
        if not f.enabled:
            continue
        marker = success_marker_for(f.fs_key)
        if not marker.exists():
            # Só alarma se o daemon já rodou tempo suficiente para ter sincronizado
            # — mata falso-positivo de instalação/folder novos.
            if uptime is not None and uptime > threshold:
                problems.append(f"{f.name}: nunca sincronizou (daemon up há {uptime / 3600:.1f}h)")
            continue
        age = now - marker.stat().st_mtime
        if age > threshold:
            problems.append(f"{f.name}: sem sucesso há {age / 3600:.1f}h")

    return problems


def run_watchdog(cfg: AppConfig) -> int:
    """Executa os checks; alerta e retorna exit code (0 saudável, 1 problemas)."""
    problems = collect_problems(cfg)
    if not problems:
        print("watchdog: ok")
        return 0
    summary = "; ".join(problems)
    # Re-emissão a cada ciclo é deliberada (persistência do sinal, #19).
    print(f"watchdog: PROBLEMAS — {summary}")
    Notifier().watchdog_alert(summary)
    return 1
