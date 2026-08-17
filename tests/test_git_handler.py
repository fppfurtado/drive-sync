"""Tests for git_handler — repo detection, bundle path, and replace heuristic."""
import os
import subprocess
import time
from pathlib import Path

import pytest

from drive_sync.git_handler import (
    bundle_path_for,
    find_git_repos,
    is_git_repo,
    should_replace_bundle,
    worktree_last_modified,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _init_repo(path: Path) -> None:
    """Create a git repo with one commit so ls-files works reliably."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", str(path)], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], capture_output=True, check=True)
    (path / "README.md").write_text("# test")
    subprocess.run(["git", "-C", str(path), "add", "."], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "init"], capture_output=True, check=True)


# ---------------------------------------------------------------------------
# is_git_repo
# ---------------------------------------------------------------------------

def test_is_git_repo_true(tmp_path):
    _init_repo(tmp_path / "repo")
    assert is_git_repo(tmp_path / "repo") is True


def test_is_git_repo_false_for_plain_dir(tmp_path):
    (tmp_path / "plain").mkdir()
    assert is_git_repo(tmp_path / "plain") is False


def test_is_git_repo_false_for_nonexistent(tmp_path):
    assert is_git_repo(tmp_path / "missing") is False


# ---------------------------------------------------------------------------
# find_git_repos
# ---------------------------------------------------------------------------

def test_find_single_repo(tmp_path):
    _init_repo(tmp_path / "repo")
    result = find_git_repos(tmp_path, max_depth=3)
    assert tmp_path / "repo" in result


def test_find_nested_repos(tmp_path):
    _init_repo(tmp_path / "outer")
    _init_repo(tmp_path / "outer" / "inner")
    result = find_git_repos(tmp_path, max_depth=4)
    assert tmp_path / "outer" in result
    assert tmp_path / "outer" / "inner" in result


def test_depth_limit_respected(tmp_path):
    deep = tmp_path / "a" / "b" / "c"
    _init_repo(deep)
    # max_depth=2: tmp_path(0) → a(1) → b(2); c is at depth 3
    result = find_git_repos(tmp_path, max_depth=2)
    assert deep not in result


def test_empty_directory_returns_empty(tmp_path):
    (tmp_path / "empty").mkdir()
    assert find_git_repos(tmp_path / "empty", max_depth=3) == []


def test_multiple_sibling_repos(tmp_path):
    for name in ("alpha", "beta", "gamma"):
        _init_repo(tmp_path / name)
    result = find_git_repos(tmp_path, max_depth=2)
    assert {r.name for r in result} == {"alpha", "beta", "gamma"}


# ---------------------------------------------------------------------------
# bundle_path_for
# ---------------------------------------------------------------------------

def test_bundle_path_simple(tmp_path):
    source_root = tmp_path / "projects"
    repo = source_root / "myrepo"
    bundles_dir = tmp_path / "bundles"
    result = bundle_path_for(repo, source_root, bundles_dir, ".gitbundle")
    assert result == bundles_dir / "myrepo.gitbundle"


def test_bundle_path_nested(tmp_path):
    source_root = tmp_path / "projects"
    repo = source_root / "org" / "myrepo"
    bundles_dir = tmp_path / "bundles"
    result = bundle_path_for(repo, source_root, bundles_dir, ".gitbundle")
    assert result == bundles_dir / "org" / "myrepo.gitbundle"


def test_bundle_path_repo_is_root(tmp_path):
    repo = tmp_path / "myrepo"
    result = bundle_path_for(repo, repo, tmp_path / "bundles", ".gitbundle")
    assert result.name == "myrepo.gitbundle"


def test_bundle_path_custom_suffix(tmp_path):
    source_root = tmp_path / "src"
    repo = source_root / "proj"
    result = bundle_path_for(repo, source_root, tmp_path / "b", ".bundle")
    assert result.suffix == ".bundle"


# ---------------------------------------------------------------------------
# should_replace_bundle
# ---------------------------------------------------------------------------

def test_replace_when_no_bundle(tmp_path):
    _init_repo(tmp_path / "repo")
    assert should_replace_bundle(tmp_path / "repo", tmp_path / "missing.gitbundle") is True


def test_no_replace_when_bundle_is_newer(tmp_path):
    _init_repo(tmp_path / "repo")
    bundle = tmp_path / "repo.gitbundle"
    bundle.write_bytes(b"fake bundle")
    future = time.time() + 3600
    os.utime(bundle, (future, future))
    assert should_replace_bundle(tmp_path / "repo", bundle) is False


def test_replace_when_repo_is_newer(tmp_path):
    _init_repo(tmp_path / "repo")
    bundle = tmp_path / "repo.gitbundle"
    bundle.write_bytes(b"fake bundle")
    past = time.time() - 3600
    os.utime(bundle, (past, past))
    assert should_replace_bundle(tmp_path / "repo", bundle) is True


# ---------------------------------------------------------------------------
# worktree_last_modified
# ---------------------------------------------------------------------------

def test_last_modified_returns_float(tmp_path):
    _init_repo(tmp_path / "repo")
    result = worktree_last_modified(tmp_path / "repo")
    assert isinstance(result, float)
    assert result > 0


def test_last_modified_increases_after_new_file(tmp_path):
    _init_repo(tmp_path / "repo")
    before = worktree_last_modified(tmp_path / "repo")
    time.sleep(0.05)
    (tmp_path / "repo" / "new_file.txt").write_text("hello")
    after = worktree_last_modified(tmp_path / "repo")
    assert after >= before


# ---------------------------------------------------------------------------
# is_linked_worktree (#24)
# ---------------------------------------------------------------------------

def test_is_linked_worktree_true_for_real_worktree(tmp_path):
    from drive_sync.git_handler import is_linked_worktree
    import subprocess
    main = tmp_path / "main"
    main.mkdir()
    subprocess.run(["git", "init", str(main)], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(main), "config", "user.email", "t@t"], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(main), "config", "user.name", "t"], capture_output=True, check=True)
    (main / "f").write_text("x")
    subprocess.run(["git", "-C", str(main), "add", "."], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(main), "commit", "-m", "i"], capture_output=True, check=True)
    wt = tmp_path / "wt"
    subprocess.run(["git", "-C", str(main), "worktree", "add", str(wt)], capture_output=True, check=True)

    assert is_linked_worktree(wt) is True
    assert is_linked_worktree(main) is False


def test_is_linked_worktree_false_for_submodule_shape(tmp_path):
    from drive_sync.git_handler import is_linked_worktree
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / ".git").write_text("gitdir: ../.git/modules/sub\n")
    assert is_linked_worktree(sub) is False


def test_is_linked_worktree_false_for_non_repo_and_malformed(tmp_path):
    from drive_sync.git_handler import is_linked_worktree
    assert is_linked_worktree(tmp_path) is False
    weird = tmp_path / "weird"
    weird.mkdir()
    (weird / ".git").write_text("not a gitdir line\n")
    assert is_linked_worktree(weird) is False


# ---------------------------------------------------------------------------
# Snapshot/bundle em repo sem HEAD (#27) — index temporário vazio
# ---------------------------------------------------------------------------

def _init_commitless_repo_with_files(path):
    """git init + arquivos NO worktree, zero commits — o shape do incidente quill."""
    import subprocess
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", str(path)], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], capture_output=True, check=True)
    (path / "README.md").write_text("# conteúdo sem commit")
    (path / "docs").mkdir()
    (path / "docs" / "a.md").write_text("a")


def test_snapshot_succeeds_on_commitless_repo(tmp_path):
    """Repo sem HEAD: add -A não pode tropeçar no index temp vazio (#27)."""
    from drive_sync.git_handler import _create_worktree_snapshot, _delete_snapshot_refs
    repo = tmp_path / "quill"
    _init_commitless_repo_with_files(repo)

    assert _create_worktree_snapshot(repo) is True
    _delete_snapshot_refs(repo)


def test_create_bundle_succeeds_on_commitless_repo(tmp_path):
    """Bundle snapshot-only de repo sem commits — conteúdo local-only ganha backup."""
    from drive_sync.git_handler import create_bundle, restore_from_bundle
    import subprocess
    repo = tmp_path / "quill"
    _init_commitless_repo_with_files(repo)
    dest = tmp_path / "quill.gitbundle"

    assert create_bundle(repo, dest, bundle_all=True) is True
    assert dest.exists() and dest.stat().st_size > 0

    # Round-trip: restaura o conteúdo num clone vazio a partir do bundle.
    target = tmp_path / "restored"
    target.mkdir()
    subprocess.run(["git", "init", str(target)], capture_output=True, check=True)
    restore_from_bundle(dest, target)
    assert (target / "README.md").read_text() == "# conteúdo sem commit"
    assert (target / "docs" / "a.md").read_text() == "a"


def test_create_bundle_empty_repo_yields_empty_snapshot_bundle(tmp_path):
    """Sem commits E sem arquivos: snapshot de tree vazia → bundle válido (True).

    Comportamento novo pós-#27 (antes: False, que envenenava o success agregado
    do folder no modo auto para sempre). Bundle de tree vazia é minúsculo e
    inócuo; o retorno True preserva o success signal honesto.
    """
    from drive_sync.git_handler import create_bundle
    import subprocess
    repo = tmp_path / "vazio"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], capture_output=True, check=True)
    dest = tmp_path / "vazio.gitbundle"

    assert create_bundle(repo, dest) is True
    assert dest.exists()


