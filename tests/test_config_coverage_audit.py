"""Tests for #56 / ADR-015: audit de cobertura de órfãos (config-time, warn).

Cobre `audit_coverage_orphans` (modelo B, git-handling-agnóstico) + o parsing da
seção `coverage_audit` + a integração no `--check` (warn, não fatal).
"""
import textwrap
from pathlib import Path

from drive_sync.__main__ import main
from drive_sync.config import (
    CoverageAuditConfig,
    FolderConfig,
    audit_coverage_orphans,
    load_config,
)


def _folder(local_path: Path, name: str = "f", git_handling: str = "plain") -> FolderConfig:
    return FolderConfig(
        name=name,
        local_path=local_path,
        remote_subpath=name,
        git_handling=git_handling,
    )


def _with_file(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "conteudo.txt").write_text("x")
    return directory


# ---------------------------------------------------------------------------
# S1/S2: órfão sibling é sinalizado; coberto e allowlisted não são
# ---------------------------------------------------------------------------

def test_orphan_sibling_flagged(tmp_path):
    """Sibling com conteúdo, sem folder cobrindo → órfão (o caso Screenshots)."""
    pics = tmp_path / "pictures"
    _with_file(pics / "screenshots")           # configurado
    _with_file(pics / "Screenshots")           # órfão (sibling, tem conteúdo)
    _with_file(pics / "photos")                # órfão
    orphans = audit_coverage_orphans([_folder(pics / "screenshots")], allow=[])
    assert (pics / "Screenshots").resolve() in orphans
    assert (pics / "photos").resolve() in orphans


def test_covered_path_not_flagged(tmp_path):
    """O próprio local_path declarado nunca é órfão de si mesmo."""
    pics = tmp_path / "pictures"
    _with_file(pics / "screenshots")
    orphans = audit_coverage_orphans([_folder(pics / "screenshots")], allow=[])
    assert (pics / "screenshots").resolve() not in orphans


def test_allowlisted_sibling_not_flagged(tmp_path):
    """Sibling intencionalmente-fora (allowlist) não é sinalizado (S1)."""
    pics = tmp_path / "pictures"
    _with_file(pics / "screenshots")
    _with_file(pics / "sandbox")               # intencional
    orphans = audit_coverage_orphans(
        [_folder(pics / "screenshots")], allow=[pics / "sandbox"]
    )
    assert (pics / "sandbox").resolve() not in orphans


def test_allow_subpath_match(tmp_path):
    """Allowlist casa também subpaths de uma entry (allow do pai cobre o filho)."""
    pics = tmp_path / "pictures"
    _with_file(pics / "screenshots")
    _with_file(pics / "tools")
    orphans = audit_coverage_orphans(
        [_folder(pics / "screenshots")], allow=[pics]  # allow o pai inteiro
    )
    assert orphans == []


# ---------------------------------------------------------------------------
# Edge: dir vazio, arquivo (não-dir), cobertura parcial
# ---------------------------------------------------------------------------

def test_empty_dir_not_flagged(tmp_path):
    """Sibling sem arquivo algum (dir vazio / só subdirs vazios) → não sinaliza."""
    pics = tmp_path / "pictures"
    _with_file(pics / "screenshots")
    (pics / "vazio").mkdir()
    (pics / "vazio" / "sub").mkdir()           # subdir vazio, sem arquivo
    orphans = audit_coverage_orphans([_folder(pics / "screenshots")], allow=[])
    assert (pics / "vazio").resolve() not in orphans


def test_sibling_file_not_flagged(tmp_path):
    """Um ARQUIVO sibling (não-diretório) não é órfão de cobertura."""
    pics = tmp_path / "pictures"
    _with_file(pics / "screenshots")
    (pics / "solto.txt").write_text("x")
    orphans = audit_coverage_orphans([_folder(pics / "screenshots")], allow=[])
    assert (pics / "solto.txt").resolve() not in orphans


def test_child_inside_covered_not_flagged(tmp_path):
    """Filho DENTRO de um folder configurado não é órfão (está coberto)."""
    docs = tmp_path / "documents"
    _with_file(docs / "sub")
    # parent scan de `documents` vê `sub`, mas `sub` está sob o coberto `documents`.
    orphans = audit_coverage_orphans([_folder(docs)], allow=[])
    assert (docs / "sub").resolve() not in orphans


def test_dir_containing_configured_not_flagged(tmp_path):
    """Sibling que CONTÉM um folder configurado (cobertura parcial) → não sinaliza."""
    root = tmp_path / "area"
    _with_file(root / "grupo" / "coberto")     # configurado é grupo/coberto
    _with_file(root / "grupo" / "extra")       # conteúdo extra sob grupo/
    # folder configurado aponta grupo/coberto; parent = grupo/. sibling `extra` é órfão;
    # mas o pai de grupo/coberto é grupo/, cujo sibling seria em `area/`. Verifica que
    # `grupo` (que contém o coberto) não é flageado a partir do scan de area/.
    orphans = audit_coverage_orphans([_folder(root / "grupo" / "coberto")], allow=[])
    assert (root / "grupo").resolve() not in orphans
    assert (root / "grupo" / "extra").resolve() in orphans


