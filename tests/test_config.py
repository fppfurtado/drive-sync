"""Tests for config loading and validation."""
import textwrap
from pathlib import Path

import pytest

from drive_sync.config import AppConfig, default_config_path, load_config


def _write_yaml(directory: Path, content: str) -> Path:
    path = directory / "config.yaml"
    path.write_text(textwrap.dedent(content))
    return path


# ---------------------------------------------------------------------------
# load_config — error cases
# ---------------------------------------------------------------------------

def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nonexistent.yaml")


def test_empty_folders_list_raises(tmp_path):
    cfg = _write_yaml(tmp_path, "folders: []")
    with pytest.raises(ValueError, match="folders"):
        load_config(cfg)


def test_missing_folders_key_raises(tmp_path):
    cfg = _write_yaml(tmp_path, "rclone:\n  remote_name: drive\n")
    with pytest.raises(ValueError, match="folders"):
        load_config(cfg)


def test_duplicate_folder_names_raises(tmp_path):
    cfg = _write_yaml(tmp_path, """
        folders:
          - name: docs
            local_path: /tmp/a
            remote_subpath: A
          - name: docs
            local_path: /tmp/b
            remote_subpath: B
    """)
    with pytest.raises(ValueError, match="duplicado"):
        load_config(cfg)


def test_invalid_git_mode_raises(tmp_path):
    cfg = _write_yaml(tmp_path, """
        folders:
          - name: code
            local_path: /tmp/code
            remote_subpath: Code
            git_mode: invalid_value
    """)
    with pytest.raises(ValueError, match="git_mode"):
        load_config(cfg)


# ---------------------------------------------------------------------------
# load_config — successful parsing
# ---------------------------------------------------------------------------

def test_valid_minimal_config(tmp_path):
    cfg = _write_yaml(tmp_path, """
        folders:
          - name: docs
            local_path: /tmp/docs
            remote_subpath: Documents
    """)
    result = load_config(cfg)
    assert isinstance(result, AppConfig)
    assert len(result.folders) == 1
    assert result.folders[0].name == "docs"


def test_folder_defaults(tmp_path):
    cfg = _write_yaml(tmp_path, """
        folders:
          - name: docs
            local_path: /tmp/docs
            remote_subpath: Documents
    """)
    folder = load_config(cfg).folders[0]
    assert folder.enabled is True
    assert folder.git_mode == "bisync"
    assert folder.auto_exclude is True
    assert folder.debounce_seconds == 5
    assert folder.exclude == []


def test_rclone_defaults(tmp_path):
    cfg = _write_yaml(tmp_path, """
        folders:
          - name: docs
            local_path: /tmp/docs
            remote_subpath: Documents
    """)
    rclone = load_config(cfg).rclone
    assert rclone.remote_name == "drive"
    assert rclone.remote_root == "Sync"
    assert rclone.binary == "rclone"
    assert rclone.global_flags == []


def test_rclone_section_override(tmp_path):
    cfg = _write_yaml(tmp_path, """
        rclone:
          remote_name: proton
          remote_root: MyRoot
          global_flags:
            - "--transfers=4"
        folders:
          - name: docs
            local_path: /tmp/docs
            remote_subpath: Documents
    """)
    rclone = load_config(cfg).rclone
    assert rclone.remote_name == "proton"
    assert rclone.remote_root == "MyRoot"
    assert "--transfers=4" in rclone.global_flags


def test_multiple_folders_loaded(tmp_path):
    cfg = _write_yaml(tmp_path, """
        folders:
          - name: docs
            local_path: /tmp/docs
            remote_subpath: Documents
          - name: photos
            local_path: /tmp/photos
            remote_subpath: Photos
            git_mode: "off"
            enabled: false
    """)
    result = load_config(cfg)
    assert len(result.folders) == 2
    photos = result.folders[1]
    assert photos.git_mode == "off"
    assert photos.enabled is False


def test_all_git_modes_accepted(tmp_path):
    for mode in ("off", "bisync", "bundle"):
        cfg = _write_yaml(tmp_path, f"""
            folders:
              - name: repo
                local_path: /tmp/repo
                remote_subpath: repo
                git_mode: "{mode}"
        """)
        folder = load_config(cfg).folders[0]
        assert folder.git_mode == mode


def test_tilde_expanded_in_local_path(tmp_path):
    cfg = _write_yaml(tmp_path, """
        folders:
          - name: home
            local_path: ~/Documents
            remote_subpath: Documents
    """)
    folder = load_config(cfg).folders[0]
    assert not str(folder.local_path).startswith("~")
    assert folder.local_path.is_absolute()


def test_remote_subpath_strips_slashes(tmp_path):
    cfg = _write_yaml(tmp_path, """
        folders:
          - name: docs
            local_path: /tmp/docs
            remote_subpath: /Documents/
    """)
    assert load_config(cfg).folders[0].remote_subpath == "Documents"


def test_source_path_recorded(tmp_path):
    cfg_file = _write_yaml(tmp_path, """
        folders:
          - name: docs
            local_path: /tmp/docs
            remote_subpath: Documents
    """)
    result = load_config(cfg_file)
    assert result.source_path == cfg_file


# ---------------------------------------------------------------------------
# default_config_path
# ---------------------------------------------------------------------------

def test_default_config_path_respects_xdg(monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", "/custom/xdg")
    assert str(default_config_path()) == "/custom/xdg/drive-sync/config.yaml"


def test_default_config_path_fallback(monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    path = default_config_path()
    assert path.name == "config.yaml"
    assert "drive-sync" in str(path)
    assert ".config" in str(path)
