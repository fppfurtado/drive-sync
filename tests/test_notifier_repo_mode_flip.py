"""Tests for Notifier.repo_mode_flip + detect_repo_mode_flips (ADR-008)."""
from pathlib import Path
from unittest.mock import patch

from drive_sync.git_handler import RepoClassification, detect_repo_mode_flips
from drive_sync.notifier import Notifier


# ---------------------------------------------------------------------------
# Notifier.repo_mode_flip — emite notify-send (canal igual a folder_degraded)
# ---------------------------------------------------------------------------

def test_repo_mode_flip_emits_notify_send(monkeypatch):
    """notify-send disparado com title 'drive-sync: repo mode flip' + body legível.

    Asserts posicionais alinhados com signature `notify-send --urgency=critical SUMMARY BODY`
    para detectar regressão de ordem de args (ex.: alguém inserir flag novo no meio).
    """
    monkeypatch.setenv("DISPLAY", ":0")
    with patch("drive_sync.notifier.subprocess.run") as mock_run:
        Notifier().repo_mode_flip("dev-projects", "drive-sync", "skip", "bundle")
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "notify-send"
        assert cmd[1] == "--urgency=critical"
        assert cmd[2] == "drive-sync: repo mode flip"
        assert cmd[3] == "dev-projects/drive-sync: skip→bundle"
        assert len(cmd) == 4


def test_repo_mode_flip_root_repo_uses_label(monkeypatch):
    """Repo na raiz (repo_subpath='') → label '<root>' no body legível."""
    monkeypatch.setenv("DISPLAY", ":0")
    with patch("drive_sync.notifier.subprocess.run") as mock_run:
        Notifier().repo_mode_flip("dev-scripts", "", "bundle", "skip")
        cmd = mock_run.call_args[0][0]
        assert cmd[3] == "dev-scripts/<root>: bundle→skip"


def test_repo_mode_flip_skipped_when_headless(monkeypatch):
    """Sem DISPLAY nem DBUS_SESSION_BUS_ADDRESS → notify-send não é invocado."""
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    with patch("drive_sync.notifier.subprocess.run") as mock_run:
        Notifier().repo_mode_flip("dev", "x", "skip", "bundle")
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# detect_repo_mode_flips — semântica de estado in-memory
# ---------------------------------------------------------------------------

def _classification(subpath: str, mode: str) -> RepoClassification:
    return RepoClassification(
        repo_path=Path(f"/tmp/{subpath}"),
        repo_subpath=subpath,
        mode=mode,
        reason="no_remote" if mode == "bundle" else "has_remote",
        remote_url=None if mode == "bundle" else "git@github.com:fake/foo.git",
    )


def test_first_classification_post_restart_silent():
    """prev_state vazio (pós-restart) → retorna [] mesmo com classificações válidas."""
    current = [_classification("foo", "bundle")]
    flips = detect_repo_mode_flips("dev", {}, current)
    assert flips == []


def test_detect_flips_returns_changed_modes():
    """Mode mudou desde prev_state → flip emitido como (subpath, old, new)."""
    current = [_classification("foo", "bundle")]
    prev = {"foo": "skip"}
    flips = detect_repo_mode_flips("dev", prev, current)
    assert flips == [("foo", "skip", "bundle")]


def test_detect_flips_ignores_unchanged_modes():
    """Mode igual ao anterior → sem flip."""
    current = [_classification("foo", "skip")]
    prev = {"foo": "skip"}
    flips = detect_repo_mode_flips("dev", prev, current)
    assert flips == []


def test_detect_flips_ignores_new_repos():
    """Repo apareceu desde prev (novo no scan) → sem flip (não é mudança de mode)."""
    current = [_classification("foo", "bundle"), _classification("bar", "skip")]
    prev = {"foo": "bundle"}
    flips = detect_repo_mode_flips("dev", prev, current)
    assert flips == []


def test_detect_flips_ignores_removed_repos():
    """Repo sumiu do scan (presente em prev, ausente em current) → sem flip.

    Decisão: 'removido' não é flip de mode — é desaparecimento. Trade-off aceito:
    sem alerta de 'repo deletado' (operador usa journal para forense).
    """
    current = [_classification("foo", "skip")]
    prev = {"foo": "skip", "bar": "bundle"}  # bar sumiu
    flips = detect_repo_mode_flips("dev", prev, current)
    assert flips == []
