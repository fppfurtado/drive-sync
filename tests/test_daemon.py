"""Tests for SyncDaemon — job routing and worker deduplication."""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

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
from drive_sync.sync_engine import AuthDegradedError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(folders: list[FolderConfig] | None = None) -> AppConfig:
    return AppConfig(
        rclone=RcloneConfig(),
        folders=folders or [],
        git=GitConfig(),
        watcher=WatcherConfig(max_concurrent_jobs=2, queue_size=20),
        dedupe=DedupeConfig(),
        health_check=HealthCheckConfig(),
        logging=LoggingConfig(),
        source_path=Path("/fake/config.yaml"),
    )


def _folder(name: str = "test", git_mode: str = "bisync") -> FolderConfig:
    return FolderConfig(
        name=name,
        local_path=Path(f"/tmp/{name}"),
        remote_subpath=name,
        git_mode=git_mode,
    )


# ---------------------------------------------------------------------------
# _is_bundle_flow
# ---------------------------------------------------------------------------

def test_bundle_mode_is_bundle_flow():
    daemon = SyncDaemon(_make_config())
    assert daemon._is_bundle_flow(_folder(git_mode="bundle")) is True


def test_bisync_mode_is_not_bundle_flow():
    daemon = SyncDaemon(_make_config())
    assert daemon._is_bundle_flow(_folder(git_mode="bisync")) is False


def test_off_mode_is_not_bundle_flow():
    daemon = SyncDaemon(_make_config())
    assert daemon._is_bundle_flow(_folder(git_mode="off")) is False


# ---------------------------------------------------------------------------
# _process_folder — routing
# ---------------------------------------------------------------------------

async def test_bisync_mode_calls_engine_bisync():
    folder = _folder(git_mode="bisync")
    daemon = SyncDaemon(_make_config([folder]))
    daemon.engine.bisync_folder = AsyncMock(return_value=True)

    await daemon._process_folder(folder)

    daemon.engine.bisync_folder.assert_called_once_with(folder)


async def test_off_mode_calls_engine_bisync():
    folder = _folder(git_mode="off")
    daemon = SyncDaemon(_make_config([folder]))
    daemon.engine.bisync_folder = AsyncMock(return_value=True)

    await daemon._process_folder(folder)

    daemon.engine.bisync_folder.assert_called_once_with(folder)


async def test_bundle_mode_calls_sync_git_folder():
    folder = _folder(git_mode="bundle")
    daemon = SyncDaemon(_make_config([folder]))
    daemon._sync_git_folder = AsyncMock()

    await daemon._process_folder(folder)

    daemon._sync_git_folder.assert_called_once_with(folder)


# ---------------------------------------------------------------------------
# Worker — in-flight deduplication
# ---------------------------------------------------------------------------

async def test_worker_discards_job_already_inflight():
    """A job whose folder name is already in _inflight must be skipped."""
    folder = _folder()
    daemon = SyncDaemon(_make_config([folder]))

    processed: list[str] = []

    async def mock_bisync(f: FolderConfig) -> bool:
        processed.append(f.name)
        return True

    daemon.engine.bisync_folder = mock_bisync

    # Simulate the folder already being processed by another worker.
    daemon._inflight.add(folder.name)
    await daemon.queue.put(folder.name)

    daemon._stop_event = asyncio.Event()
    worker = asyncio.create_task(daemon._worker(0))
    await asyncio.sleep(0.05)
    daemon._stop_event.set()
    worker.cancel()
    try:
        await worker
    except asyncio.CancelledError:
        pass

    assert processed == []


async def test_worker_processes_job_when_not_inflight():
    """A job whose folder is not in _inflight must be processed normally."""
    folder = _folder()
    daemon = SyncDaemon(_make_config([folder]))

    processed: list[str] = []

    async def mock_bisync(f: FolderConfig) -> bool:
        processed.append(f.name)
        return True

    daemon.engine.bisync_folder = mock_bisync

    await daemon.queue.put(folder.name)

    daemon._stop_event = asyncio.Event()
    worker = asyncio.create_task(daemon._worker(0))
    await asyncio.sleep(0.1)
    daemon._stop_event.set()
    worker.cancel()
    try:
        await worker
    except asyncio.CancelledError:
        pass

    assert processed == [folder.name]


