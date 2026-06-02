"""Test isolation: previne escrita em ~/.cache/ real durante test_command.

Tests do daemon (`_mark_success`) e do status (`render_status`) leem `XDG_CACHE_HOME`
para resolver paths como `~/.cache/drive-sync/state/<fs_key>.success`. Sem
isolação, tests pollute o cache real do operador.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolate_xdg_cache(tmp_path, monkeypatch):
    """Autouse: redireciona XDG_CACHE_HOME para tmp_path em todo teste."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
