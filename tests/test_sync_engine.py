"""Tests for sync_engine — remote URI building and bisync behaviour."""
import asyncio
import time
from pathlib import Path
from unittest.mock import patch

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
from drive_sync.sync_engine import (
    AuthDegradedError,
    RcloneEngine,
    _classify_rclone_stderr,
    _run,
    _state_marker_for,
    remote_uri_for,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _app(remote_name: str = "proton", remote_root: str = "Sync") -> AppConfig:
    return AppConfig(
        rclone=RcloneConfig(remote_name=remote_name, remote_root=remote_root),
        folders=[],
        git=GitConfig(),
        watcher=WatcherConfig(),
        dedupe=DedupeConfig(),
        health_check=HealthCheckConfig(),
        logging=LoggingConfig(),
        source_path=Path("/fake/config.yaml"),
    )


def _folder(name: str = "docs", remote_subpath: str = "Documents", *, auto_exclude: bool = False, local_path: Path | None = None) -> FolderConfig:
    return FolderConfig(
        name=name,
        local_path=local_path or Path(f"/tmp/{name}"),
        remote_subpath=remote_subpath,
        auto_exclude=auto_exclude,
    )


async def _fake_ok(cmd: list[str]) -> tuple[int, str, str]:
    return (0, "", "")


async def _fake_err(cmd: list[str]) -> tuple[int, str, str]:
    return (1, "", "rclone: something went wrong")


def _bisync_calls(captured: list[list[str]]) -> list[list[str]]:
    """Filtra apenas as chamadas de bisync (exclui mkdir)."""
    return [c for c in captured if "bisync" in c]


def _mkdir_calls(captured: list[list[str]]) -> list[list[str]]:
    return [c for c in captured if "mkdir" in c]


# ---------------------------------------------------------------------------
# remote_uri_for
# ---------------------------------------------------------------------------

def test_remote_uri_basic():
    uri = remote_uri_for(_folder(remote_subpath="Documents"), _app(remote_name="proton", remote_root="Sync"))
    assert uri == "proton:Sync/Documents"


def test_remote_uri_with_sub():
    uri = remote_uri_for(_folder(remote_subpath="Code"), _app(remote_name="drive", remote_root="Root"), sub="repo.gitbundle")
    assert uri == "drive:Root/Code/repo.gitbundle"


def test_remote_uri_sub_none_omitted():
    uri = remote_uri_for(_folder(remote_subpath="Photos"), _app(), sub=None)
    assert uri == "proton:Sync/Photos"


def test_remote_uri_strips_extra_slashes():
    app = _app(remote_root="Sync")
    folder = FolderConfig(name="x", local_path=Path("/tmp/x"), remote_subpath="nested/path")
    assert remote_uri_for(folder, app) == "proton:Sync/nested/path"


# ---------------------------------------------------------------------------
# bisync_folder — first run adds --resync, subsequent run does not
# ---------------------------------------------------------------------------

async def test_first_run_adds_resync_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    app = _app()
    engine = RcloneEngine(app)
    folder = _folder(local_path=tmp_path / "local")
    captured: list[list[str]] = []

    async def fake_run(cmd):
        captured.append(cmd)
        return (0, "", "")

    with patch("drive_sync.sync_engine._run", fake_run):
        result = await engine.bisync_folder(folder)

    assert result is True
    bisync_cmds = _bisync_calls(captured)
    assert len(bisync_cmds) == 1
    assert "--resync" in bisync_cmds[0]


async def test_subsequent_run_omits_resync(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    app = _app()
    engine = RcloneEngine(app)
    folder = _folder(local_path=tmp_path / "local")

    remote = remote_uri_for(folder, app)
    marker = _state_marker_for(folder.local_path, remote)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()

    captured: list[list[str]] = []

    async def fake_run(cmd):
        captured.append(cmd)
        return (0, "", "")

    with patch("drive_sync.sync_engine._run", fake_run):
        result = await engine.bisync_folder(folder)

    assert result is True
    bisync_cmds = _bisync_calls(captured)
    assert len(bisync_cmds) == 1
    assert "--resync" not in bisync_cmds[0]


async def test_failed_bisync_returns_false(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    engine = RcloneEngine(_app())
    folder = _folder(local_path=tmp_path / "local")

    # mkdir succeeds, bisync fails
    async def fake_run(cmd):
        if "mkdir" in cmd:
            return (0, "", "")
        return (1, "", "rclone: something went wrong")

    with patch("drive_sync.sync_engine._run", fake_run):
        result = await engine.bisync_folder(folder)

    assert result is False


async def test_success_creates_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    app = _app()
    engine = RcloneEngine(app)
    folder = _folder(local_path=tmp_path / "local")

    with patch("drive_sync.sync_engine._run", _fake_ok):
        await engine.bisync_folder(folder)

    marker = _state_marker_for(folder.local_path, remote_uri_for(folder, app))
    assert marker.exists()


async def test_failure_does_not_create_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    app = _app()
    engine = RcloneEngine(app)
    folder = _folder(local_path=tmp_path / "local")

    with patch("drive_sync.sync_engine._run", _fake_err):
        await engine.bisync_folder(folder)

    marker = _state_marker_for(folder.local_path, remote_uri_for(folder, app))
    assert not marker.exists()


async def test_auto_exclude_appends_preset_patterns(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    engine = RcloneEngine(_app())
    folder = _folder(local_path=tmp_path / "code", auto_exclude=True)
    captured: list[list[str]] = []

    async def fake_run(cmd):
        captured.append(cmd)
        return (0, "", "")

    with patch("drive_sync.sync_engine._run", fake_run):
        await engine.bisync_folder(folder)

    cmd = _bisync_calls(captured)[0]
    assert "--exclude" in cmd
    assert "node_modules/**" in cmd


async def test_auto_exclude_false_skips_presets(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    engine = RcloneEngine(_app())
    folder = _folder(local_path=tmp_path / "code", auto_exclude=False)
    captured: list[list[str]] = []

    async def fake_run(cmd):
        captured.append(cmd)
        return (0, "", "")

    with patch("drive_sync.sync_engine._run", fake_run):
        await engine.bisync_folder(folder)

    cmd = _bisync_calls(captured)[0]
    assert "node_modules/**" not in cmd


# ---------------------------------------------------------------------------
# _ensure_remote_dir — mkdir é chamado antes do bisync
# ---------------------------------------------------------------------------

async def test_mkdir_called_before_bisync(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    engine = RcloneEngine(_app())
    folder = _folder(local_path=tmp_path / "local")
    captured: list[list[str]] = []

    async def fake_run(cmd):
        captured.append(cmd)
        return (0, "", "")

    with patch("drive_sync.sync_engine._run", fake_run):
        await engine.bisync_folder(folder)

    assert len(_mkdir_calls(captured)) == 1
    assert captured.index(_mkdir_calls(captured)[0]) < captured.index(_bisync_calls(captured)[0])


async def test_mkdir_failure_aborts_bisync(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    engine = RcloneEngine(_app())
    folder = _folder(local_path=tmp_path / "local")
    captured: list[list[str]] = []

    async def fake_run(cmd):
        captured.append(cmd)
        if "mkdir" in cmd:
            return (1, "", "rclone: mkdir failed")
        return (0, "", "")

    with patch("drive_sync.sync_engine._run", fake_run):
        result = await engine.bisync_folder(folder)

    assert result is False
    assert len(_bisync_calls(captured)) == 0


async def test_mkdir_uses_correct_remote(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    app = _app(remote_name="proton", remote_root="Sync")
    engine = RcloneEngine(app)
    folder = _folder(remote_subpath="dev/projects", local_path=tmp_path / "local")
    captured: list[list[str]] = []

    async def fake_run(cmd):
        captured.append(cmd)
        return (0, "", "")

    with patch("drive_sync.sync_engine._run", fake_run):
        await engine.bisync_folder(folder)

    mkdir_cmd = _mkdir_calls(captured)[0]
    assert "proton:Sync/dev/projects" in mkdir_cmd


# ---------------------------------------------------------------------------
# _run — serialização de chamadas concorrentes (ADR-001, rclone#7381)
# ---------------------------------------------------------------------------

async def test_run_serializes_concurrent_calls(monkeypatch):
    """Duas chamadas concorrentes a _run não se sobrepõem temporalmente."""
    intervals: list[tuple[float, float]] = []

    class _FakeProc:
        returncode = 0

        async def communicate(self):
            start = time.perf_counter()
            await asyncio.sleep(0.05)
            end = time.perf_counter()
            intervals.append((start, end))
            return (b"", b"")

    async def fake_subprocess_exec(*args, **kwargs):
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess_exec)

    await asyncio.gather(_run(["rclone", "one"]), _run(["rclone", "two"]))

    assert len(intervals) == 2
    (start1, end1), (start2, end2) = sorted(intervals)
    assert start2 >= end1 - 0.010, (
        f"Chamadas concorrentes se sobrepõem: "
        f"call1=({start1:.4f},{end1:.4f}), call2=({start2:.4f},{end2:.4f})"
    )


async def test_run_releases_lock_on_subprocess_exception(monkeypatch):
    """Exceção dentro do `async with` libera o lock — chamada seguinte não trava."""
    call_count = {"n": 0}

    async def fake_subprocess_exec(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise OSError("rclone binary missing")

        class _FakeProc:
            returncode = 0

            async def communicate(self):
                return (b"", b"")

        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess_exec)

    with pytest.raises(OSError):
        await _run(["rclone", "first"])

    rc, _out, _err = await asyncio.wait_for(_run(["rclone", "second"]), timeout=1.0)
    assert rc == 0


# ---------------------------------------------------------------------------
# _classify_rclone_stderr — detecção de falha de auth conhecida
# ---------------------------------------------------------------------------

# Amostras reais do journal de 2026-05-11 — preservar literais.
_STDERR_8002 = (
    'CRITICAL: Failed to create file system for "proton:Sync/dev/projects": '
    "couldn't initialize a new proton drive instance: 422 POST "
    "https://mail.proton.me/api/auth/v4/2fa: Incorrect login credentials. "
    "Please try again. (Code=8002, Status=422)"
)
_STDERR_9001 = (
    'CRITICAL: Failed to create file system for "proton:Sync/library": '
    "couldn't initialize a new proton drive instance: 422 POST "
    "https://mail.proton.me/api/auth/v4: For security reasons, please complete "
    "CAPTCHA. (Code=9001, Status=422)"
)


def test_classify_invalid_credentials_8002():
    err = _classify_rclone_stderr(_STDERR_8002)
    assert err is not None
    assert err.kind == "invalid_credentials"
    assert err.code == 8002
    assert "Code=8002" in err.stderr_tail


def test_classify_captcha_required_9001():
    err = _classify_rclone_stderr(_STDERR_9001)
    assert err is not None
    assert err.kind == "captcha_required"
    assert err.code == 9001


def test_classify_returns_none_for_non_auth_error():
    assert _classify_rclone_stderr("rclone: directory not found") is None


def test_classify_returns_none_for_other_status_code():
    # Code=10013 / Status=400 no /refresh — não é dos códigos alvo.
    stderr = (
        "POST https://mail.proton.me/api/auth/v4/refresh: Invalid refresh token "
        "(Code=10013, Status=400)"
    )
    assert _classify_rclone_stderr(stderr) is None


def test_classify_returns_none_without_full_anchor():
    # Substring "Code=8002" sem o "(...Status=422)" completo — não casa.
    assert _classify_rclone_stderr("logged Code=8002 somewhere /api/auth/v4") is None


def test_classify_returns_none_without_auth_endpoint():
    # Códigos batem mas o endpoint não é /api/auth/v4 — descarta.
    stderr = "POST https://api.proton.me/drive/v2/foo (Code=8002, Status=422)"
    assert _classify_rclone_stderr(stderr) is None


# ---------------------------------------------------------------------------
# _run levanta AuthDegradedError quando stderr matcha
# ---------------------------------------------------------------------------

async def test_run_raises_auth_degraded_on_matching_stderr(monkeypatch):
    class _FakeProc:
        returncode = 1

        async def communicate(self):
            return (b"", _STDERR_8002.encode("utf-8"))

    async def fake_exec(*args, **kwargs):
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(AuthDegradedError) as excinfo:
        await _run(["rclone", "mkdir", "proton:Sync/dev/projects"])

    assert excinfo.value.code == 8002


async def test_run_does_not_raise_on_non_auth_failure(monkeypatch):
    class _FakeProc:
        returncode = 1

        async def communicate(self):
            return (b"", b"rclone: directory not found")

    async def fake_exec(*args, **kwargs):
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    rc, _out, err = await _run(["rclone", "lsd", "proton:nonexistent"])
    assert rc == 1
    assert "not found" in err


# ---------------------------------------------------------------------------
# AuthDegradedError propaga via _ensure_remote_dir e bisync_folder
# ---------------------------------------------------------------------------

async def test_bisync_folder_propagates_auth_error(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    engine = RcloneEngine(_app())
    folder = _folder(local_path=tmp_path / "local")

    async def fake_run(cmd):
        raise AuthDegradedError(kind="invalid_credentials", code=8002, stderr_tail="...")

    with patch("drive_sync.sync_engine._run", fake_run):
        with pytest.raises(AuthDegradedError):
            await engine.bisync_folder(folder)


# ---------------------------------------------------------------------------
# auth_probe — propaga AuthDegradedError, silencia outros erros
# ---------------------------------------------------------------------------

async def test_auth_probe_propagates_auth_error():
    engine = RcloneEngine(_app())

    async def fake_run(cmd):
        raise AuthDegradedError(kind="captcha_required", code=9001, stderr_tail="...")

    with patch("drive_sync.sync_engine._run", fake_run):
        with pytest.raises(AuthDegradedError):
            await engine.auth_probe()


async def test_auth_probe_silences_non_auth_error():
    engine = RcloneEngine(_app())

    async def fake_run(cmd):
        raise OSError("network unreachable")

    with patch("drive_sync.sync_engine._run", fake_run):
        await engine.auth_probe()  # não deve levantar
