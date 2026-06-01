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


def _folder(
    name: str = "test", git_handling: str = "auto", cooldown_seconds: int = 0
) -> FolderConfig:
    return FolderConfig(
        name=name,
        local_path=Path(f"/tmp/{name}"),
        remote_subpath=name,
        git_handling=git_handling,
        cooldown_seconds=cooldown_seconds,
    )


# ---------------------------------------------------------------------------
# _is_bundle_flow
# ---------------------------------------------------------------------------

def test_bundle_handling_is_bundle_flow():
    daemon = SyncDaemon(_make_config())
    assert daemon._is_bundle_flow(_folder(git_handling="bundle")) is True


def test_auto_handling_is_not_bundle_flow():
    daemon = SyncDaemon(_make_config())
    assert daemon._is_bundle_flow(_folder(git_handling="auto")) is False


def test_plain_handling_is_not_bundle_flow():
    daemon = SyncDaemon(_make_config())
    assert daemon._is_bundle_flow(_folder(git_handling="plain")) is False


# ---------------------------------------------------------------------------
# _process_folder — routing
# ---------------------------------------------------------------------------

async def test_auto_handling_calls_engine_bisync():
    folder = _folder(git_handling="auto")
    daemon = SyncDaemon(_make_config([folder]))
    daemon.engine.bisync_folder = AsyncMock(return_value=True)

    await daemon._process_folder(folder)

    daemon.engine.bisync_folder.assert_called_once_with(folder)


async def test_plain_handling_calls_engine_bisync():
    folder = _folder(git_handling="plain")
    daemon = SyncDaemon(_make_config([folder]))
    daemon.engine.bisync_folder = AsyncMock(return_value=True)

    await daemon._process_folder(folder)

    daemon.engine.bisync_folder.assert_called_once_with(folder)


async def test_bundle_handling_calls_sync_git_folder():
    folder = _folder(git_handling="bundle")
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


# ---------------------------------------------------------------------------
# cooldown_seconds — gate per folder (ADR-004)
# ---------------------------------------------------------------------------

async def test_cooldown_disabled_processes_every_event():
    """cooldown_seconds=0 (default) preserva o comportamento atual."""
    folder = _folder(cooldown_seconds=0)
    daemon = SyncDaemon(_make_config([folder]))

    processed: list[str] = []

    async def mock_bisync(f: FolderConfig) -> bool:
        processed.append(f.name)
        return True

    daemon.engine.bisync_folder = mock_bisync

    await daemon.queue.put(folder.name)
    await daemon.queue.put(folder.name)

    daemon._stop_event = asyncio.Event()
    worker = asyncio.create_task(daemon._worker(0))
    await asyncio.sleep(0.15)
    daemon._stop_event.set()
    worker.cancel()
    try:
        await worker
    except asyncio.CancelledError:
        pass

    assert len(processed) == 2
    assert daemon._cooldown_scheduled == set()


async def test_cooldown_event_inside_window_is_deferred():
    """Evento dentro da janela cria task diferida e não chama _process_folder."""
    folder = _folder(cooldown_seconds=60)
    daemon = SyncDaemon(_make_config([folder]))
    daemon.engine.bisync_folder = AsyncMock(return_value=True)

    loop = asyncio.get_running_loop()
    daemon._last_sync_at[folder.name] = loop.time()  # janela acabou de abrir
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

    daemon.engine.bisync_folder.assert_not_called()
    assert folder.name in daemon._cooldown_scheduled
    assert len(daemon._cooldown_tasks) == 1


async def test_cooldown_subsequent_events_are_absorbed():
    """Eventos extras na mesma janela não criam tasks adicionais."""
    folder = _folder(cooldown_seconds=60)
    daemon = SyncDaemon(_make_config([folder]))
    daemon.engine.bisync_folder = AsyncMock(return_value=True)

    loop = asyncio.get_running_loop()
    daemon._last_sync_at[folder.name] = loop.time()
    for _ in range(5):
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

    daemon.engine.bisync_folder.assert_not_called()
    assert len(daemon._cooldown_tasks) == 1


