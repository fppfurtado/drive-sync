"""Tests for SyncDaemon folder-staleness degraded signaling (ADR-005)."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from drive_sync.config import (
    AppConfig,
    CoverageAuditConfig,
    DedupeConfig,
    FolderConfig,
    GitConfig,
    HealthCheckConfig,
    LoggingConfig,
    RcloneConfig,
    WatcherConfig,
)
from drive_sync.daemon import SyncDaemon


def _make_config(
    folders: list[FolderConfig] | None = None,
    threshold: int = 43200,
) -> AppConfig:
    return AppConfig(
        rclone=RcloneConfig(),
        folders=folders or [],
        git=GitConfig(),
        watcher=WatcherConfig(
            max_concurrent_jobs=2,
            queue_size=20,
            folder_staleness_threshold_seconds=threshold,
        ),
        dedupe=DedupeConfig(),
        health_check=HealthCheckConfig(),
        logging=LoggingConfig(),
        coverage_audit=CoverageAuditConfig(),
        source_path=Path("/fake/config.yaml"),
    )


def _folder(name: str = "test", enabled: bool = True) -> FolderConfig:
    return FolderConfig(
        name=name,
        local_path=Path(f"/tmp/{name}"),
        remote_subpath=name,
        enabled=enabled,
    )


def _build_daemon(folders: list[FolderConfig], threshold: int = 43200) -> SyncDaemon:
    daemon = SyncDaemon(_make_config(folders, threshold=threshold))
    daemon._notifier = MagicMock()
    return daemon


# ---------------------------------------------------------------------------
# _check_folder_staleness — detecção
# ---------------------------------------------------------------------------

def test_stale_folder_enters_degraded(monkeypatch):
    daemon = _build_daemon([_folder("alpha")])
    # Espelhar os dois dicts (invariante mantida em produção pelo _process_folder).
    daemon._last_successful_sync_at["alpha"] = 1_000.0
    daemon._last_successful_sync_at_mono["alpha"] = 1_000.0
    monkeypatch.setattr("drive_sync.daemon.time.time", lambda: 1_000.0 + 43_201)
    monkeypatch.setattr("drive_sync.daemon.time.monotonic", lambda: 1_000.0 + 43_201)

    daemon._check_folder_staleness()

    assert "alpha" in daemon._degraded_folders
    daemon._notifier.folder_degraded.assert_called_once()
    args, _ = daemon._notifier.folder_degraded.call_args
    assert args[0] == "alpha"
    daemon._notifier.send_status.assert_called_once()


def test_never_synced_folder_uses_daemon_start_baselines(monkeypatch):
    monkeypatch.setattr("drive_sync.daemon.time.time", lambda: 1_000.0)
    monkeypatch.setattr("drive_sync.daemon.time.monotonic", lambda: 1_000.0)
    daemon = _build_daemon([_folder("alpha")])
    assert daemon._daemon_start_time == 1_000.0
    assert daemon._daemon_start_monotonic == 1_000.0

    monkeypatch.setattr("drive_sync.daemon.time.time", lambda: 1_000.0 + 43_201)
    monkeypatch.setattr("drive_sync.daemon.time.monotonic", lambda: 1_000.0 + 43_201)
    daemon._check_folder_staleness()

    assert "alpha" in daemon._degraded_folders


def test_fresh_daemon_does_not_mark_folders_degraded(monkeypatch):
    monkeypatch.setattr("drive_sync.daemon.time.time", lambda: 1_000.0)
    monkeypatch.setattr("drive_sync.daemon.time.monotonic", lambda: 1_000.0)
    daemon = _build_daemon([_folder("alpha"), _folder("beta")])

    monkeypatch.setattr("drive_sync.daemon.time.time", lambda: 1_500.0)
    monkeypatch.setattr("drive_sync.daemon.time.monotonic", lambda: 1_500.0)
    daemon._check_folder_staleness()

    assert daemon._degraded_folders == {}
    daemon._notifier.folder_degraded.assert_not_called()


def test_check_is_idempotent(monkeypatch):
    daemon = _build_daemon([_folder("alpha")])
    daemon._last_successful_sync_at["alpha"] = 1_000.0
    daemon._last_successful_sync_at_mono["alpha"] = 1_000.0
    monkeypatch.setattr("drive_sync.daemon.time.time", lambda: 1_000.0 + 43_201)
    monkeypatch.setattr("drive_sync.daemon.time.monotonic", lambda: 1_000.0 + 43_201)

    daemon._check_folder_staleness()
    daemon._check_folder_staleness()

    daemon._notifier.folder_degraded.assert_called_once()


def test_disabled_folder_is_skipped(monkeypatch):
    daemon = _build_daemon([_folder("alpha", enabled=False)])
    monkeypatch.setattr("drive_sync.daemon.time.time", lambda: 1e9)
    monkeypatch.setattr("drive_sync.daemon.time.monotonic", lambda: 1e9)

    daemon._check_folder_staleness()

    assert "alpha" not in daemon._degraded_folders
    daemon._notifier.folder_degraded.assert_not_called()


def test_threshold_zero_is_noop(monkeypatch):
    daemon = _build_daemon([_folder("alpha")], threshold=0)
    monkeypatch.setattr("drive_sync.daemon.time.time", lambda: 1e9)
    monkeypatch.setattr("drive_sync.daemon.time.monotonic", lambda: 1e9)

    daemon._check_folder_staleness()

    assert daemon._degraded_folders == {}
    daemon._notifier.folder_degraded.assert_not_called()


# Invariante novo introduzido por ADR-007: monotonic congela com o processo
# sob suspend; wall-clock avança normalmente. Gate em monotonic evita
# falso-positivo de degraded quando o daemon não teve oportunidade de tentar.

def test_check_does_not_mark_degraded_under_suspend_gap(monkeypatch):
    # Setup: folder com sucesso há "1000s" em ambos os clocks.
    daemon = _build_daemon([_folder("alpha")])
    daemon._last_successful_sync_at["alpha"] = 1_000.0
    daemon._last_successful_sync_at_mono["alpha"] = 1_000.0

    # Simula suspend de 13h (wall avança 46_800s = 13h) mas daemon estava
    # congelado quase o tempo todo (monotonic avança só 10s).
    monkeypatch.setattr("drive_sync.daemon.time.time", lambda: 1_000.0 + 46_800)
    monkeypatch.setattr("drive_sync.daemon.time.monotonic", lambda: 1_000.0 + 10)

    daemon._check_folder_staleness()

    # Gate é monotonic — 10s < threshold (43_200s = 12h) → NÃO marca degraded.
    assert "alpha" not in daemon._degraded_folders
    daemon._notifier.folder_degraded.assert_not_called()


def test_check_reason_uses_min_wall_mono(monkeypatch):
    # Setup: cenário onde ambos os clocks avançaram > threshold, mas com gap
    # de cadência do periodic — wall conta sleep (20.5h), monotonic conta
    # só tempo ativo (12.1h). Reason deve reportar o menor.
    daemon = _build_daemon([_folder("alpha")])
    daemon._last_successful_sync_at["alpha"] = 1_000.0
    daemon._last_successful_sync_at_mono["alpha"] = 1_000.0

    elapsed_wall = 20.5 * 3600  # 73_800s
    elapsed_mono = 12.1 * 3600  # 43_560s (> threshold de 43_200s)
    monkeypatch.setattr("drive_sync.daemon.time.time", lambda: 1_000.0 + elapsed_wall)
    monkeypatch.setattr("drive_sync.daemon.time.monotonic", lambda: 1_000.0 + elapsed_mono)

    daemon._check_folder_staleness()

    assert "alpha" in daemon._degraded_folders
    reason = daemon._degraded_folders["alpha"]
    # min(20.5, 12.1) = 12.1 → reason reporta o menor.
    assert reason == "sem sucesso há 12.1h"


def test_check_reason_uses_min_wall_mono_when_wall_smaller(monkeypatch):
    # Cenário oposto: wall < mono (plausível se NTP corrigiu wall-clock para
    # trás durante a sessão, ou `date` manual). min(...) ainda escolhe o menor —
    # protege contra implementação que devolvesse `elapsed_mono` sempre.
    daemon = _build_daemon([_folder("alpha")])
    daemon._last_successful_sync_at["alpha"] = 1_000.0
    daemon._last_successful_sync_at_mono["alpha"] = 1_000.0

    elapsed_wall = 12.5 * 3600  # 45_000s
    elapsed_mono = 13.0 * 3600  # 46_800s (ambos acima do threshold 12h)
    monkeypatch.setattr("drive_sync.daemon.time.time", lambda: 1_000.0 + elapsed_wall)
    monkeypatch.setattr("drive_sync.daemon.time.monotonic", lambda: 1_000.0 + elapsed_mono)

    daemon._check_folder_staleness()

    assert "alpha" in daemon._degraded_folders
    assert daemon._degraded_folders["alpha"] == "sem sucesso há 12.5h"


# ---------------------------------------------------------------------------
# Recuperação por sucesso
# ---------------------------------------------------------------------------

def test_successful_sync_clears_degraded_silently(monkeypatch):
    folder = _folder("alpha")
    daemon = _build_daemon([folder])
    daemon._degraded_folders["alpha"] = "sem sucesso há 13.0h"
    daemon.engine = MagicMock()
    daemon.engine.bisync_folder = AsyncMock(return_value=True)
    monkeypatch.setattr("drive_sync.daemon.time.time", lambda: 2_000.0)
    monkeypatch.setattr("drive_sync.daemon.time.monotonic", lambda: 2_000.0)

    result = asyncio.run(daemon._process_folder(folder))

    assert result is True
    assert "alpha" not in daemon._degraded_folders
    # Invariante de espelhamento (ADR-007): _process_folder atualiza ambos os
    # dicts com o mesmo conjunto de chaves — se está num, está no outro.
    assert daemon._last_successful_sync_at["alpha"] == 2_000.0
    assert daemon._last_successful_sync_at_mono["alpha"] == 2_000.0
    assert (
        set(daemon._last_successful_sync_at.keys())
        == set(daemon._last_successful_sync_at_mono.keys())
    )
    # Recuperação re-emite STATUS agregada (limpa) mas NÃO chama folder_degraded.
    daemon._notifier.send_status.assert_called_once()
    daemon._notifier.folder_degraded.assert_not_called()


def test_failed_sync_does_not_update_last_successful(monkeypatch):
    folder = _folder("alpha")
    daemon = _build_daemon([folder])
    daemon.engine = MagicMock()
    daemon.engine.bisync_folder = AsyncMock(return_value=False)
    monkeypatch.setattr("drive_sync.daemon.time.time", lambda: 2_000.0)
    monkeypatch.setattr("drive_sync.daemon.time.monotonic", lambda: 2_000.0)

    result = asyncio.run(daemon._process_folder(folder))

    assert result is False
    assert "alpha" not in daemon._last_successful_sync_at
    assert "alpha" not in daemon._last_successful_sync_at_mono


# ---------------------------------------------------------------------------
# _compose_status_payload — precedência auth > folder
# ---------------------------------------------------------------------------

def test_compose_empty_state():
    daemon = _build_daemon([])
    assert daemon._compose_status_payload() == "STATUS="


def test_compose_only_folders_sorts_alphabetically():
    daemon = _build_daemon([])
    daemon._degraded_folders = {"beta": "r2", "alpha": "r1"}
    assert (
        daemon._compose_status_payload()
        == "STATUS=degraded folders: alpha (r1), beta (r2)"
    )


def test_compose_auth_only():
    daemon = _build_daemon([])
    daemon._degraded.set()
    daemon._degraded_reason = "invalid_credentials (Code=8002)"
    assert (
        daemon._compose_status_payload()
        == "STATUS=degraded: invalid_credentials (Code=8002)"
    )


def test_compose_auth_takes_precedence_over_folders():
    daemon = _build_daemon([])
    daemon._degraded.set()
    daemon._degraded_reason = "auth"
    daemon._degraded_folders = {"alpha": "stale"}
    assert daemon._compose_status_payload() == "STATUS=degraded: auth"


# ---------------------------------------------------------------------------
# Piggyback no _periodic_full_sync (ADR-005 §Decisão)
# ---------------------------------------------------------------------------

async def _run_periodic_one_tick(daemon, monkeypatch):
    """Roda _periodic_full_sync por exatamente uma iteração.

    1ª wait_for: TimeoutError (interval expirou → corpo do ciclo roda).
    2ª wait_for: retorna normalmente (stop_event setado → `return` na função).
    """
    call_count = [0]

    async def fake_wait_for(coro, timeout):
        call_count[0] += 1
        if hasattr(coro, "close"):
            coro.close()
        if call_count[0] == 1:
            raise asyncio.TimeoutError()
        daemon._stop_event.set()
        return None

    monkeypatch.setattr("drive_sync.daemon.asyncio.wait_for", fake_wait_for)
    await daemon._periodic_full_sync()


def test_periodic_full_sync_invokes_staleness_when_healthy(monkeypatch):
    daemon = _build_daemon([_folder("alpha")])
    daemon._check_folder_staleness = MagicMock()
    daemon.cfg.watcher.periodic_full_sync_seconds = 60

    asyncio.run(_run_periodic_one_tick(daemon, monkeypatch))

    daemon._check_folder_staleness.assert_called_once()


def test_periodic_full_sync_skips_staleness_when_auth_degraded(monkeypatch):
    daemon = _build_daemon([_folder("alpha")])
    daemon._check_folder_staleness = MagicMock()
    daemon._degraded.set()
    daemon.cfg.watcher.periodic_full_sync_seconds = 60

    asyncio.run(_run_periodic_one_tick(daemon, monkeypatch))

    daemon._check_folder_staleness.assert_not_called()


# ---------------------------------------------------------------------------
# STATUS recomposta após recuperação parcial (1 de N folders recupera)
# ---------------------------------------------------------------------------

def test_recovery_recomputes_status_with_remaining_folders(monkeypatch):
    folder_alpha = _folder("alpha")
    daemon = _build_daemon([folder_alpha, _folder("beta")])
    daemon._degraded_folders = {"alpha": "r1", "beta": "r2"}
    daemon.engine = MagicMock()
    daemon.engine.bisync_folder = AsyncMock(return_value=True)
    monkeypatch.setattr("drive_sync.daemon.time.time", lambda: 2_000.0)
    monkeypatch.setattr("drive_sync.daemon.time.monotonic", lambda: 2_000.0)

    asyncio.run(daemon._process_folder(folder_alpha))

    daemon._notifier.send_status.assert_called_once_with(
        "STATUS=degraded folders: beta (r2)"
    )