def test_find_git_repos_does_not_descend_into_linked_worktree(tmp_path):
    """Worktree entra na lista (p/ --exclude do auto) mas o subtree não é varrido (#30)."""
    import subprocess
    from drive_sync.git_handler import find_git_repos
    main = tmp_path / "main"
    _init_commitless_repo_with_files(main)
    subprocess.run(["git", "-C", str(main), "add", "."], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(main), "commit", "-m", "i"], capture_output=True, check=True)
    wt = tmp_path / "wt"
    subprocess.run(["git", "-C", str(main), "worktree", "add", str(wt)], capture_output=True, check=True)
    nested = wt / "nested-repo"
    nested.mkdir()
    subprocess.run(["git", "init", str(nested)], capture_output=True, check=True)

    repos = find_git_repos(tmp_path, max_depth=6)

    assert wt in repos          # a worktree em si segue descoberta
    assert nested not in repos  # o aninhado dentro dela, não
    assert main in repos


# ---------------------------------------------------------------------------
# Restore fresh-clone: HEAD derivado de SNAPSHOT_REF^ (sem HEAD_MARKER_REF) (#17)
# ---------------------------------------------------------------------------

def _git(repo, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=check,
    )


def _init_repo_with_commit(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], capture_output=True, check=True)
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    (path / "committed.txt").write_text("v1")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "base")


