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


def test_invalid_git_handling_raises(tmp_path):
    cfg = _write_yaml(tmp_path, """
        folders:
          - name: code
            local_path: /tmp/code
            remote_subpath: Code
            git_handling: invalid_value
    """)
    with pytest.raises(ValueError, match="git_handling"):
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
    assert folder.git_handling == "auto"
    assert folder.auto_exclude is True
    assert folder.debounce_seconds == 5
    assert folder.exclude == []
    assert folder.repo_overrides == []


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
    # SP-T3 (#47/ADR-019): auto-recuperação de rc=7 stale-listings ON por default.
    assert rclone.auto_resync_stale_listings is True


def test_auto_resync_stale_listings_opt_out(tmp_path):
    # WHERE auto_resync_stale_listings: false → knob respeitado (comportamento legado).
    cfg = _write_yaml(tmp_path, """
        rclone:
          auto_resync_stale_listings: false
        folders:
          - name: docs
            local_path: /tmp/docs
            remote_subpath: Documents
    """)
    assert load_config(cfg).rclone.auto_resync_stale_listings is False


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
            git_handling: "plain"
            enabled: false
    """)
    result = load_config(cfg)
    assert len(result.folders) == 2
    photos = result.folders[1]
    assert photos.git_handling == "plain"
    assert photos.enabled is False


def test_all_git_handlings_accepted(tmp_path):
    for handling in ("auto", "skip", "bundle", "plain"):
        cfg = _write_yaml(tmp_path, f"""
            folders:
              - name: repo
                local_path: /tmp/repo
                remote_subpath: repo
                git_handling: "{handling}"
        """)
        folder = load_config(cfg).folders[0]
        assert folder.git_handling == handling


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


# ---------------------------------------------------------------------------
# health_check — defaults e parsing
# ---------------------------------------------------------------------------

def test_health_check_defaults_when_section_absent(tmp_path):
    cfg = _write_yaml(tmp_path, """
        folders:
          - name: x
            local_path: /tmp/x
            remote_subpath: X
    """)
    app = load_config(cfg)
    assert app.health_check.enabled is True
    assert app.health_check.interval_seconds == 3600


def test_health_check_disabled(tmp_path):
    cfg = _write_yaml(tmp_path, """
        folders:
          - name: x
            local_path: /tmp/x
            remote_subpath: X
        health_check:
          enabled: false
    """)
    app = load_config(cfg)
    assert app.health_check.enabled is False


def test_health_check_custom_interval(tmp_path):
    cfg = _write_yaml(tmp_path, """
        folders:
          - name: x
            local_path: /tmp/x
            remote_subpath: X
        health_check:
          interval_seconds: 60
    """)
    app = load_config(cfg)
    assert app.health_check.interval_seconds == 60


# ---------------------------------------------------------------------------
# subpath_overrides (ADR-006 + ADR-008) — expansão em FolderConfig synthetic
# ---------------------------------------------------------------------------

def test_fs_key_defaults_to_name_for_plain_folder(tmp_path):
    cfg = _write_yaml(tmp_path, """
        folders:
          - name: dev-projects
            local_path: /tmp/projects
            remote_subpath: dev/projects
    """)
    app = load_config(cfg)
    assert app.folders[0].fs_key == "dev-projects"


def test_subpath_override_expands_into_synthetic_folder(tmp_path):
    cfg = _write_yaml(tmp_path, """
        folders:
          - name: dev-projects
            local_path: /tmp/projects
            remote_subpath: dev/projects
            cooldown_seconds: 3600
            debounce_seconds: 40
            auto_exclude: false
            subpath_overrides:
              - subpath: tjpa/pje-2.1
                git_handling: bundle
    """)
    app = load_config(cfg)
    assert len(app.folders) == 2

    parent, synthetic = app.folders
    assert parent.name == "dev-projects"
    assert parent.git_handling == "auto"  # default preservado, override só afeta synthetic
    assert synthetic.name == "dev-projects/tjpa/pje-2.1"
    assert synthetic.fs_key == "dev-projects-tjpa-pje-2.1"
    assert synthetic.git_handling == "bundle"
    assert synthetic.local_path == Path("/tmp/projects/tjpa/pje-2.1")
    assert synthetic.remote_subpath == "dev/projects/tjpa/pje-2.1"
    # Demais campos herdam do parent.
    assert synthetic.cooldown_seconds == 3600
    assert synthetic.debounce_seconds == 40
    assert synthetic.auto_exclude is False
    assert synthetic.enabled is True
    # Não-recursivo.
    assert synthetic.subpath_overrides == []
    assert synthetic.repo_overrides == []


def test_subpath_override_injects_glob_in_parent_exclude(tmp_path):
    cfg = _write_yaml(tmp_path, """
        folders:
          - name: dev-projects
            local_path: /tmp/projects
            remote_subpath: dev/projects
            subpath_overrides:
              - subpath: tjpa/pje-2.1
                git_handling: bundle
    """)
    app = load_config(cfg)
    parent = app.folders[0]
    assert "tjpa/pje-2.1/**" in parent.exclude


def test_subpath_override_redundant_exclude_warns(tmp_path, caplog):
    """Operador declara exclude E subpath_overrides — loader não duplica, emite WARNING."""
    cfg = _write_yaml(tmp_path, """
        folders:
          - name: dev-projects
            local_path: /tmp/projects
            remote_subpath: dev/projects
            exclude:
              - "tjpa/pje-2.1/**"
            subpath_overrides:
              - subpath: tjpa/pje-2.1
                git_handling: bundle
    """)
    with caplog.at_level("WARNING", logger="drive_sync.config"):
        app = load_config(cfg)
    parent = app.folders[0]
    assert parent.exclude.count("tjpa/pje-2.1/**") == 1
    assert any(
        "exclude redundante" in r.message and "tjpa/pje-2.1/**" in r.message
        for r in caplog.records
    )


def test_two_overrides_expand_in_order(tmp_path):
    cfg = _write_yaml(tmp_path, """
        folders:
          - name: dev-projects
            local_path: /tmp/projects
            remote_subpath: dev/projects
            subpath_overrides:
              - subpath: a/repo-x
                git_handling: bundle
              - subpath: b/repo-y
                git_handling: "plain"
    """)
    app = load_config(cfg)
    assert [f.name for f in app.folders] == [
        "dev-projects",
        "dev-projects/a/repo-x",
        "dev-projects/b/repo-y",
    ]
    parent = app.folders[0]
    assert "a/repo-x/**" in parent.exclude
    assert "b/repo-y/**" in parent.exclude


def test_synthetic_inherits_parent_user_excludes(tmp_path):
    """Synthetic clona exclude do parent ANTES do auto-inject do glob (não inclui self)."""
    cfg = _write_yaml(tmp_path, """
        folders:
          - name: dev-projects
            local_path: /tmp/projects
            remote_subpath: dev/projects
            exclude:
              - "*.local"
            subpath_overrides:
              - subpath: sub
                git_handling: bundle
    """)
    app = load_config(cfg)
    synthetic = app.folders[1]
    assert "*.local" in synthetic.exclude
    assert "sub/**" not in synthetic.exclude


def test_subpath_empty_raises(tmp_path):
    cfg = _write_yaml(tmp_path, """
        folders:
          - name: x
            local_path: /tmp/x
            remote_subpath: X
            subpath_overrides:
              - subpath: ""
                git_handling: bundle
    """)
    with pytest.raises(ValueError, match="subpath vazio"):
        load_config(cfg)


def test_subpath_absolute_raises(tmp_path):
    cfg = _write_yaml(tmp_path, """
        folders:
          - name: x
            local_path: /tmp/x
            remote_subpath: X
            subpath_overrides:
              - subpath: /absolute
                git_handling: bundle
    """)
    with pytest.raises(ValueError, match="absoluto"):
        load_config(cfg)


def test_subpath_with_dotdot_raises(tmp_path):
    cfg = _write_yaml(tmp_path, """
        folders:
          - name: x
            local_path: /tmp/x
            remote_subpath: X
            subpath_overrides:
              - subpath: a/../b
                git_handling: bundle
    """)
    with pytest.raises(ValueError, match=r"\.\."):
        load_config(cfg)


def test_override_invalid_git_handling_raises(tmp_path):
    cfg = _write_yaml(tmp_path, """
        folders:
          - name: x
            local_path: /tmp/x
            remote_subpath: X
            subpath_overrides:
              - subpath: sub
                git_handling: bogus
    """)
    with pytest.raises(ValueError, match="bogus"):
        load_config(cfg)


def test_duplicate_subpath_in_same_parent_raises(tmp_path):
    cfg = _write_yaml(tmp_path, """
        folders:
          - name: x
            local_path: /tmp/x
            remote_subpath: X
            subpath_overrides:
              - subpath: sub
                git_handling: bundle
              - subpath: sub
                git_handling: "plain"
    """)
    with pytest.raises(ValueError, match="duplicado"):
        load_config(cfg)


def test_synthetic_name_collision_when_other_folder_declared_first(tmp_path):
    """Folder declarado primeiro → expansão de synthetic colide ('colide')."""
    cfg = _write_yaml(tmp_path, """
        folders:
          - name: parent/collision
            local_path: /tmp/other
            remote_subpath: O
          - name: parent
            local_path: /tmp/parent
            remote_subpath: P
            subpath_overrides:
              - subpath: collision
                git_handling: bundle
    """)
    with pytest.raises(ValueError, match="colide"):
        load_config(cfg)


def test_synthetic_name_collision_when_synthetic_added_first(tmp_path):
    """Synthetic adicionado primeiro → segundo folder cai no check de duplicado ('duplicado')."""
    cfg = _write_yaml(tmp_path, """
        folders:
          - name: parent
            local_path: /tmp/parent
            remote_subpath: P
            subpath_overrides:
              - subpath: collision
                git_handling: bundle
          - name: parent/collision
            local_path: /tmp/other
            remote_subpath: O
    """)
    with pytest.raises(ValueError, match="duplicado"):
        load_config(cfg)


def test_synthetic_inherits_disabled_from_parent(tmp_path):
    """ADR-006: enabled herda do parent — desabilitar parent desabilita synthetic."""
    cfg = _write_yaml(tmp_path, """
        folders:
          - name: parent
            local_path: /tmp/parent
            remote_subpath: P
            enabled: false
            subpath_overrides:
              - subpath: sub
                git_handling: bundle
    """)
    app = load_config(cfg)
    assert app.folders[0].enabled is False
    assert app.folders[1].enabled is False


def test_override_missing_git_handling_raises(tmp_path):
    """Chave git_handling omitida no override → ValueError."""
    cfg = _write_yaml(tmp_path, """
        folders:
          - name: x
            local_path: /tmp/x
            remote_subpath: X
            subpath_overrides:
              - subpath: sub
    """)
    with pytest.raises(ValueError, match="git_handling"):
        load_config(cfg)


def test_overlapping_subpaths_in_same_parent_raises(tmp_path):
    """ADR-006: dois subpaths sobrepostos no mesmo parent não são tolerados."""
    cfg = _write_yaml(tmp_path, """
        folders:
          - name: x
            local_path: /tmp/x
            remote_subpath: X
            subpath_overrides:
              - subpath: a
                git_handling: bundle
              - subpath: a/b
                git_handling: bundle
    """)
    with pytest.raises(ValueError, match="aninhado"):
        load_config(cfg)


def test_overlapping_subpaths_reverse_order_raises(tmp_path):
    """Ordem inversa do anterior — mais específico declarado antes."""
    cfg = _write_yaml(tmp_path, """
        folders:
          - name: x
            local_path: /tmp/x
            remote_subpath: X
            subpath_overrides:
              - subpath: a/b
                git_handling: bundle
              - subpath: a
                git_handling: bundle
    """)
    with pytest.raises(ValueError, match="aninhado"):
        load_config(cfg)


# ---------------------------------------------------------------------------
# ADR-016 — validação do detector de storm (F1 do Review)
# ---------------------------------------------------------------------------

def test_infra_storm_threshold_below_one_raises(tmp_path):
    cfg = _write_yaml(tmp_path, """
        rclone:
          infra_storm_threshold: 0
        folders:
          - name: docs
            local_path: /tmp/docs
            remote_subpath: Documents
    """)
    with pytest.raises(ValueError, match="infra_storm_threshold"):
        load_config(cfg)


def test_infra_window_seconds_non_positive_raises(tmp_path):
    cfg = _write_yaml(tmp_path, """
        rclone:
          infra_window_seconds: 0
        folders:
          - name: docs
            local_path: /tmp/docs
            remote_subpath: Documents
    """)
    with pytest.raises(ValueError, match="infra_window_seconds"):
        load_config(cfg)
