"""Tests for SyncDaemon — job routing and worker deduplication."""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

from drive_sync.config import (
    AppConfig,
    DedupeConfig,
    FolderConfig,
    GitConfig,
    LoggingConfig,
    RcloneConfig,
    WatcherConfig,
)
from drive_sync.daemon import SyncDaemon


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
