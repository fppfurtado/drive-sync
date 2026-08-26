"""Tests for status — render_status and helpers."""
import os
from pathlib import Path

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
from drive_sync.status import (
    HEADER,
    _marker_name,
    _sanitize_for_lst,
    render_status,
)


def _app(folders: list[FolderConfig], remote_name: str = "proton",
         remote_root: str = "Sync") -> AppConfig:
    return AppConfig(
        rclone=RcloneConfig(remote_name=remote_name, remote_root=remote_root),
        folders=folders,
        git=GitConfig(),
        watcher=WatcherConfig(),
        dedupe=DedupeConfig(),
        health_check=HealthCheckConfig(),
        logging=LoggingConfig(),
        coverage_audit=CoverageAuditConfig(),
        source_path=Path("/fake/config.yaml"),
    )


def _folder(name: str, local: Path, sub: str = "docs",
            enabled: bool = True) -> FolderConfig:
    return FolderConfig(
        name=name, local_path=local, remote_subpath=sub, enabled=enabled,
    )


def _lst_name(local: Path, remote: str, side: int = 1) -> str:
    return (
        f"{_sanitize_for_lst(str(local))}.."
        f"{_sanitize_for_lst(remote)}.path{side}.lst"
    )


# ---------- caminho-feliz: .lst recente reflete em "Last sync" ----------

def test_lst_mtime_appears_as_last_sync(tmp_path: Path):
    bisync = tmp_path / "bisync"
    bisync.mkdir()
    local = Path("/tmp/docs")
    remote = "proton:Sync/docs"
    lst = bisync / _lst_name(local, remote)
    lst.write_text("")
    os.utime(lst, (1_700_000_000, 1_700_000_000))

    out = render_status(_app([_folder("docs", local, "docs")]), bisync)

    # mtime 1700000000 = 2023-11-14 22:13 UTC (asserir só ano-mês:
    # `_format_mtime` usa local time e o dia muda em fusos extremos).
    assert "2023-11" in out


# ---------- ausência de .lst: Last sync = "never" ----------

def test_no_lst_means_never(tmp_path: Path):
    bisync = tmp_path / "bisync"
    bisync.mkdir()
    out = render_status(_app([_folder("docs", Path("/tmp/docs"))]), bisync)
    assert "never" in out


# ---------- presença/ausência do marker .initialized ----------

def _row_for(out: str, folder_path: str) -> str:
    for line in out.splitlines():
        if folder_path in line:
            return line
    raise AssertionError(f"folder {folder_path!r} not found in:\n{out}")


def test_marker_present_means_initialized_yes(tmp_path: Path):
    bisync = tmp_path / "bisync"
    bisync.mkdir()
    local = Path("/tmp/docs")
    remote = "proton:Sync/docs"
    (bisync / _marker_name(local, remote)).write_text("")
    out = render_status(_app([_folder("docs", local, "docs")]), bisync)
    row = _row_for(out, "/tmp/docs")
    assert " yes " in row
    assert " no " not in row


def test_marker_absent_means_initialized_no(tmp_path: Path):
    bisync = tmp_path / "bisync"
    bisync.mkdir()
    out = render_status(_app([_folder("docs", Path("/tmp/docs"))]), bisync)
    row = _row_for(out, "/tmp/docs")
    assert " no " in row
    assert " yes " not in row


# ---------- bisync_dir inexistente não crasha ----------

def test_missing_bisync_dir_does_not_crash(tmp_path: Path):
    out = render_status(_app([_folder("docs", Path("/tmp/docs"))]),
                        tmp_path / "missing")
    assert "never" in out
    assert " no " in out


# ---------- match com caracteres especiais (espaço no path) ----------

