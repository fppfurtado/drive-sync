"""Tests for ADR-010: validação config-time de auto_exclude contra markers de código."""
import textwrap
from pathlib import Path

import pytest

from drive_sync.config import load_config


def _write_yaml(directory: Path, content: str) -> Path:
    path = directory / "config.yaml"
    path.write_text(textwrap.dedent(content))
    return path


def _yaml_with_folder(local_path: Path, auto_exclude: bool, max_depth: int | None = None) -> str:
    depth_line = f"\ngit:\n  max_recursion_depth: {max_depth}" if max_depth is not None else ""
    return textwrap.dedent(f"""
        folders:
          - name: test
            local_path: {local_path}
            remote_subpath: test
            auto_exclude: {str(auto_exclude).lower()}
            git_handling: plain
    """) + depth_line


# ---------------------------------------------------------------------------
# Caminho feliz: auto_exclude=True skipa scan; clean path passa
# ---------------------------------------------------------------------------

def test_auto_exclude_true_skips_scan_even_with_code(tmp_path):
    """auto_exclude: true → validator skipa (fast-path); presence de .venv/ é ignorada."""
    (tmp_path / ".venv").mkdir()
    cfg = _write_yaml(tmp_path, _yaml_with_folder(tmp_path, auto_exclude=True))
    # Sem raise; folder carrega normalmente.
    app = load_config(cfg)
    assert app.folders[0].auto_exclude is True


def test_auto_exclude_false_clean_path_passes(tmp_path):
    """auto_exclude: false em path sem markers de código → carrega sem erro."""
    (tmp_path / "documents").mkdir()
    (tmp_path / "photos.jpg").touch()
    cfg = _write_yaml(tmp_path, _yaml_with_folder(tmp_path, auto_exclude=False))
    app = load_config(cfg)
    assert app.folders[0].auto_exclude is False


# ---------------------------------------------------------------------------
# Detecção dos 3 markers
# ---------------------------------------------------------------------------

def test_auto_exclude_false_detects_venv(tmp_path):
    (tmp_path / ".venv").mkdir()
    cfg = _write_yaml(tmp_path, _yaml_with_folder(tmp_path, auto_exclude=False))
    with pytest.raises(ValueError) as exc:
        load_config(cfg)
    assert ".venv" in str(exc.value)
    assert str(tmp_path / ".venv") in str(exc.value)


def test_auto_exclude_false_detects_node_modules(tmp_path):
    (tmp_path / "node_modules").mkdir()
    cfg = _write_yaml(tmp_path, _yaml_with_folder(tmp_path, auto_exclude=False))
    with pytest.raises(ValueError) as exc:
        load_config(cfg)
    assert "node_modules" in str(exc.value)


def test_auto_exclude_false_detects_target(tmp_path):
    """ADR-010 §Decisão (1): target/ (Rust) incluído por simetria + paridade com presets."""
    (tmp_path / "target").mkdir()
    cfg = _write_yaml(tmp_path, _yaml_with_folder(tmp_path, auto_exclude=False))
    with pytest.raises(ValueError) as exc:
        load_config(cfg)
    assert "target" in str(exc.value)


# ---------------------------------------------------------------------------
# Agregação + mensagem
# ---------------------------------------------------------------------------

def test_auto_exclude_false_aggregates_multiple_hits(tmp_path):
    """Hits em paths diferentes → mensagem lista todos."""
    (tmp_path / "proj-a").mkdir()
    (tmp_path / "proj-a" / ".venv").mkdir()
    (tmp_path / "proj-b").mkdir()
    (tmp_path / "proj-b" / "node_modules").mkdir()
    cfg = _write_yaml(tmp_path, _yaml_with_folder(tmp_path, auto_exclude=False))
    with pytest.raises(ValueError) as exc:
        load_config(cfg)
    msg = str(exc.value)
    assert ".venv" in msg
    assert "node_modules" in msg
    assert "proj-a" in msg
    assert "proj-b" in msg


def test_error_message_includes_recommended_action(tmp_path):
    """ADR-010 §Decisão (mensagem): ação primária recomendada + escape hatch."""
    (tmp_path / ".venv").mkdir()
    cfg = _write_yaml(tmp_path, _yaml_with_folder(tmp_path, auto_exclude=False))
    with pytest.raises(ValueError) as exc:
        load_config(cfg)
    msg = str(exc.value)
    assert "auto_exclude: true" in msg
    assert "recomendado" in msg
    # `exclude:` aparece em duas formas (auto_exclude: ... e adicione globs em exclude:);
    # asserir o trecho específico do escape hatch para diferenciar.
    assert "adicione globs em `exclude:`" in msg


# ---------------------------------------------------------------------------
# Edge cases: depth, missing path, substring variants, .git/ skip
# ---------------------------------------------------------------------------

def test_auto_exclude_false_respects_max_depth(tmp_path):
    """Marker em depth=7 NÃO detectado com max_depth default 6."""
    deep = tmp_path / "a" / "b" / "c" / "d" / "e" / "f" / "g"
    deep.mkdir(parents=True)
    (deep / ".venv").mkdir()  # marker em depth 7 (relativo ao root tmp_path)
    cfg = _write_yaml(tmp_path, _yaml_with_folder(tmp_path, auto_exclude=False))
    # Sem raise — depth 6 corta antes de listar dirnames em depth 6 (incluindo o pai do marker).
    app = load_config(cfg)
    assert app.folders[0].auto_exclude is False