# ---------------------------------------------------------------------------
# git_handling é ORTOGONAL (soundness ADR-015): todo declarado é "conhecido"
# ---------------------------------------------------------------------------

def test_disabled_folder_counts_as_covered(tmp_path):
    """Folder declarado com enabled=false é toggle consciente → não é órfão (documentado)."""
    dev = tmp_path / "dev"
    _with_file(dev / "projects")
    _with_file(dev / "paused")
    folders = [
        _folder(dev / "projects", name="projects"),
        FolderConfig(name="paused", local_path=dev / "paused",
                     remote_subpath="paused", enabled=False, git_handling="plain"),
    ]
    orphans = audit_coverage_orphans(folders, allow=[])
    assert (dev / "paused").resolve() not in orphans


def test_git_handling_agnostic_skip_folder_not_flagged(tmp_path):
    """Um folder declarado git_handling=skip é 'conhecido' → não é órfão, apesar de
    deliberadamente não-sincronizado. Só conteúdo NÃO-declarado alarma."""
    dev = tmp_path / "dev"
    _with_file(dev / "projects")               # configurado, plain
    _with_file(dev / "vendor")                 # configurado, skip
    _with_file(dev / "random")                 # órfão real
    folders = [
        _folder(dev / "projects", name="projects", git_handling="plain"),
        _folder(dev / "vendor", name="vendor", git_handling="skip"),
    ]
    orphans = audit_coverage_orphans(folders, allow=[])
    assert (dev / "vendor").resolve() not in orphans   # skip declarado = conhecido
    assert (dev / "random").resolve() in orphans


# ---------------------------------------------------------------------------
# Parsing da seção coverage_audit
# ---------------------------------------------------------------------------

def _write_cfg(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent(body))
    return p


def test_coverage_audit_defaults(tmp_path):
    """Sem seção coverage_audit → enabled True, allow vazio."""
    (tmp_path / "d").mkdir()
    cfg = _write_cfg(tmp_path, f"""
        folders:
          - name: d
            local_path: {tmp_path / "d"}
            remote_subpath: d
            git_handling: plain
    """)
    app = load_config(cfg)
    assert app.coverage_audit.enabled is True
    assert app.coverage_audit.allow == []


def test_coverage_audit_parsed(tmp_path):
    """Seção coverage_audit lida: enabled + allow expandidos."""
    (tmp_path / "d").mkdir()
    cfg = _write_cfg(tmp_path, f"""
        folders:
          - name: d
            local_path: {tmp_path / "d"}
            remote_subpath: d
            git_handling: plain
        coverage_audit:
          enabled: false
          allow:
            - {tmp_path / "sandbox"}
    """)
    app = load_config(cfg)
    assert app.coverage_audit.enabled is False
    assert app.coverage_audit.allow == [(tmp_path / "sandbox")]


# ---------------------------------------------------------------------------
# Integração --check: warn (não fatal), exit 0
# ---------------------------------------------------------------------------

def _no_logging_setup(monkeypatch):
    """Evita que main() reconfigure o logger `drive_sync` (propagate=False quebraria
    o caplog de testes seguintes). Testamos a audit, não o logging."""
    import logging

    monkeypatch.setattr(
        "drive_sync.__main__.setup_logging",
        lambda _cfg: logging.getLogger("drive_sync.test"),
    )


def test_check_warns_orphan_but_exits_zero(tmp_path, capsys, monkeypatch):
    """--check com órfão presente: lista no stderr, mas exit 0 (warn, não fatal, S1)."""
    _no_logging_setup(monkeypatch)
    pics = tmp_path / "pictures"
    _with_file(pics / "screenshots")
    _with_file(pics / "Screenshots")
    cfg = _write_cfg(tmp_path, f"""
        folders:
          - name: pics
            local_path: {pics / "screenshots"}
            remote_subpath: pics
            git_handling: plain
    """)
    rc = main(["--config", str(cfg), "--check"])
    assert rc == 0
    err = capsys.readouterr().err
    assert str((pics / "Screenshots")) in err
    assert "órfãos de cobertura" in err


def test_check_opt_out_silences_audit(tmp_path, capsys, monkeypatch):
    """coverage_audit.enabled=false → --check não roda o audit."""
    _no_logging_setup(monkeypatch)
    pics = tmp_path / "pictures"
    _with_file(pics / "screenshots")
    _with_file(pics / "Screenshots")
    cfg = _write_cfg(tmp_path, f"""
        folders:
          - name: pics
            local_path: {pics / "screenshots"}
            remote_subpath: pics
            git_handling: plain
        coverage_audit:
          enabled: false
    """)
    rc = main(["--config", str(cfg), "--check"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "órfãos" not in err