async def test_worker_removes_inflight_after_completion():
    """After a job finishes, the folder name must be removed from _inflight."""
    folder = _folder()
    daemon = SyncDaemon(_make_config([folder]))
    daemon.engine.bisync_folder = AsyncMock(return_value=True)

    await daemon.queue.put(folder.name)

    daemon._stop_event = asyncio.Event()
    worker = asyncio.create_task(daemon._worker(0))
    await asyncio.sleep(0.1)
    daemon._stop_event.set()
    worker.cancel()
    try:
        await worker
    except asyncio.CancelledError:
        pass

    assert folder.name not in daemon._inflight


async def test_worker_removes_inflight_after_exception():
    """Even when _process_folder raises, the folder must leave _inflight."""
    folder = _folder()
    daemon = SyncDaemon(_make_config([folder]))

    async def crashing_bisync(f: FolderConfig) -> bool:
        raise RuntimeError("boom")

    daemon.engine.bisync_folder = crashing_bisync

    await daemon.queue.put(folder.name)

    daemon._stop_event = asyncio.Event()
    worker = asyncio.create_task(daemon._worker(0))
    await asyncio.sleep(0.1)
    daemon._stop_event.set()
    worker.cancel()
    try:
        await worker
    except asyncio.CancelledError:
        pass

    assert folder.name not in daemon._inflight


async def test_worker_ignores_unknown_folder_name():
    """A folder name not present in cfg.folders must be silently discarded."""
    daemon = SyncDaemon(_make_config([]))  # no folders configured
    daemon.engine.bisync_folder = AsyncMock(return_value=True)

    await daemon.queue.put("unknown_folder")

    daemon._stop_event = asyncio.Event()
    worker = asyncio.create_task(daemon._worker(0))
    await asyncio.sleep(0.05)
    daemon._stop_event.set()
    worker.cancel()
    try:
        await worker
    except asyncio.CancelledError:
        pass

    daemon.engine.bisync_folder.assert_not_called()


# ---------------------------------------------------------------------------
# Pause-on-failure: AuthDegradedError dispara degraded, workers drenam
# ---------------------------------------------------------------------------

async def test_worker_enters_degraded_on_auth_error():
    folder = _folder()
    daemon = SyncDaemon(_make_config([folder]))
    daemon._notifier = MagicMock()  # silencia subprocess calls

    async def raising_bisync(f: FolderConfig) -> bool:
        raise AuthDegradedError(kind="invalid_credentials", code=8002, stderr_tail="...")

    daemon.engine.bisync_folder = raising_bisync

    await daemon.queue.put(folder.name)

    daemon._stop_event = asyncio.Event()
    worker = asyncio.create_task(daemon._worker(0))
    await asyncio.sleep(0.1)
    daemon._stop_event.set()
    worker.cancel()
    try:
        await worker
    except asyncio.CancelledError:
        pass

    assert daemon._degraded.is_set()
    assert "invalid_credentials" in (daemon._degraded_reason or "")
    assert "(Code=8002)" in (daemon._degraded_reason or "")
    daemon._notifier.degraded.assert_called_once()


async def test_worker_drains_queue_when_degraded():
    folder = _folder()
    daemon = SyncDaemon(_make_config([folder]))
    daemon._notifier = MagicMock()
    daemon._enter_degraded("pre-set for test")

    processed: list[str] = []

    async def tracking_bisync(f: FolderConfig) -> bool:
        processed.append(f.name)
        return True

    daemon.engine.bisync_folder = tracking_bisync

    await daemon.queue.put(folder.name)

    daemon._stop_event = asyncio.Event()
    worker = asyncio.create_task(daemon._worker(0))
    await asyncio.sleep(0.1)
    daemon._stop_event.set()
    worker.cancel()
    try:
        await worker
    except asyncio.CancelledError:
        pass

    assert processed == []


