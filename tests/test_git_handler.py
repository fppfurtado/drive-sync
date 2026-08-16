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