async def test_cooldown_after_window_processes_normally():
    """Com last_sync_at fora da janela, evento processa e atualiza o timestamp."""
    folder = _folder(cooldown_seconds=1)
    daemon = SyncDaemon(_make_config([folder]))
    daemon.engine.bisync_folder = AsyncMock(return_value=True)

    loop = asyncio.get_running_loop()
    daemon._last_sync_at[folder.name] = loop.time() - 10  # bem fora da janela
    await daemon.queue.put(folder.name)

    daemon._stop_event = asyncio.Event()
    worker = asyncio.create_task(daemon._worker(0))
    await asyncio.sleep(0.15)
    daemon._stop_event.set()
    worker.cancel()
    try:
        await worker
    except asyncio.CancelledError:
        pass

    daemon.engine.bisync_folder.assert_called_once_with(folder)
    assert daemon._last_sync_at[folder.name] > loop.time() - 1  # atualizado from-start


async def test_cooldown_deferred_task_reenqueues_after_window():
    """Task diferida acorda no fim da janela e re-enfileira o folder."""
    folder = _folder(cooldown_seconds=1)
    daemon = SyncDaemon(_make_config([folder]))
    daemon.engine.bisync_folder = AsyncMock(return_value=True)

    loop = asyncio.get_running_loop()
    # Janela quase no fim — sobra ~50ms para o sleep da task diferida acordar.
    daemon._last_sync_at[folder.name] = loop.time() - folder.cooldown_seconds + 0.05
    await daemon.queue.put(folder.name)

    daemon._stop_event = asyncio.Event()
    worker = asyncio.create_task(daemon._worker(0))
    await asyncio.sleep(0.2)
    daemon._stop_event.set()
    worker.cancel()
    try:
        await worker
    except asyncio.CancelledError:
        pass

    daemon.engine.bisync_folder.assert_called_once_with(folder)
    assert folder.name not in daemon._cooldown_scheduled


async def test_cooldown_deferred_task_cancelled_on_shutdown():
    """Cancellation simulando shutdown não re-enfileira e não vaza estado."""
    folder = _folder(cooldown_seconds=60)
    daemon = SyncDaemon(_make_config([folder]))
    daemon.engine.bisync_folder = AsyncMock(return_value=True)

    loop = asyncio.get_running_loop()
    daemon._last_sync_at[folder.name] = loop.time()
    await daemon.queue.put(folder.name)

    daemon._stop_event = asyncio.Event()
    worker = asyncio.create_task(daemon._worker(0))
    await asyncio.sleep(0.05)  # deixa o worker criar a task diferida
    daemon._stop_event.set()
    worker.cancel()
    try:
        await worker
    except asyncio.CancelledError:
        pass

    assert len(daemon._cooldown_tasks) == 1
    qsize_before = daemon.queue.qsize()
    for task in list(daemon._cooldown_tasks):
        task.cancel()
    await asyncio.gather(*daemon._cooldown_tasks, return_exceptions=True)

    assert daemon.queue.qsize() == qsize_before  # não re-enfileirou


async def test_cooldown_deferred_drops_when_degraded_on_wakeup():
    """Task diferida que acorda durante degraded deposita na queue; worker descarta."""
    folder = _folder(cooldown_seconds=1)
    daemon = SyncDaemon(_make_config([folder]))
    daemon._notifier = MagicMock()
    daemon.engine.bisync_folder = AsyncMock(return_value=True)

    loop = asyncio.get_running_loop()
    # Mesma técnica da janela curta — task diferida acorda em ~50ms.
    daemon._last_sync_at[folder.name] = loop.time() - folder.cooldown_seconds + 0.05
    await daemon.queue.put(folder.name)

    daemon._stop_event = asyncio.Event()
    worker = asyncio.create_task(daemon._worker(0))
    await asyncio.sleep(0.02)  # worker cria a task diferida
    daemon._enter_degraded("simulado para teste")
    await asyncio.sleep(0.2)  # task diferida acorda durante degraded
    daemon._stop_event.set()
    worker.cancel()
    try:
        await worker
    except asyncio.CancelledError:
        pass

    daemon.engine.bisync_folder.assert_not_called()


async def test_cooldown_gates_periodic_full_sync():
    """Enfileiramentos do periodic_full_sync passam pelo mesmo gate (ADR-004)."""
    folder = _folder(cooldown_seconds=60)
    daemon = SyncDaemon(_make_config([folder]))
    daemon.engine.bisync_folder = AsyncMock(return_value=True)

    loop = asyncio.get_running_loop()
    daemon._last_sync_at[folder.name] = loop.time()  # janela aberta

    # Periodic_full_sync apenas faz queue.put — simulamos isso diretamente.
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

    daemon.engine.bisync_folder.assert_not_called()
    assert folder.name in daemon._cooldown_scheduled