def test_enter_degraded_is_idempotent():
    daemon = SyncDaemon(_make_config())
    daemon._notifier = MagicMock()

    daemon._enter_degraded("first reason")
    daemon._enter_degraded("second reason")

    assert daemon._degraded_reason == "first reason"
    daemon._notifier.degraded.assert_called_once_with("first reason")


async def test_enter_degraded_idempotent_under_concurrent_calls():
    """Regression guard contra refactor que insira await em _enter_degraded."""
    daemon = SyncDaemon(_make_config())
    daemon._notifier = MagicMock()

    async def call(reason: str):
        daemon._enter_degraded(reason)

    await asyncio.gather(*(call(f"r{i}") for i in range(20)))

    assert daemon._notifier.degraded.call_count == 1


async def test_periodic_full_sync_skips_when_degraded():
    folder = _folder()
    cfg = _make_config([folder])
    cfg.watcher.periodic_full_sync_seconds = 1
    daemon = SyncDaemon(cfg)
    daemon._notifier = MagicMock()
    daemon._enter_degraded("pre-set for test")

    daemon._stop_event = asyncio.Event()
    task = asyncio.create_task(daemon._periodic_full_sync())
    await asyncio.sleep(1.2)
    daemon._stop_event.set()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert daemon.queue.qsize() == 0


async def test_auth_probe_loop_skips_when_already_degraded():
    cfg = _make_config()
    cfg.health_check = HealthCheckConfig(enabled=True, interval_seconds=1)
    daemon = SyncDaemon(cfg)
    daemon._notifier = MagicMock()
    daemon._enter_degraded("pre-set for test")
    daemon.engine.auth_probe = AsyncMock()

    daemon._stop_event = asyncio.Event()
    task = asyncio.create_task(daemon._auth_probe_loop())
    await asyncio.sleep(1.2)
    daemon._stop_event.set()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    daemon.engine.auth_probe.assert_not_called()


# ---------------------------------------------------------------------------
# _auth_probe_loop
# ---------------------------------------------------------------------------

async def test_auth_probe_loop_triggers_degraded_on_auth_error():
    cfg = _make_config()
    cfg.health_check = HealthCheckConfig(enabled=True, interval_seconds=1)
    daemon = SyncDaemon(cfg)
    daemon._notifier = MagicMock()

    async def raising_probe():
        raise AuthDegradedError(kind="invalid_credentials", code=8002, stderr_tail="...")

    daemon.engine.auth_probe = raising_probe

    # Pula o sleep do intervalo — interval=1 já é o mínimo prático.
    daemon._stop_event = asyncio.Event()
    loop_task = asyncio.create_task(daemon._auth_probe_loop())
    await asyncio.sleep(1.2)
    daemon._stop_event.set()
    loop_task.cancel()
    try:
        await loop_task
    except asyncio.CancelledError:
        pass

    assert daemon._degraded.is_set()
    assert "(Code=8002)" in (daemon._degraded_reason or "")


async def test_auth_probe_loop_skips_when_disabled():
    cfg = _make_config()
    cfg.health_check = HealthCheckConfig(enabled=False, interval_seconds=1)
    daemon = SyncDaemon(cfg)
    daemon.engine.auth_probe = AsyncMock()

    daemon._stop_event = asyncio.Event()
    await daemon._auth_probe_loop()  # retorna imediatamente

    daemon.engine.auth_probe.assert_not_called()
    assert not daemon._degraded.is_set()


async def test_auth_probe_loop_ignores_non_auth_exception():
    cfg = _make_config()
    cfg.health_check = HealthCheckConfig(enabled=True, interval_seconds=1)
    daemon = SyncDaemon(cfg)
    daemon._notifier = MagicMock()

    async def network_failure():
        raise OSError("network unreachable")

    daemon.engine.auth_probe = network_failure

    daemon._stop_event = asyncio.Event()
    loop_task = asyncio.create_task(daemon._auth_probe_loop())
    await asyncio.sleep(1.2)
    daemon._stop_event.set()
    loop_task.cancel()
    try:
        await loop_task
    except (asyncio.CancelledError, OSError):
        pass

    assert not daemon._degraded.is_set()
