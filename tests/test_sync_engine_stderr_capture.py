"""Tests for ADR-012 stderr capture helpers in sync_engine."""
from pathlib import Path

import pytest

from drive_sync.sync_engine import _capture_stderr, _slug, _stderr_dir


@pytest.fixture
def xdg_state(tmp_path, monkeypatch):
    """Isola _stderr_dir() em tmp_path via XDG_STATE_HOME."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    return tmp_path / "drive-sync"


def test_capture_writes_full_stderr_to_file(xdg_state):
    stderr = "line\n" * 800  # ~4kb, well over the 500-char tail
    _capture_stderr("bisync", "archive", stderr)
    out = (xdg_state / "last-stderr-bisync-archive.log").read_text()
    assert out == stderr


def test_capture_returns_first_error_line(xdg_state):
    stderr = (
        "2026/06/10 06:28:00 NOTICE: warmup\n"
        "2026/06/10 06:28:17 ERROR : Bisync critical error: 500 GET https://...\n"
        "2026/06/10 06:28:18 NOTICE: trailing\n"
    )
    summary, _ = _capture_stderr("bisync", "archive", stderr)
    assert summary == "2026/06/10 06:28:17 ERROR : Bisync critical error: 500 GET https://..."


def test_capture_fallback_to_tail_when_no_error_line(xdg_state):
    stderr = "INFO: nothing went wrong-ish\n" + "x" * 700
    summary, _ = _capture_stderr("bisync", "archive", stderr)
    assert summary == stderr.strip()[-500:]
    assert len(summary) == 500


def test_capture_overwrites_previous_run(xdg_state):
    _capture_stderr("bisync", "archive", "first run output")
    _capture_stderr("bisync", "archive", "second run output")
    out = (xdg_state / "last-stderr-bisync-archive.log").read_text()
    assert out == "second run output"


def test_capture_uses_xdg_state_home_when_set(tmp_path, monkeypatch):
    custom = tmp_path / "custom_state"
    monkeypatch.setenv("XDG_STATE_HOME", str(custom))
    _capture_stderr("bisync", "archive", "x")
    assert (custom / "drive-sync" / "last-stderr-bisync-archive.log").exists()


def test_capture_creates_directory_idempotent(xdg_state):
    assert not xdg_state.exists()
    _capture_stderr("bisync", "archive", "first")
    assert xdg_state.is_dir()
    _capture_stderr("bisync", "archive", "second")  # would raise if not idempotent
    assert xdg_state.is_dir()


def test_capture_handles_multiline_error_picks_first(xdg_state):
    stderr = (
        "NOTICE: warmup\n"
        "2026/06/10 06:28:17 ERROR : first error here\n"
        "2026/06/10 06:28:18 ERROR : second error\n"
        "2026/06/10 06:28:19 ERROR : third\n"
    )
    summary, _ = _capture_stderr("bisync", "archive", stderr)
    assert summary == "2026/06/10 06:28:17 ERROR : first error here"


def test_capture_filenames_distinguish_operations(xdg_state):
    _capture_stderr("bisync", "archive", "bisync stderr")
    _capture_stderr("mkdir", "archive", "mkdir stderr")
    bisync = (xdg_state / "last-stderr-bisync-archive.log").read_text()
    mkdir = (xdg_state / "last-stderr-mkdir-archive.log").read_text()
    assert bisync == "bisync stderr"
    assert mkdir == "mkdir stderr"


def test_capture_includes_rel_subpath_for_bundle_operations(xdg_state):
    _capture_stderr("upload-bundle", "tjpa-pje-2.1", "repo-a stderr", sub="h3/repo-a")
    _capture_stderr("upload-bundle", "tjpa-pje-2.1", "repo-b stderr", sub="h3/repo-b")
    repo_a = (xdg_state / "last-stderr-upload-bundle-tjpa-pje-2.1-h3_repo-a.log").read_text()
    repo_b = (xdg_state / "last-stderr-upload-bundle-tjpa-pje-2.1-h3_repo-b.log").read_text()
    assert repo_a == "repo-a stderr"
    assert repo_b == "repo-b stderr"


def test_capture_sanitizes_folder_name_with_special_chars(xdg_state):
    _capture_stderr("bisync", "tjpa/pje 2.1", "x")
    expected = xdg_state / "last-stderr-bisync-tjpa_pje_2.1.log"
    assert expected.exists()


def test_capture_returns_file_path(xdg_state):
    _, path = _capture_stderr("bisync", "archive", "x")
    assert path == xdg_state / "last-stderr-bisync-archive.log"


def test_slug_replaces_unsafe_chars():
    assert _slug("simple") == "simple"
    assert _slug("dev/projects") == "dev_projects"
    assert _slug("tjpa pje-2.1") == "tjpa_pje-2.1"
    assert _slug("a/b/../c") == "a_b_.._c"  # `..` literal is preserved but cannot escape — concatenated inside fixed prefix


def test_stderr_dir_creates_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    p1 = _stderr_dir()
    p2 = _stderr_dir()
    assert p1 == p2 == tmp_path / "drive-sync"
    assert p1.is_dir()


def test_stderr_dir_falls_back_to_home_local_state_when_xdg_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    p = _stderr_dir()
    assert p == tmp_path / ".local" / "state" / "drive-sync"
    assert p.is_dir()
