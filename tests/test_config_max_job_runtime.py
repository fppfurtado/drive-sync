"""Tests for max_job_runtime_seconds config parse + validation (#45)."""
import textwrap
from pathlib import Path

import pytest

from drive_sync.config import load_config


def _write_yaml(directory: Path, content: str) -> Path:
    path = directory / "config.yaml"
    path.write_text(textwrap.dedent(content))
    return path


def _base_folder() -> str:
    return """
        folders:
          - name: docs
            local_path: /tmp/docs
            remote_subpath: Documents
    """


# ---- global (RcloneConfig) ----

def test_global_default_is_7200(tmp_path):
    app = load_config(_write_yaml(tmp_path, _base_folder()))
    assert app.rclone.max_job_runtime_seconds == 7200


def test_global_explicit_parsed(tmp_path):
    app = load_config(_write_yaml(tmp_path, _base_folder() + """
        rclone:
          max_job_runtime_seconds: 3600
    """))
    assert app.rclone.max_job_runtime_seconds == 3600


def test_global_zero_disables(tmp_path):
    app = load_config(_write_yaml(tmp_path, _base_folder() + """
        rclone:
          max_job_runtime_seconds: 0
    """))
    assert app.rclone.max_job_runtime_seconds == 0


def test_global_negative_rejected(tmp_path):
    cfg = _write_yaml(tmp_path, _base_folder() + """
        rclone:
          max_job_runtime_seconds: -1
    """)
    with pytest.raises(ValueError, match=r"max_job_runtime_seconds.*>= 0"):
        load_config(cfg)


# ---- per-folder (FolderConfig) override ----

def test_folder_absent_inherits_global_via_none(tmp_path):
    app = load_config(_write_yaml(tmp_path, _base_folder()))
    assert app.folders[0].max_job_runtime_seconds is None


def test_folder_override_parsed(tmp_path):
    app = load_config(_write_yaml(tmp_path, """
        folders:
          - name: archive
            local_path: /tmp/archive
            remote_subpath: Archive
            max_job_runtime_seconds: 21600
    """))
    assert app.folders[0].max_job_runtime_seconds == 21600


def test_folder_zero_disables_for_folder(tmp_path):
    app = load_config(_write_yaml(tmp_path, """
        folders:
          - name: archive
            local_path: /tmp/archive
            remote_subpath: Archive
            max_job_runtime_seconds: 0
    """))
    assert app.folders[0].max_job_runtime_seconds == 0


def test_folder_negative_rejected(tmp_path):
    cfg = _write_yaml(tmp_path, """
        folders:
          - name: archive
            local_path: /tmp/archive
            remote_subpath: Archive
            max_job_runtime_seconds: -5
    """)
    with pytest.raises(ValueError, match=r"max_job_runtime_seconds.*archive.*>= 0"):
        load_config(cfg)


def test_synthetic_subpath_inherits_parent_max_job_runtime(tmp_path):
    app = load_config(_write_yaml(tmp_path, """
        folders:
          - name: docs
            local_path: /tmp/docs
            remote_subpath: Documents
            max_job_runtime_seconds: 1234
            subpath_overrides:
              - subpath: sub
                git_handling: plain
    """))
    synthetic = next(f for f in app.folders if f.name == "docs/sub")
    assert synthetic.max_job_runtime_seconds == 1234
