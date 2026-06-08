"""Tests for ADR-011: validação config-time de case-duplicates Path1↔Path2."""
import textwrap
from pathlib import Path

import pytest

from drive_sync.config import load_config


def _write_yaml(directory: Path, content: str) -> Path:
    path = directory / "config.yaml"
    path.write_text(textwrap.dedent(content))
    return path


def _yaml_with_folder(
    local_path: Path,
    max_depth: int | None = None,
    git_handling: str = "plain",
) -> str:
    depth_line = f"\ngit:\n  max_recursion_depth: {max_depth}" if max_depth is not None else ""
    return textwrap.dedent(f"""
        folders:
          - name: test
            local_path: {local_path}
            remote_subpath: test
            auto_exclude: true
            git_handling: {git_handling}
    """) + depth_line


# ---------------------------------------------------------------------------
# Caminho feliz: sem case-duplicates
# ---------------------------------------------------------------------------

def test_no_case_duplicates_passes(tmp_path):
    """Siblings de nomes distintos (sem colisão case-insensitive) → carrega sem erro."""
    (tmp_path / "documents").mkdir()
    (tmp_path / "photos").mkdir()
    (tmp_path / "videos").mkdir()
    cfg = _write_yaml(tmp_path, _yaml_with_folder(tmp_path))
    app = load_config(cfg)
    assert app.folders[0].git_handling == "plain"


# ---------------------------------------------------------------------------
# Detecção: top-level, nested, 3-way
# ---------------------------------------------------------------------------

def test_top_level_case_duplicates_raises(tmp_path):
    """family + Family no root → raise; mensagem inclui paths absolutos ↔-separados."""
    (tmp_path / "family").mkdir()
    (tmp_path / "Family").mkdir()
    cfg = _write_yaml(tmp_path, _yaml_with_folder(tmp_path))
    with pytest.raises(ValueError) as exc:
        load_config(cfg)
    msg = str(exc.value)
    assert str(tmp_path / "family") in msg
    assert str(tmp_path / "Family") in msg
    assert "↔" in msg


def test_nested_case_duplicates_raises(tmp_path):
    """sub/learning + sub/Learning → raise com paths corretos do parent aninhado."""
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "learning").mkdir()
    (sub / "Learning").mkdir()
    cfg = _write_yaml(tmp_path, _yaml_with_folder(tmp_path))
    with pytest.raises(ValueError) as exc:
        load_config(cfg)
    msg = str(exc.value)
    assert str(sub / "learning") in msg
    assert str(sub / "Learning") in msg


def test_aggregates_multiple_pairs(tmp_path):
    """3 pares em paths diferentes (top-level + nested) → mensagem lista os 3 grupos."""
    (tmp_path / "family").mkdir()
    (tmp_path / "Family").mkdir()
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "work").mkdir()
    (sub / "Work").mkdir()
    other = tmp_path / "other"
    other.mkdir()
    (other / "data").mkdir()
    (other / "DATA").mkdir()
    cfg = _write_yaml(tmp_path, _yaml_with_folder(tmp_path))
    with pytest.raises(ValueError) as exc:
        load_config(cfg)
    msg = str(exc.value)
    # 3 grupos presentes; assert via paths absolutos
    assert str(tmp_path / "family") in msg and str(tmp_path / "Family") in msg
    assert str(sub / "work") in msg and str(sub / "Work") in msg
    assert str(other / "data") in msg and str(other / "DATA") in msg
    # 3 bullets `↔`-separados
    assert msg.count("↔") == 3


def test_three_way_collision_grouped(tmp_path):
    """family/Family/FAMILY siblings → 1 bullet com os 3 nomes, não 3 bullets."""
    (tmp_path / "family").mkdir()
    (tmp_path / "Family").mkdir()
    (tmp_path / "FAMILY").mkdir()
    cfg = _write_yaml(tmp_path, _yaml_with_folder(tmp_path))
    with pytest.raises(ValueError) as exc:
        load_config(cfg)
    msg = str(exc.value)
    # Apenas 1 bullet de colisão (1 ocorrência de `↔` separando 3 nomes; 2 separadores)
    # Estrutura: "<a> ↔ <b> ↔ <c>" → 2 ocorrências de `↔` no bullet único.
    # Como não há outros pares no fixture, total de `↔` na mensagem == 2.
    assert msg.count("↔") == 2
    assert str(tmp_path / "family") in msg
    assert str(tmp_path / "Family") in msg
    assert str(tmp_path / "FAMILY") in msg


# ---------------------------------------------------------------------------
# Skip rules: git_handling bundle|skip, local_path inexistente
# ---------------------------------------------------------------------------

