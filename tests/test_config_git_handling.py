"""Tests for git_handling schema migration (ADR-008)."""
import textwrap
from pathlib import Path

import pytest

from drive_sync.config import load_config


def _write_yaml(directory: Path, content: str) -> Path:
    path = directory / "config.yaml"
    path.write_text(textwrap.dedent(content))
    return path


# ---------------------------------------------------------------------------
# Falha-fast simétrica: git_mode legado rejeitado em qualquer valor
# ---------------------------------------------------------------------------

def test_git_mode_bisync_rejected_with_migration_hint(tmp_path):
    cfg = _write_yaml(tmp_path, """
        folders:
          - name: code
            local_path: /tmp/code
            remote_subpath: Code
            git_mode: bisync
    """)
    with pytest.raises(ValueError, match=r"git_mode.*removida.*ADR-008.*git_handling"):
        load_config(cfg)


def test_git_mode_bundle_rejected_with_migration_hint(tmp_path):
    cfg = _write_yaml(tmp_path, """
        folders:
          - name: code
            local_path: /tmp/code
            remote_subpath: Code
            git_mode: bundle
    """)
    with pytest.raises(ValueError, match=r"git_mode.*removida.*bundle"):
        load_config(cfg)


def test_git_mode_off_rejected_with_migration_hint(tmp_path):
    """Mapping 'off' → 'plain' aparece na mensagem de migração."""
    cfg = _write_yaml(tmp_path, """
        folders:
          - name: docs
            local_path: /tmp/docs
            remote_subpath: Docs
            git_mode: "off"
    """)
    with pytest.raises(ValueError, match=r"git_mode.*removida.*plain"):
        load_config(cfg)


# ---------------------------------------------------------------------------
# git_handling: plain semântica
# ---------------------------------------------------------------------------

def test_git_handling_plain_accepted_and_dispatches_like_legacy_off(tmp_path):
    """plain = 'bisync sem excludes git' (renomeio explícito do legacy 'off')."""
    cfg = _write_yaml(tmp_path, """
        folders:
          - name: docs
            local_path: /tmp/docs
            remote_subpath: Docs
            git_handling: "plain"
            auto_exclude: false
    """)
    app = load_config(cfg)
    folder = app.folders[0]
    assert folder.git_handling == "plain"
    assert folder.auto_exclude is False  # operador controla excludes manualmente


# ---------------------------------------------------------------------------
# Coexistência subpath_overrides + repo_overrides (ADR-008 §Coexistência)
# ---------------------------------------------------------------------------

def test_subpath_overrides_plus_auto_coexists_with_precedence(tmp_path):
    """ADR-008: ambos overrides coexistem; precedência aplicada em runtime."""
    cfg = _write_yaml(tmp_path, """
        folders:
          - name: dev
            local_path: /tmp/dev
            remote_subpath: dev
            git_handling: auto
            subpath_overrides:
              - subpath: tools
                git_handling: plain
            repo_overrides:
              - repo_subpath: legacy-fork
                mode: bundle
    """)
    app = load_config(cfg)
    parent = next(f for f in app.folders if f.name == "dev")
    assert parent.git_handling == "auto"
    assert len(parent.subpath_overrides) == 1
    assert len(parent.repo_overrides) == 1
    assert parent.repo_overrides[0].repo_subpath == "legacy-fork"
    assert parent.repo_overrides[0].mode == "bundle"


def test_subpath_overrides_and_repo_overrides_overlap_rejected(tmp_path):
    """Loader rejeita mesmo path em ambos repo_overrides e subpath_overrides."""
    cfg = _write_yaml(tmp_path, """
        folders:
          - name: dev
            local_path: /tmp/dev
            remote_subpath: dev
            git_handling: auto
            subpath_overrides:
              - subpath: shared/sub
                git_handling: bundle
            repo_overrides:
              - repo_subpath: shared/sub
                mode: skip
    """)
    with pytest.raises(ValueError, match=r"shared/sub.*sobreposição"):
        load_config(cfg)


# ---------------------------------------------------------------------------
# repo_overrides — validação de mode
# ---------------------------------------------------------------------------

def test_repo_overrides_invalid_mode_rejected(tmp_path):
    """mode aceita só 'skip'|'bundle' — 'plain' e 'auto' não fazem sentido em repo_overrides."""
    cfg = _write_yaml(tmp_path, """
        folders:
          - name: dev
            local_path: /tmp/dev
            remote_subpath: dev
            git_handling: auto
            repo_overrides:
              - repo_subpath: x
                mode: plain
    """)
    with pytest.raises(ValueError, match=r"mode.*'plain'.*inválido"):
        load_config(cfg)


def test_repo_overrides_duplicate_subpath_rejected(tmp_path):
    """Mesmo repo_subpath declarado duas vezes em repo_overrides → erro."""
    cfg = _write_yaml(tmp_path, """
        folders:
          - name: dev
            local_path: /tmp/dev
            remote_subpath: dev
            git_handling: auto
            repo_overrides:
              - repo_subpath: x
                mode: skip
              - repo_subpath: x
                mode: bundle
    """)
    with pytest.raises(ValueError, match=r"x.*duplicado"):
        load_config(cfg)
