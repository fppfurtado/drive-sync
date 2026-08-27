"""Tests for SyncDaemon StuckJobError handling — max-runtime kill switch (#45)."""
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
from drive_sync.sync_engine import StuckJobError


def _make_config(folders: list[FolderConfig]) -> AppConfig:
    return AppConfig(
        rclone=RcloneConfig(),
        folders=folders,
        git=GitConfig(),
        watcher=WatcherConfig(max_concurrent_jobs=2, queue_size=20),
        dedupe=DedupeConfig(),
        health_check=HealthCheckConfig(),
        logging=LoggingConfig(),
        coverage_audit=CoverageAuditConfig(),
        source_path=Path("/fake/config.yaml"),
    )


def _plain_folder(name: str = "archive") -> FolderConfig:
    # git_handling="plain" → _process_folder despacha direto para bisync_folder.
    return FolderConfig(
        name=name,
        local_path=Path(f"/tmp/{name}"),
        remote_subpath=name,
        git_handling="plain",
    )


def _build_daemon(folder: FolderConfig) -> SyncDaemon:
    daemon = SyncDaemon(_make_config([folder]))
    daemon._notifier = MagicMock()
    daemon.engine = MagicMock()
    return daemon


def test_stuck_job_marks_folder_degraded_and_fails(monkeypatch):
    folder = _plain_folder("archive")
    daemon = _build_daemon(folder)
    daemon.engine.bisync_folder = AsyncMock(side_effect=StuckJobError(7200.0))

    result = asyncio.run(daemon._process_folder(folder))

    assert result is False
    assert "archive" in daemon._degraded_folders
    reason = daemon._degraded_folders["archive"]
    assert "2.0h" in reason and "max_job_runtime" in reason
    daemon._notifier.folder_degraded.assert_called_once()
    daemon._notifier.send_status.assert_called_once()


def test_stuck_job_does_not_mark_success(monkeypatch):
    folder = _plain_folder("archive")
    daemon = _build_daemon(folder)
    daemon.engine.bisync_folder = AsyncMock(side_effect=StuckJobError(7200.0))

    asyncio.run(daemon._process_folder(folder))

    # Sem sucesso registrado → staleness (ADR-005) segue livre para escalar.
    assert "archive" not in daemon._last_successful_sync_at
    assert "archive" not in daemon._last_successful_sync_at_mono


def test_stuck_job_is_per_folder_not_global_pause(monkeypatch):
    """StuckJob NÃO pausa os workers (distinto de auth-degraded ADR-003).

    _process_folder trata e retorna False sem chamar o caminho de pausa global.
    """
    folder = _plain_folder("archive")
    daemon = _build_daemon(folder)
    daemon.engine.bisync_folder = AsyncMock(side_effect=StuckJobError(3600.0))

    # O Event de pausa global (setado por _enter_degraded no caminho de auth)
    # NÃO deve ser ativado por um stuck job — este é per-folder.
    assert not daemon._degraded.is_set()
    asyncio.run(daemon._process_folder(folder))
    assert not daemon._degraded.is_set()
