"""Tests do degrade poll-only em esgotamento inotify (#20).

Camada watcher: FilesystemWatcher.start converte ENOSPC/EMFILE em
WatchLimitError após liberar watches parciais. Camada daemon:
_start_watcher degrada para poll-only (watcher off, periodic segue) em vez
de crashar — salvo quando o periodic está desligado (aí o erro segue fatal,
com mensagem actionable).
"""
import asyncio
import errno
from pathlib import Path
from unittest.mock import MagicMock

import pytest

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
from drive_sync.daemon import SyncDaemon
from drive_sync.watcher import FilesystemWatcher, WatchLimitError


def _make_config(
    folders: list[FolderConfig] | None = None,
    watcher: WatcherConfig | None = None,
) -> AppConfig:
    return AppConfig(
        rclone=RcloneConfig(),
        folders=folders or [],
        git=GitConfig(),
        watcher=watcher or WatcherConfig(max_concurrent_jobs=2, queue_size=20),
        dedupe=DedupeConfig(),
        health_check=HealthCheckConfig(),
        logging=LoggingConfig(),
        source_path=Path("/fake/config.yaml"),
    )


def _folder(tmp_path: Path, name: str = "test") -> FolderConfig:
    local = tmp_path / name
    local.mkdir(exist_ok=True)
    return FolderConfig(name=name, local_path=local, remote_subpath=name)


def _watcher_with_mock_observer(tmp_path: Path) -> FilesystemWatcher:
    cfg = _make_config([_folder(tmp_path)])
    watcher = FilesystemWatcher(cfg, loop=MagicMock(), queue=MagicMock())
    watcher.observer = MagicMock()
    return watcher


# ---------------------------------------------------------------------------
# FilesystemWatcher.start — conversão + teardown
# ---------------------------------------------------------------------------

def test_enospc_on_schedule_raises_watch_limit_error(tmp_path):
    watcher = _watcher_with_mock_observer(tmp_path)
    watcher.observer.schedule.side_effect = OSError(
        errno.ENOSPC, "inotify watch limit reached"
    )

    with pytest.raises(WatchLimitError, match="ENOSPC"):
        watcher.start()

    watcher.observer.unschedule_all.assert_called_once()
    watcher.observer.stop.assert_called_once()


def test_enospc_on_observer_start_raises_watch_limit_error(tmp_path):
    watcher = _watcher_with_mock_observer(tmp_path)
    watcher.observer.start.side_effect = OSError(
        errno.ENOSPC, "inotify watch limit reached"
    )

    with pytest.raises(WatchLimitError):
        watcher.start()

    watcher.observer.unschedule_all.assert_called_once()


def test_emfile_instance_limit_also_converts(tmp_path):
    watcher = _watcher_with_mock_observer(tmp_path)
    watcher.observer.start.side_effect = OSError(
        errno.EMFILE, "inotify instance limit reached"
    )

    with pytest.raises(WatchLimitError, match="EMFILE"):
        watcher.start()


def test_unrelated_oserror_propagates_untouched(tmp_path):
    watcher = _watcher_with_mock_observer(tmp_path)
    watcher.observer.start.side_effect = OSError(errno.EACCES, "permission denied")

    with pytest.raises(OSError) as excinfo:
        watcher.start()

    assert not isinstance(excinfo.value, WatchLimitError)
    watcher.observer.unschedule_all.assert_not_called()


def test_teardown_failure_does_not_mask_watch_limit_error(tmp_path):
    watcher = _watcher_with_mock_observer(tmp_path)
    watcher.observer.start.side_effect = OSError(errno.ENOSPC, "limit")
    watcher.observer.unschedule_all.side_effect = RuntimeError("emitter morto")

    with pytest.raises(WatchLimitError):
        watcher.start()

    # Teardown é passo a passo: falha no unschedule_all não pula o stop —
    # pular deixaria emitters vivos com o daemon em poll-only.
    watcher.observer.stop.assert_called_once()


def test_disk_full_enospc_from_elsewhere_is_not_converted(tmp_path):
    """mkdir/disk-full fora da superfície inotify não vira WatchLimitError.

    A materialização de local_paths saiu do watcher (é setup do daemon);
    o try cobre apenas schedule/start. Um OSError ENOSPC vindo de outra
    camada (ex.: handler init) segue sendo o erro real.
    """
    cfg = _make_config([_folder(tmp_path)])
    watcher = FilesystemWatcher(cfg, loop=MagicMock(), queue=MagicMock())
    missing = tmp_path / "sumiu"
    cfg.folders[0].local_path = missing
    watcher.observer = MagicMock()
    # Sem mkdir no start(): pasta ausente não é criada pelo watcher.
    watcher.start()
    assert not missing.exists()


# ---------------------------------------------------------------------------
# SyncDaemon._start_watcher — degrade poll-only vs fatal
# ---------------------------------------------------------------------------

def _daemon_with_failing_watcher(monkeypatch, cfg: AppConfig) -> SyncDaemon:
    daemon = SyncDaemon(cfg)
    daemon._notifier = MagicMock()
    watcher_cls = MagicMock()
    watcher_cls.return_value.start.side_effect = WatchLimitError(
        "inotify esgotado (ENOSPC: limit)"
    )
    monkeypatch.setattr("drive_sync.daemon.FilesystemWatcher", watcher_cls)
    return daemon


