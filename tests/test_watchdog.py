"""Tests do watchdog dead-man's-switch externo (#19 / ADR-014).

collect_problems recebe `facts` injetados (o parse do systemctl é testado à
parte) e lê success markers reais sob XDG_CACHE_HOME isolado (conftest).
"""
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from drive_sync.config import (
    AppConfig,
    DedupeConfig,
    FolderConfig,
    GitConfig,
    HealthCheckConfig,
    LoggingConfig,
    RcloneConfig,
    WatcherConfig,
)
from drive_sync.status import success_marker_for
from drive_sync.watchdog import collect_problems, run_watchdog


def _make_config(
    folders: list[FolderConfig] | None = None, staleness: int = 43200
) -> AppConfig:
    return AppConfig(
        rclone=RcloneConfig(),
        folders=folders or [],
        git=GitConfig(),
        watcher=WatcherConfig(folder_staleness_threshold_seconds=staleness),
        dedupe=DedupeConfig(),
        health_check=HealthCheckConfig(),
        logging=LoggingConfig(),
        source_path=Path("/fake/config.yaml"),
    )


def _folder(name: str = "t") -> FolderConfig:
    return FolderConfig(name=name, local_path=Path(f"/tmp/{name}"), remote_subpath=name)


def _facts(state: str = "active", status: str = "", uptime_hours: float = 24.0) -> dict:
    mono_usec = int((time.monotonic() - uptime_hours * 3600) * 1_000_000)
    return {
        "ActiveState": state,
        "StatusText": status,
        "ActiveEnterTimestampMonotonic": str(max(mono_usec, 1)),
    }


def _touch_marker(fs_key: str, age_seconds: float = 0.0) -> Path:
    marker = success_marker_for(fs_key)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()
    if age_seconds:
        past = time.time() - age_seconds
        os.utime(marker, (past, past))
    return marker


# ---------------------------------------------------------------------------
# collect_problems
# ---------------------------------------------------------------------------

def test_healthy_service_fresh_marker_is_clean():
    f = _folder()
    _touch_marker(f.fs_key)
    problems = collect_problems(_make_config([f]), facts=_facts())
    assert problems == []


def test_service_not_active_dominates():
    f = _folder()
    _touch_marker(f.fs_key, age_seconds=999_999)
    problems = collect_problems(_make_config([f]), facts=_facts(state="failed"))
    assert problems == ["serviço failed"]  # marker vira ruído derivado — suprimido


def test_degraded_status_is_reported():
    f = _folder()
    _touch_marker(f.fs_key)
    problems = collect_problems(
        _make_config([f]), facts=_facts(status="degraded: invalid_credentials")
    )
    assert problems == ["STATUS degraded: invalid_credentials"]


def test_stale_marker_is_reported():
    f = _folder()
    _touch_marker(f.fs_key, age_seconds=13 * 3600)
    problems = collect_problems(_make_config([f], staleness=43200), facts=_facts())
    assert len(problems) == 1
    assert "sem sucesso há 13.0h" in problems[0]


def test_missing_marker_alarms_only_after_uptime_exceeds_threshold():
    f = _folder("nunca")
    cfg = _make_config([f], staleness=43200)
    # Daemon up há 1h < threshold → instalação nova, sem alarme.
    assert collect_problems(cfg, facts=_facts(uptime_hours=1)) == []
    # Daemon up há 24h > threshold → nunca sincronizou é real.
    problems = collect_problems(cfg, facts=_facts(uptime_hours=24))
    assert len(problems) == 1
    assert "nunca sincronizou" in problems[0]


def test_staleness_zero_opts_out_marker_checks():
    f = _folder("optout")
    problems = collect_problems(
        _make_config([f], staleness=0), facts=_facts(uptime_hours=48)
    )
    assert problems == []


def test_disabled_folder_is_ignored():
    f = FolderConfig(
        name="off", local_path=Path("/tmp/off"), remote_subpath="off", enabled=False
    )
    problems = collect_problems(
        _make_config([f]), facts=_facts(uptime_hours=48)
    )
    assert problems == []


def test_no_facts_is_itself_a_problem():
    problems = collect_problems(_make_config(), facts={})
    assert problems and "sem visibilidade" in problems[0]


# ---------------------------------------------------------------------------
# run_watchdog — exit codes + re-emissão
# ---------------------------------------------------------------------------

def test_run_watchdog_clean_returns_zero():
    f = _folder()
    _touch_marker(f.fs_key)
    with patch("drive_sync.watchdog._service_facts", return_value=_facts()), \
         patch("drive_sync.watchdog.Notifier") as notifier_cls:
        rc = run_watchdog(_make_config([f]))
    assert rc == 0
    notifier_cls.return_value.watchdog_alert.assert_not_called()


def test_run_watchdog_problems_alert_and_return_one():
    with patch(
        "drive_sync.watchdog._service_facts", return_value=_facts(state="inactive")
    ), patch("drive_sync.watchdog.Notifier") as notifier_cls:
        rc = run_watchdog(_make_config())
    assert rc == 1
    notifier_cls.return_value.watchdog_alert.assert_called_once()
    assert "serviço inactive" in notifier_cls.return_value.watchdog_alert.call_args.args[0]