def test_auto_exclude_false_skips_when_path_missing(tmp_path):
    """local_path inexistente → skip silente (paridade com find_git_repos)."""
    missing = tmp_path / "does-not-exist"
    cfg = _write_yaml(tmp_path, _yaml_with_folder(missing, auto_exclude=False))
    app = load_config(cfg)
    assert app.folders[0].auto_exclude is False


def test_auto_exclude_false_does_not_match_substring_variants(tmp_path):
    """ADR-010 §Decisão (1) F7 absorvida: match exato Path.name, não substring."""
    (tmp_path / ".venv-backup").mkdir()
    (tmp_path / "node_modules_old").mkdir()
    (tmp_path / "target-archived").mkdir()
    cfg = _write_yaml(tmp_path, _yaml_with_folder(tmp_path, auto_exclude=False))
    app = load_config(cfg)
    assert app.folders[0].auto_exclude is False


def test_auto_exclude_false_skips_dot_git_subtree(tmp_path):
    """ADR-010 §Decisão (3): .git/ inteiro fora do escopo — ADR-008 cobre via git_handling."""
    git_pack = tmp_path / ".git" / "objects" / "pack"
    git_pack.mkdir(parents=True)
    (git_pack / "pack-abc.pack").touch()
    cfg = _write_yaml(tmp_path, _yaml_with_folder(tmp_path, auto_exclude=False))
    # Sem raise; .git/ não dispara.
    app = load_config(cfg)
    assert app.folders[0].auto_exclude is False


def test_auto_exclude_false_detects_marker_at_max_depth_boundary(tmp_path):
    """Gap 1 (QA-reviewer): boundary do depth limit — marker em depth=6 É detectado.

    Code usa `len(rel_parts) >= max_depth`; rel_parts é do dirpath PAI. Marker em
    `a/b/c/d/e/f/.venv` tem pai `a/b/c/d/e/f` com rel_parts len=6 = max_depth → corta
    antes de listar. Logo, marker NA depth max_depth NÃO é detectado.
    Crava o comportamento via teste — off-by-one é a regressão mais provável.
    """
    # Marker em depth 6 (pai a/b/c/d/e em depth 5 → listdir inclui .venv em depth 6).
    depth5 = tmp_path / "a" / "b" / "c" / "d" / "e"
    depth5.mkdir(parents=True)
    (depth5 / ".venv").mkdir()
    cfg = _write_yaml(tmp_path, _yaml_with_folder(tmp_path, auto_exclude=False))
    with pytest.raises(ValueError) as exc:
        load_config(cfg)
    assert ".venv" in str(exc.value)


def test_auto_exclude_false_message_omits_substring_variants(tmp_path):
    """Gap 4 (QA-reviewer): mensagem lista só matches exatos, não variantes substring."""
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv-backup").mkdir()
    cfg = _write_yaml(tmp_path, _yaml_with_folder(tmp_path, auto_exclude=False))
    with pytest.raises(ValueError) as exc:
        load_config(cfg)
    msg = str(exc.value)
    assert str(tmp_path / ".venv") + "/" in msg
    assert ".venv-backup" not in msg


def test_auto_exclude_false_reports_nested_markers_independently(tmp_path):
    """Gap 5 (QA-reviewer, operador escolheu cravar atual): markers aninhados → hits independentes.

    Comportamento atual: validator continua descendo dentro de markers (sem clear). Marker
    dentro de marker (`.venv/inner/node_modules/`) produz 2 hits independentes. Crava o
    comportamento — mudança futura para 'primeiro hit por subtree' precisará atualizar
    este teste E o ADR-010.
    """
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "inner" / "node_modules").mkdir(parents=True)
    cfg = _write_yaml(tmp_path, _yaml_with_folder(tmp_path, auto_exclude=False))
    with pytest.raises(ValueError) as exc:
        load_config(cfg)
    msg = str(exc.value)
    assert str(tmp_path / ".venv") + "/" in msg
    assert str(tmp_path / ".venv" / "inner" / "node_modules") + "/" in msg


def test_auto_exclude_false_ignores_marker_as_file(tmp_path):
    """Gap 6 (QA-reviewer): marker como arquivo (não dir) NÃO dispara — match é em dirnames."""
    (tmp_path / ".venv").touch()  # arquivo, não dir
    cfg = _write_yaml(tmp_path, _yaml_with_folder(tmp_path, auto_exclude=False))
    app = load_config(cfg)
    assert app.folders[0].auto_exclude is False


def test_auto_exclude_false_uses_custom_max_recursion_depth(tmp_path):
    """git.max_recursion_depth customizado é reusado pela validação (ADR-010 §Decisão (2))."""
    # Marker em depth 3.
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    (deep / ".venv").mkdir()
    # Com max_depth=2, marker em depth 3 NÃO é detectado.
    cfg = _write_yaml(
        tmp_path,
        _yaml_with_folder(tmp_path, auto_exclude=False, max_depth=2),
    )
    app = load_config(cfg)
    assert app.folders[0].auto_exclude is False
