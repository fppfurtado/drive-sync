"""Tests for watcher.owning_folder — the sub-path deduplication logic."""
from pathlib import Path

from drive_sync.config import FolderConfig
from drive_sync.watcher import owning_folder


def _folder(name: str, path: str) -> FolderConfig:
    return FolderConfig(name=name, local_path=Path(path), remote_subpath=name)


def test_single_folder_matches_child_path():
    folders = [_folder("a", "/home/user/A")]
    result = owning_folder(Path("/home/user/A/file.txt"), folders)
    assert result is not None
    assert result.name == "a"


def test_picks_most_specific_ancestor():
    folders = [
        _folder("parent", "/home/user/A"),
        _folder("child", "/home/user/A/B"),
    ]
    result = owning_folder(Path("/home/user/A/B/file.txt"), folders)
    assert result is not None
    assert result.name == "child"


def test_picks_parent_when_path_not_under_child():
    folders = [
        _folder("parent", "/home/user/A"),
        _folder("child", "/home/user/A/B"),
    ]
    result = owning_folder(Path("/home/user/A/other.txt"), folders)
    assert result is not None
    assert result.name == "parent"


def test_no_match_returns_none():
    folders = [_folder("a", "/home/user/A")]
    result = owning_folder(Path("/home/user/B/file.txt"), folders)
    assert result is None


def test_empty_folder_list_returns_none():
    result = owning_folder(Path("/home/user/A/file.txt"), [])
    assert result is None


def test_exact_root_path_matches():
    folders = [_folder("a", "/home/user/A")]
    result = owning_folder(Path("/home/user/A"), folders)
    assert result is not None
    assert result.name == "a"


def test_sibling_folders_do_not_cross_match():
    folders = [
        _folder("alpha", "/home/user/alpha"),
        _folder("beta", "/home/user/beta"),
    ]
    assert owning_folder(Path("/home/user/alpha/x"), folders).name == "alpha"
    assert owning_folder(Path("/home/user/beta/x"), folders).name == "beta"
