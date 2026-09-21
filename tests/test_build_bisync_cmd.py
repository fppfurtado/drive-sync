"""Equivalência do builder extraído de `bisync_folder` (#94).

`build_bisync_cmd` foi extraído para que o diagnóstico `--dry-run-resync` rode
com EXATAMENTE os flags que o daemon usaria — transcrevê-los à mão daria um
veredito que não corresponde ao que o daemon faz (o gap que #94 cura).

O extract é refactor puro; estes testes pinam isso: o cmd que `bisync_folder`
efetivamente executa é o do builder (mais `--resync` apenas no first-run).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

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
from drive_sync.sync_engine import RcloneEngine, _state_marker_for, remote_uri_for


def _app(max_lock_seconds: int = 3600) -> AppConfig:
    return AppConfig(
        rclone=RcloneConfig(
            remote_name="proton", remote_root="Sync", max_lock_seconds=max_lock_seconds
        ),
        folders=[],
        git=GitConfig(),
        watcher=WatcherConfig(),
        dedupe=DedupeConfig(),
        health_check=HealthCheckConfig(),
        logging=LoggingConfig(),
        coverage_audit=CoverageAuditConfig(),
        source_path=Path("/fake/config.yaml"),
    )


def _folder(tmp_path: Path, **kw) -> FolderConfig:
    kw.setdefault("auto_exclude", False)
    f = FolderConfig(
        name="t", local_path=tmp_path / "data", remote_subpath="t", **kw
    )
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    return f


async def _executed_cmd(app: AppConfig, folder: FolderConfig, first_run: bool):
    """Roda bisync_folder com _run stubado e devolve o cmd executado."""
    marker = _state_marker_for(folder.local_path, remote_uri_for(folder, app))
    marker.parent.mkdir(parents=True, exist_ok=True)
    if first_run:
        marker.unlink(missing_ok=True)
    else:
        marker.touch()

    captured: list[list[str]] = []

    async def fake_run(cmd, timeout=None):
        captured.append(cmd)
        return (0, "", "")

    with patch("drive_sync.sync_engine._run", fake_run):
        engine = RcloneEngine(app)
        await engine.bisync_folder(folder)
    # [0] é o mkdir do _ensure_remote_dir; o bisync é o último.
    return captured[-1]


@pytest.mark.asyncio
async def test_executed_cmd_matches_builder(tmp_path):
    app, folder = _app(), _folder(tmp_path)
    remote = remote_uri_for(folder, app)
    expected = RcloneEngine(app).build_bisync_cmd(folder, folder.local_path, remote)
    assert await _executed_cmd(app, folder, first_run=False) == expected


@pytest.mark.asyncio
async def test_first_run_appends_resync_and_nothing_else(tmp_path):
    app, folder = _app(), _folder(tmp_path)
    remote = remote_uri_for(folder, app)
    expected = RcloneEngine(app).build_bisync_cmd(folder, folder.local_path, remote)
    assert await _executed_cmd(app, folder, first_run=True) == expected + ["--resync"]


def test_builder_preserves_flag_order_and_excludes(tmp_path):
    """Pina a sequência: subcomando → conflito → empty-dirs → max-lock → excludes."""
    app = _app(max_lock_seconds=1800)
    folder = _folder(tmp_path, exclude=["**/.zotero/**", "*.tmp"])
    cmd = RcloneEngine(app).build_bisync_cmd(
        folder, folder.local_path, remote_uri_for(folder, app)
    )

    assert cmd[cmd.index("bisync") + 1] == str(folder.local_path)
    assert cmd[cmd.index("bisync") + 2] == "proton:Sync/t"
    assert cmd[cmd.index("--conflict-resolve") + 1] == "newer"
    assert cmd[cmd.index("--conflict-loser") + 1] == "delete"
    assert "--create-empty-src-dirs" in cmd
    assert cmd[cmd.index("--max-lock") + 1] == "1800s"
    # Excludes do usuário preservados, em ordem.
    pats = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--exclude"]
    assert pats == ["**/.zotero/**", "*.tmp"]
    # O builder NUNCA injeta --resync/--dry-run: quem decide é o chamador.
    assert "--resync" not in cmd
    assert "--dry-run" not in cmd


def test_max_lock_zero_omits_flag(tmp_path):
    app = _app(max_lock_seconds=0)
    folder = _folder(tmp_path)
    cmd = RcloneEngine(app).build_bisync_cmd(
        folder, folder.local_path, remote_uri_for(folder, app)
    )
    assert "--max-lock" not in cmd


def test_auto_exclude_adds_presets_after_user_patterns(tmp_path):
    app = _app()
    folder = _folder(tmp_path, auto_exclude=True, exclude=["meu-proprio/**"])
    cmd = RcloneEngine(app).build_bisync_cmd(
        folder, folder.local_path, remote_uri_for(folder, app)
    )
    pats = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--exclude"]
    assert pats[0] == "meu-proprio/**"
    assert "__pycache__/**" in pats
    assert len(pats) == len(set(pats)), "excludes duplicados"