def test_skips_when_git_handling_bundle(tmp_path):
    """git_handling: bundle não passa por bisync — validator skipa case-duplicates."""
    (tmp_path / "family").mkdir()
    (tmp_path / "Family").mkdir()
    cfg = _write_yaml(tmp_path, _yaml_with_folder(tmp_path, git_handling="bundle"))
    app = load_config(cfg)
    assert app.folders[0].git_handling == "bundle"


def test_skips_when_git_handling_skip(tmp_path):
    """git_handling: skip → folder fora do sync; validator skipa."""
    (tmp_path / "family").mkdir()
    (tmp_path / "Family").mkdir()
    cfg = _write_yaml(tmp_path, _yaml_with_folder(tmp_path, git_handling="skip"))
    app = load_config(cfg)
    assert app.folders[0].git_handling == "skip"


def test_skips_when_local_path_missing(tmp_path):
    """local_path inexistente → skip silente (paridade com find_git_repos)."""
    missing = tmp_path / "does-not-exist"
    cfg = _write_yaml(tmp_path, _yaml_with_folder(missing))
    app = load_config(cfg)
    assert app.folders[0].local_path == missing


# ---------------------------------------------------------------------------
# Edge cases: depth, .git/ excluído
# ---------------------------------------------------------------------------

def test_respects_max_depth(tmp_path):
    """Par case-duplicate em depth=7 NÃO detectado com max_depth default 6."""
    # Par no dirpath `a/b/c/d/e/f/g` — len(rel_parts)=7, >= max_depth=6 corta antes.
    deep_parent = tmp_path / "a" / "b" / "c" / "d" / "e" / "f" / "g"
    deep_parent.mkdir(parents=True)
    (deep_parent / "family").mkdir()
    (deep_parent / "Family").mkdir()
    cfg = _write_yaml(tmp_path, _yaml_with_folder(tmp_path))
    # Sem raise — par está em depth 8 (relativo ao root), seu dirpath em depth 7 > limit.
    app = load_config(cfg)
    assert app.folders[0].git_handling == "plain"


def test_dot_git_subtree_excluded(tmp_path):
    """ADR-011 §Decisão: .git/ fora do escopo — ADR-008 cobre repos git estruturalmente."""
    refs = tmp_path / ".git" / "refs" / "heads"
    refs.mkdir(parents=True)
    (refs / "feature").mkdir()
    (refs / "Feature").mkdir()
    cfg = _write_yaml(tmp_path, _yaml_with_folder(tmp_path))
    # Sem raise; .git/ não dispara.
    app = load_config(cfg)
    assert app.folders[0].git_handling == "plain"


# ---------------------------------------------------------------------------
# Mensagem de erro: ação recomendada + paths concretos + separador ↔
# ---------------------------------------------------------------------------

def test_error_message_includes_recommended_action(tmp_path):
    """ADR-011 §Decisão mensagem: instrução de cleanup operator-driven explícita."""
    (tmp_path / "family").mkdir()
    (tmp_path / "Family").mkdir()
    cfg = _write_yaml(tmp_path, _yaml_with_folder(tmp_path))
    with pytest.raises(ValueError) as exc:
        load_config(cfg)
    msg = str(exc.value)
    assert "Cleanup é responsabilidade do operador" in msg
    assert "rename, merge ou delete" in msg
    assert "re-execute `--check`" in msg


def test_error_message_lists_concrete_pairs(tmp_path):
    """Mensagem inclui paths absolutos `↔`-separados; um bullet por grupo de colisão."""
    (tmp_path / "family").mkdir()
    (tmp_path / "Family").mkdir()
    cfg = _write_yaml(tmp_path, _yaml_with_folder(tmp_path))
    with pytest.raises(ValueError) as exc:
        load_config(cfg)
    msg = str(exc.value)
    # Bullet começa com 2 espaços + hífen; paths absolutos com ↔ separador.
    expected = f"  - {tmp_path / 'Family'} ↔ {tmp_path / 'family'}"
    assert expected in msg
    # Nome do folder e Path1 citados no header.
    assert "'test'" in msg
    assert str(tmp_path) in msg


# ---------------------------------------------------------------------------
# Synthetic herda validação do parent (ADR-011 §Mitigações terceiro bullet)
# ---------------------------------------------------------------------------

def test_parent_walk_covers_subpath_overrides_subtree(tmp_path):
    """ADR-011 §Mitigações 3o bullet: parent's walk varre subtree do subpath_overrides,
    detectando case-duplicates DENTRO de `x/` antes da expansão de synthetic. Mensagem
    cita o folder name do parent (não synthetic), comprovando que o gate disparou no parent."""
    # Case-duplicates DENTRO do subpath (não no root do parent).
    x = tmp_path / "x"
    x.mkdir()
    (x / "family").mkdir()
    (x / "Family").mkdir()
    cfg = _write_yaml(tmp_path, textwrap.dedent(f"""
        folders:
          - name: parent_folder
            local_path: {tmp_path}
            remote_subpath: parent
            auto_exclude: true
            git_handling: plain
            subpath_overrides:
              - subpath: x
                git_handling: bundle
    """))
    with pytest.raises(ValueError) as exc:
        load_config(cfg)
    msg = str(exc.value)
    # Paths dentro de x/ presentes na mensagem.
    assert str(x / "family") in msg
    assert str(x / "Family") in msg
    # Header cita o nome do parent (não synthetic name "parent_folder/x").
    assert "'parent_folder'" in msg
    assert "parent_folder/x" not in msg