async def test_start_watcher_degrades_to_poll_only(monkeypatch):
    daemon = _daemon_with_failing_watcher(monkeypatch, _make_config())

    daemon._start_watcher(asyncio.get_running_loop())

    assert daemon._watcher is None
    assert "poll-only a cada 1800s" in daemon._watcher_degraded_reason
    daemon._notifier.watcher_degraded.assert_called_once()
    daemon._notifier.send_status.assert_called_once_with(
        daemon._compose_status_payload()
    )


async def test_start_watcher_stays_fatal_without_periodic(monkeypatch):
    cfg = _make_config(
        watcher=WatcherConfig(
            periodic_full_sync_seconds=0, folder_staleness_threshold_seconds=0
        )
    )
    daemon = _daemon_with_failing_watcher(monkeypatch, cfg)

    with pytest.raises(WatchLimitError):
        daemon._start_watcher(asyncio.get_running_loop())

    assert daemon._watcher is None
    daemon._notifier.watcher_degraded.assert_not_called()


async def test_start_watcher_success_keeps_watcher(monkeypatch):
    daemon = SyncDaemon(_make_config())
    daemon._notifier = MagicMock()
    watcher_cls = MagicMock()
    monkeypatch.setattr("drive_sync.daemon.FilesystemWatcher", watcher_cls)

    daemon._start_watcher(asyncio.get_running_loop())

    assert daemon._watcher is watcher_cls.return_value
    assert daemon._watcher_degraded_reason is None
    daemon._notifier.watcher_degraded.assert_not_called()


# ---------------------------------------------------------------------------
# _ensure_local_paths — materialização é do daemon (compartilhada c/ poll-only)
# ---------------------------------------------------------------------------

def test_ensure_local_paths_creates_missing_dirs(tmp_path):
    folder = FolderConfig(
        name="ghost", local_path=tmp_path / "nao-existe", remote_subpath="ghost"
    )
    daemon = SyncDaemon(_make_config([folder]))

    daemon._ensure_local_paths()

    assert folder.local_path.is_dir()


def test_ensure_local_paths_skips_disabled(tmp_path):
    folder = FolderConfig(
        name="off",
        local_path=tmp_path / "off",
        remote_subpath="off",
        enabled=False,
    )
    daemon = SyncDaemon(_make_config([folder]))

    daemon._ensure_local_paths()

    assert not folder.local_path.exists()


# ---------------------------------------------------------------------------
# _check_watcher_liveness — morte do watcher em runtime também degrada
# ---------------------------------------------------------------------------

def test_liveness_dead_observer_degrades():
    daemon = SyncDaemon(_make_config())
    daemon._notifier = MagicMock()
    watcher = MagicMock()
    watcher.observer.is_alive.return_value = False
    daemon._watcher = watcher

    daemon._check_watcher_liveness()

    assert daemon._watcher is None
    assert "watcher morreu em runtime" in daemon._watcher_degraded_reason
    watcher.stop.assert_called_once()
    daemon._notifier.watcher_degraded.assert_called_once()


def test_liveness_dead_emitter_degrades():
    daemon = SyncDaemon(_make_config())
    daemon._notifier = MagicMock()
    watcher = MagicMock()
    watcher.observer.is_alive.return_value = True
    dead_emitter = MagicMock()
    dead_emitter.is_alive.return_value = False
    watcher.observer.emitters = [MagicMock(), dead_emitter]

    daemon._watcher = watcher

    daemon._check_watcher_liveness()

    assert daemon._watcher is None
    daemon._notifier.watcher_degraded.assert_called_once()


def test_liveness_healthy_watcher_is_noop():
    daemon = SyncDaemon(_make_config())
    daemon._notifier = MagicMock()
    watcher = MagicMock()
    watcher.observer.is_alive.return_value = True
    watcher.observer.emitters = [MagicMock()]
    daemon._watcher = watcher

    daemon._check_watcher_liveness()

    assert daemon._watcher is watcher
    assert daemon._watcher_degraded_reason is None
    daemon._notifier.watcher_degraded.assert_not_called()


def test_liveness_noop_when_already_degraded():
    daemon = SyncDaemon(_make_config())
    daemon._notifier = MagicMock()
    daemon._watcher = None
    daemon._watcher_degraded_reason = "watcher off"

    daemon._check_watcher_liveness()

    daemon._notifier.watcher_degraded.assert_not_called()


# ---------------------------------------------------------------------------
# _compose_status_payload — precedência auth > watcher > folders
# ---------------------------------------------------------------------------

def test_compose_watcher_only():
    daemon = SyncDaemon(_make_config())
    daemon._watcher_degraded_reason = "watcher off (ENOSPC) — poll-only a cada 1800s"
    assert (
        daemon._compose_status_payload()
        == "STATUS=degraded: watcher off (ENOSPC) — poll-only a cada 1800s"
    )


def test_compose_watcher_and_folders_combined():
    daemon = SyncDaemon(_make_config())
    daemon._watcher_degraded_reason = "watcher off"
    daemon._degraded_folders = {"docs": "sem sucesso há 13.0h"}
    assert (
        daemon._compose_status_payload()
        == "STATUS=degraded: watcher off; folders: docs (sem sucesso há 13.0h)"
    )


def test_compose_auth_takes_precedence_over_watcher():
    daemon = SyncDaemon(_make_config())
    daemon._degraded_reason = "auth"
    daemon._degraded.set()
    daemon._watcher_degraded_reason = "watcher off"
    assert daemon._compose_status_payload() == "STATUS=degraded: auth"
