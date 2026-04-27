"""Tests for sync_engine — remote URI building and bisync behaviour."""
from pathlib import Path
from unittest.mock import patch

import pytest

from drive_sync.config import (
    AppConfig,
    DedupeConfig,
    FolderConfig,
    GitConfig,
    LoggingConfig,
    RcloneConfig,
    WatcherConfig,
)
from drive_sync.sync_engine import RcloneEngine, _state_marker_for, remote_uri_for


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
