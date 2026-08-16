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


# ---------------------------------------------------------------------------
# _DebouncingHandler — eventos de leitura não são mudança (#29)
# ---------------------------------------------------------------------------

def _make_handler():
    from unittest.mock import MagicMock
    from drive_sync.watcher import _DebouncingHandler
    folder = _folder("a", "/home/user/A")
    folder.debounce_seconds = 40
    return _DebouncingHandler(
        folder=folder, all_folders=[folder],
        loop=MagicMock(), queue=MagicMock(), dedupe_subpaths=False,
    )


def test_opened_event_is_ignored():
    from watchdog.events import FileOpenedEvent
    h = _make_handler()
    h.on_any_event(FileOpenedEvent("/home/user/A/.git/config"))
    assert h._timer is None


def test_closed_no_write_event_is_ignored():
    from watchdog.events import FileClosedNoWriteEvent
    h = _make_handler()
    h.on_any_event(FileClosedNoWriteEvent("/home/user/A/.git/HEAD"))
    assert h._timer is None


def test_modified_event_schedules_debounce():
    from watchdog.events import FileModifiedEvent
    h = _make_handler()
    h.on_any_event(FileModifiedEvent("/home/user/A/x.txt"))
    assert h._timer is not None
    h._timer.cancel()


def test_closed_after_write_event_schedules_debounce():
    from watchdog.events import FileClosedEvent
    h = _make_handler()
    h.on_any_event(FileClosedEvent("/home/user/A/x.txt"))
    assert h._timer is not None
    h._timer.cancel()
