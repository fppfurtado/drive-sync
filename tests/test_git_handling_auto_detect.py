"""Tests for classify_repos (ADR-008 auto-detect)."""
import subprocess
from pathlib import Path

from drive_sync.config import FolderConfig, GitConfig, RepoOverride
from drive_sync.git_handler import classify_repos


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _init_repo(path: Path, with_commit: bool = True) -> None:
    """git init + opcional commit. Sem commit simula `git init` recém-rodado."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", str(path)], capture_output=True, check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@example.com"],
        capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Test"],
        capture_output=True, check=True,
    )
    if with_commit:
        (path / "README.md").write_text("# test")
        subprocess.run(
            ["git", "-C", str(path), "add", "."],
            capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "-C", str(path), "commit", "-m", "init"],
            capture_output=True, check=True,
        )


def _add_remote(
    repo: Path, name: str = "origin", url: str = "git@github.com:fake/repo.git"
) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", name, url],
        capture_output=True, check=True,
    )


def _folder(local_path: Path, repo_overrides=None) -> FolderConfig:
    return FolderConfig(
        name="test",
        local_path=local_path,
        remote_subpath="test",
        git_handling="auto",
        repo_overrides=list(repo_overrides or []),
    )


def _git_cfg() -> GitConfig:
    return GitConfig(recursive_detection=True, max_recursion_depth=4)


# ---------------------------------------------------------------------------
# Auto-detect: presença/ausência de remote decide mode
# ---------------------------------------------------------------------------

def test_classify_no_remote_returns_bundle(tmp_path):
    repo = tmp_path / "local-only"
    _init_repo(repo)
    classifications = classify_repos(_folder(tmp_path), _git_cfg())
    assert len(classifications) == 1
    c = classifications[0]
    assert c.repo_subpath == "local-only"
    assert c.mode == "bundle"
    assert c.reason == "no_remote"
    assert c.remote_url is None


def test_classify_with_remote_returns_skip(tmp_path):
    repo = tmp_path / "remote-backed"
    _init_repo(repo)
    _add_remote(repo, url="git@github.com:user/foo.git")
    classifications = classify_repos(_folder(tmp_path), _git_cfg())
    c = classifications[0]
    assert c.mode == "skip"
    assert c.reason == "has_remote"
    assert c.remote_url == "git@github.com:user/foo.git"


# ---------------------------------------------------------------------------
# repo_overrides — precedência total sobre auto-detect
# ---------------------------------------------------------------------------

def test_repo_override_wins_over_auto(tmp_path):
    """Repo com remote, mas override força bundle (caso 'desconfio do remote')."""
    repo = tmp_path / "forked"
    _init_repo(repo)
    _add_remote(repo)
    folder = _folder(
        tmp_path,
        repo_overrides=[RepoOverride(repo_subpath="forked", mode="bundle")],
    )
    classifications = classify_repos(folder, _git_cfg())
    c = classifications[0]
    assert c.mode == "bundle"
    assert c.reason == "override"


def test_repo_override_forces_skip_for_local_only(tmp_path):
    """Repo local-only, mas override força skip (caso 'descartar do sync')."""
    repo = tmp_path / "deprecated"
    _init_repo(repo)
    folder = _folder(
        tmp_path,
        repo_overrides=[RepoOverride(repo_subpath="deprecated", mode="skip")],
    )
    classifications = classify_repos(folder, _git_cfg())
    c = classifications[0]
    assert c.mode == "skip"
    assert c.reason == "override"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_repo_init_no_head_returns_bundle_silently(tmp_path):
    """git init sem commit → mode bundle; downstream create_bundle no-op silente."""
    repo = tmp_path / "empty"
    _init_repo(repo, with_commit=False)
    classifications = classify_repos(_folder(tmp_path), _git_cfg())
    c = classifications[0]
    assert c.mode == "bundle"
    assert c.reason == "no_remote"


def test_git_file_worktree_inherits_remote(tmp_path):
    """Worktree (`.git` é arquivo) — git remote -v delega ao repo principal."""
    main_repo = tmp_path / "main"
    _init_repo(main_repo)
    _add_remote(main_repo, url="git@github.com:owner/main.git")

    container = tmp_path / "container"
    container.mkdir()
    wt = container / "branch"
    subprocess.run(
        ["git", "-C", str(main_repo), "worktree", "add", str(wt)],
        capture_output=True, check=True,
    )
    assert (wt / ".git").is_file()  # confirma shape worktree

    folder = _folder(container)
    classifications = classify_repos(folder, _git_cfg())
    branch_class = next((c for c in classifications if c.repo_subpath == "branch"), None)
    assert branch_class is not None
    assert branch_class.mode == "skip"
    assert branch_class.remote_url == "git@github.com:owner/main.git"


def test_folder_local_path_is_itself_repo(tmp_path):
    """Quando folder.local_path É um repo, classification.repo_subpath == ''."""
    _init_repo(tmp_path)
    _add_remote(tmp_path, url="git@github.com:owner/root.git")
    classifications = classify_repos(_folder(tmp_path), _git_cfg())
    # tmp_path está incluído pois find_git_repos detecta o root também
    root_class = next((c for c in classifications if c.repo_subpath == ""), None)
    assert root_class is not None
    assert root_class.mode == "skip"
    assert root_class.remote_url == "git@github.com:owner/root.git"


def test_folder_path_missing_returns_empty(tmp_path):
    """folder.local_path inexistente → []. Sem raise (delegado a find_git_repos)."""
    missing = tmp_path / "does-not-exist"
    folder = _folder(missing)
    classifications = classify_repos(folder, _git_cfg())
    assert classifications == []


def test_bare_repo_not_classified(tmp_path):
    """Bare repo (HEAD na raiz, sem .git/ standard) é limitação conhecida — find_git_repos
    pula. ADR-008 ganha gatilho de revisão se virar caso real.
    """
    bare = tmp_path / "bare.git"
    subprocess.run(
        ["git", "init", "--bare", str(bare)],
        capture_output=True, check=True,
    )
    assert not (bare / ".git").exists()  # confirma shape bare
    classifications = classify_repos(_folder(tmp_path), _git_cfg())
    assert classifications == []


def test_multiple_remotes_classify_skip_with_first_remote_url(tmp_path):
    """Múltiplos remotes — classifier captura primeira URL do output (ordem inserção)."""
    repo = tmp_path / "multi"
    _init_repo(repo)
    _add_remote(repo, name="origin", url="git@github.com:owner/origin.git")
    _add_remote(repo, name="upstream", url="git@github.com:owner/upstream.git")
    classifications = classify_repos(_folder(tmp_path), _git_cfg())
    c = classifications[0]
    assert c.mode == "skip"
    # Qualquer URL configurada conta como "has remote" — invariante "≥1 remote → skip".
    # Lock no comportamento real de `git remote -v` (origem ordenada por inserção).
    assert c.remote_url == "git@github.com:owner/origin.git"


def test_repo_skip_log_includes_remote_url(tmp_path, caplog):
    """F3 absorvida (design-review): log [REPO_SKIP] enriquecido com URL para grep."""
    repo = tmp_path / "with-remote"
    _init_repo(repo)
    _add_remote(repo, url="git@github.com:user/foo.git")
    with caplog.at_level("INFO", logger="drive_sync.git_handler"):
        classify_repos(_folder(tmp_path), _git_cfg())
    assert any(
        "[REPO_SKIP]" in r.message and "has_remote: git@github.com:user/foo.git" in r.message
        for r in caplog.records
    )
