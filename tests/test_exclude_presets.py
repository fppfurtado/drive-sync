"""Tests for exclude_presets."""
from drive_sync.exclude_presets import default_excludes_for_code


def test_returns_list_of_strings():
    result = default_excludes_for_code()
    assert isinstance(result, list)
    assert len(result) > 0
    assert all(isinstance(p, str) for p in result)


def test_contains_common_build_artifacts():
    patterns = default_excludes_for_code()
    assert "node_modules/**" in patterns
    assert "__pycache__/**" in patterns
    assert "target/**" in patterns
    assert ".venv/**" in patterns
    assert "*.pyc" in patterns


def test_no_duplicate_patterns():
    patterns = default_excludes_for_code()
    assert len(patterns) == len(set(patterns))


def test_git_dir_itself_not_excluded():
    # In bisync mode .git/ must travel with the repo so the cloud copy stays usable.
    patterns = default_excludes_for_code()
    assert ".git/**" not in patterns
    assert ".git/" not in patterns


def test_transient_git_files_excluded():
    patterns = default_excludes_for_code()
    assert ".git/index.lock" in patterns
    assert ".git/COMMIT_EDITMSG" in patterns