# ---------------------------------------------------------------------------
# Gap 1: boundary positivo do max_depth (paridade com test_..._marker_at_max_depth_boundary)
# ---------------------------------------------------------------------------

def test_detects_case_duplicates_at_max_depth_boundary(tmp_path):
    """Boundary positivo: par no dirpath em depth=5 (rel_parts len=5 < 6) É detectado.

    Crava o gate `len(rel_parts) >= max_depth` no comportamento atual — off-by-one
    (mudar para `>`) silenciaria detecção em depth=6 sem nenhum teste positivo falhar.
    """
    # dirpath em depth 5; par case-duplicate como children (depth 6 relativo).
    depth5 = tmp_path / "a" / "b" / "c" / "d" / "e"
    depth5.mkdir(parents=True)
    (depth5 / "family").mkdir()
    (depth5 / "Family").mkdir()
    cfg = _write_yaml(tmp_path, _yaml_with_folder(tmp_path))
    with pytest.raises(ValueError) as exc:
        load_config(cfg)
    msg = str(exc.value)
    assert str(depth5 / "family") in msg
    assert str(depth5 / "Family") in msg


# ---------------------------------------------------------------------------
# Gap 2: git.max_recursion_depth customizado é reusado (paridade com ADR-010)
# ---------------------------------------------------------------------------

def test_respects_custom_max_recursion_depth(tmp_path):
    """git.max_recursion_depth customizado é reusado — ADR-011 §Decisão "Reuso".

    Sem isso, refactor que hardcode 6 no validator passaria todos os testes atuais.
    """
    # Par em depth=3 (dirpath em depth=2, rel_parts len=2 >= max_depth=2 → corta).
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    (deep / "family").mkdir()
    (deep / "Family").mkdir()
    cfg = _write_yaml(tmp_path, _yaml_with_folder(tmp_path, max_depth=2))
    # Sem raise — max_depth=2 corta antes de listar siblings em depth=2.
    app = load_config(cfg)
    assert app.folders[0].git_handling == "plain"


# ---------------------------------------------------------------------------
# Gap 3: git_handling: auto também valida (não só plain)
# ---------------------------------------------------------------------------

def test_validates_for_git_handling_auto(tmp_path):
    """ADR-011 §Decisão linha 31: aplica a `auto` E `plain` (modos que bisync worktree).

    Sem este teste, refactor que estreite skip-gate para "só plain valida" passaria
    silenciosamente (todos os outros testes positivos usam plain).
    """
    (tmp_path / "family").mkdir()
    (tmp_path / "Family").mkdir()
    cfg = _write_yaml(tmp_path, _yaml_with_folder(tmp_path, git_handling="auto"))
    with pytest.raises(ValueError) as exc:
        load_config(cfg)
    msg = str(exc.value)
    assert str(tmp_path / "family") in msg
    assert str(tmp_path / "Family") in msg


# ---------------------------------------------------------------------------
# Gap 5: detecta case-duplicates em arquivos e mistura dir/arquivo
# (ADR-011 §Decisão "dirs e arquivos" — Proton trata os dois como mesma entry)
# ---------------------------------------------------------------------------

def test_detects_case_duplicates_at_file_level(tmp_path):
    """Arquivos `readme.md` + `README.md` colidem case-insensitive no Proton — disparam."""
    (tmp_path / "readme.md").touch()
    (tmp_path / "README.md").touch()
    cfg = _write_yaml(tmp_path, _yaml_with_folder(tmp_path))
    with pytest.raises(ValueError) as exc:
        load_config(cfg)
    msg = str(exc.value)
    assert str(tmp_path / "readme.md") in msg
    assert str(tmp_path / "README.md") in msg


def test_detects_case_duplicates_mixed_dir_and_file(tmp_path):
    """Dir `family/` e arquivo `Family` (sem extensão) colidem case-insensitive — disparam.

    Proton não distingue dir vs file no namespace; ambos viram entry `family`.
    """
    (tmp_path / "family").mkdir()
    (tmp_path / "Family").touch()  # arquivo
    cfg = _write_yaml(tmp_path, _yaml_with_folder(tmp_path))
    with pytest.raises(ValueError) as exc:
        load_config(cfg)
    msg = str(exc.value)
    assert str(tmp_path / "family") in msg
    assert str(tmp_path / "Family") in msg