def test_fresh_clone_restore_repositions_head_and_restores_wip(tmp_path):
    """Fresh-clone: HEAD volta ao commit original (via SNAPSHOT_REF^) e o WIP
    não-commitado + untracked é materializado — prova de equivalência do #17."""
    from drive_sync.git_handler import create_bundle, restore_from_bundle

    src = tmp_path / "src"
    _init_repo_with_commit(src)
    original_head = _git(src, "rev-parse", "HEAD").stdout.strip()
    # Estado não-commitado: modifica tracked + adiciona untracked.
    (src / "committed.txt").write_text("v2-uncommitted")
    (src / "novo.txt").write_text("untracked wip")

    dest_bundle = tmp_path / "src.gitbundle"
    assert create_bundle(src, dest_bundle, bundle_all=True) is True

    target = tmp_path / "restored"  # NÃO existe → caminho fresh-clone
    assert restore_from_bundle(dest_bundle, target) is True

    # HEAD no commit original (não num commit-snapshot).
    assert _git(target, "rev-parse", "HEAD").stdout.strip() == original_head
    # WIP não-commitado materializado no worktree.
    assert (target / "committed.txt").read_text() == "v2-uncommitted"
    assert (target / "novo.txt").read_text() == "untracked wip"
    # Nenhuma ref interna vaza pro repo do usuário.
    refs = _git(target, "for-each-ref", "refs/drive-sync/").stdout.strip()
    assert refs == ""


def test_fresh_clone_restore_commitless_repo(tmp_path):
    """Fresh-clone de repo de origem SEM commits: SNAPSHOT_REF^ não existe →
    sem reposicionamento, mas o conteúdo do worktree é restaurado (#17/#27)."""
    from drive_sync.git_handler import create_bundle, restore_from_bundle

    src = tmp_path / "src"
    src.mkdir()
    subprocess.run(["git", "init", "-q", str(src)], capture_output=True, check=True)
    _git(src, "config", "user.email", "t@t")
    _git(src, "config", "user.name", "t")
    (src / "so-worktree.txt").write_text("sem commit nenhum")

    dest_bundle = tmp_path / "src.gitbundle"
    assert create_bundle(src, dest_bundle, bundle_all=True) is True

    target = tmp_path / "restored"
    # Não deve levantar apesar de SNAPSHOT_REF^ inexistente.
    restore_from_bundle(dest_bundle, target)
    assert (target / "so-worktree.txt").read_text() == "sem commit nenhum"


def test_delete_snapshot_refs_clears_legacy_head_marker(tmp_path):
    """Restore cross-version: bundle antigo traz a ref legada head-at-snapshot;
    _delete_snapshot_refs a apaga junto (transição pré-#17 → sem lixo)."""
    from drive_sync.git_handler import _delete_snapshot_refs
    repo = tmp_path / "r"
    _init_repo_with_commit(repo)
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "update-ref", "refs/drive-sync/snapshot", head)
    _git(repo, "update-ref", "refs/drive-sync/head-at-snapshot", head)

    _delete_snapshot_refs(repo)

    assert _git(repo, "for-each-ref", "refs/drive-sync/").stdout.strip() == ""