def test_match_with_spaces_in_path(tmp_path: Path):
    bisync = tmp_path / "bisync"
    bisync.mkdir()
    local = Path("/storage/3. Resources/Projects")
    remote = "proton:Sync/dev/projects"
    lst = bisync / _lst_name(local, remote)
    lst.write_text("")
    os.utime(lst, (1_700_000_000, 1_700_000_000))

    out = render_status(_app([_folder("projects", local, "dev/projects")]),
                        bisync)
    # Forma real conferida no smoke contra config de produção:
    # "_storage_3._Resources_Projects.._proton_Sync_dev_projects.path1.lst".
    assert "2023-11" in out, out


# ---------- header de instabilidade na 1ª linha ----------

def test_first_line_is_instability_header(tmp_path: Path):
    bisync = tmp_path / "bisync"
    bisync.mkdir()
    out = render_status(_app([_folder("docs", Path("/tmp/docs"))]), bisync)
    assert out.splitlines()[0] == HEADER
    assert out.splitlines()[0].startswith("# drive-sync status v1")


# ---------- mais recente entre path1 e path2 vence ----------

def test_picks_most_recent_lst_when_path1_and_path2_differ(tmp_path: Path):
    bisync = tmp_path / "bisync"
    bisync.mkdir()
    local = Path("/tmp/docs")
    remote = "proton:Sync/docs"
    old = bisync / _lst_name(local, remote, side=1)
    new = bisync / _lst_name(local, remote, side=2)
    old.write_text("")
    new.write_text("")
    os.utime(old, (1_600_000_000, 1_600_000_000))  # 2020-09
    os.utime(new, (1_700_000_000, 1_700_000_000))  # 2023-11

    out = render_status(_app([_folder("docs", local, "docs")]), bisync)
    assert "2023-11" in out
    assert "2020-09" not in out


# ---------- marker presente sem .lst (estado pós-resync sem listing) ----------

def test_marker_present_without_lst(tmp_path: Path):
    bisync = tmp_path / "bisync"
    bisync.mkdir()
    local = Path("/tmp/docs")
    remote = "proton:Sync/docs"
    (bisync / _marker_name(local, remote)).write_text("")

    out = render_status(_app([_folder("docs", local, "docs")]), bisync)
    row = _row_for(out, "/tmp/docs")
    assert " yes " in row
    assert "never" in row


# ---------- pastas disabled ficam de fora ----------

def test_disabled_folder_is_omitted(tmp_path: Path):
    bisync = tmp_path / "bisync"
    bisync.mkdir()
    out = render_status(
        _app([
            _folder("docs", Path("/tmp/docs")),
            _folder("hidden", Path("/tmp/hidden"), enabled=False),
        ]),
        bisync,
    )
    assert "/tmp/docs" in out
    assert "/tmp/hidden" not in out


# ---------- success marker (preferência sobre bisync) ----------

def test_success_marker_overrides_bisync_marker_for_initialized_and_last_sync(
    tmp_path: Path, monkeypatch
):
    """Per ADR de --status fallback: success marker é fonte primária; cobre auto/bundle."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    bisync = tmp_path / "rclone" / "bisync"
    bisync.mkdir(parents=True)
    state = tmp_path / "drive-sync" / "state"
    state.mkdir(parents=True)
    # Folder sem marker bisync MAS com success marker.
    (state / "scripts.success").touch()
    folders = [_folder("scripts", Path("/tmp/scripts"), "dev/scripts")]
    out = render_status(_app(folders), bisync)
    row = _row_for(out, "/tmp/scripts")
    assert " yes " in row
    assert "never" not in row  # mtime do success marker é recente


def test_no_marker_at_all_shows_never(tmp_path: Path, monkeypatch):
    """Sem nenhum sinal (bisync nem success) → no/never."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    bisync = tmp_path / "rclone" / "bisync"
    bisync.mkdir(parents=True)
    out = render_status(
        _app([_folder("orphan", Path("/tmp/orphan"))]),
        bisync,
    )
    row = _row_for(out, "/tmp/orphan")
    assert " no " in row
    assert "never" in row
