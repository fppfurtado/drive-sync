"""Tests for notifier — sd_notify socket protocol and notify-send subprocess."""
from __future__ import annotations

import socket as stdlib_socket
from unittest.mock import MagicMock

import pytest

from drive_sync.notifier import Notifier


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeSocket:
    """Stand-in capturing constructor args and sendto/settimeout calls."""

    def __init__(self, family=None, kind=None):
        self.family = family
        self.kind = kind
        self.sendto_calls: list[tuple[bytes, str]] = []
        self.timeouts: list[float] = []
        self.entered = False
        self.exited = False

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, *exc):
        self.exited = True

    def settimeout(self, value: float) -> None:
        self.timeouts.append(value)

    def sendto(self, data: bytes, addr) -> None:
        self.sendto_calls.append((data, addr))


@pytest.fixture
def fake_sockets(monkeypatch):
    """Replace socket.socket inside notifier with a recording factory."""
    instances: list[_FakeSocket] = []

    def factory(*args, **kwargs):
        s = _FakeSocket(*args, **kwargs)
        instances.append(s)
        return s

    monkeypatch.setattr("drive_sync.notifier.socket.socket", factory)
    return instances


@pytest.fixture
def isolated_env(monkeypatch):
    """Strip env vars that influence Notifier so each test sets only what it needs."""
    for var in ("NOTIFY_SOCKET", "DISPLAY", "DBUS_SESSION_BUS_ADDRESS"):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# ready() / degraded() send the right protocol payload
# ---------------------------------------------------------------------------

def test_ready_sends_ready_payload(monkeypatch, isolated_env, fake_sockets):
    monkeypatch.setenv("NOTIFY_SOCKET", "/run/notify.sock")
    Notifier().ready()

    assert len(fake_sockets) == 1
    sock = fake_sockets[0]
    assert sock.family == stdlib_socket.AF_UNIX
    assert sock.kind == stdlib_socket.SOCK_DGRAM
    assert sock.sendto_calls == [(b"READY=1\n", "/run/notify.sock")]
    assert sock.timeouts == [1.0]
    assert sock.entered is True and sock.exited is True


def test_degraded_sends_status_payload(monkeypatch, isolated_env, fake_sockets):
    monkeypatch.setenv("NOTIFY_SOCKET", "/run/notify.sock")
    Notifier().degraded("invalid_credentials (Code=8002)")

    assert len(fake_sockets) == 1
    assert fake_sockets[0].sendto_calls == [
        (b"STATUS=degraded: invalid_credentials (Code=8002)\n", "/run/notify.sock"),
    ]


def test_settimeout_set_before_sendto(monkeypatch, isolated_env):
    """Invariante de não-bloqueio: timeout aplicado antes do envio (plano §Resumo)."""
    monkeypatch.setenv("NOTIFY_SOCKET", "/run/notify.sock")

    order: list[str] = []

    class _OrderedSocket(_FakeSocket):
        def settimeout(self, v):
            order.append("settimeout")
            super().settimeout(v)

        def sendto(self, data, addr):
            order.append("sendto")
            super().sendto(data, addr)

    monkeypatch.setattr("drive_sync.notifier.socket.socket", _OrderedSocket)
    Notifier().ready()
    assert order == ["settimeout", "sendto"]


# ---------------------------------------------------------------------------
# degraded() also drives notify-send when DISPLAY is set
# ---------------------------------------------------------------------------

def test_degraded_fires_notify_send_when_display_set(
    monkeypatch, isolated_env, fake_sockets
):
    monkeypatch.setenv("NOTIFY_SOCKET", "/run/notify.sock")
    monkeypatch.setenv("DISPLAY", ":0")
    captured: list[list[str]] = []

    def fake_run(args, **kwargs):
        captured.append(args)
        return MagicMock()

    monkeypatch.setattr("drive_sync.notifier.subprocess.run", fake_run)
    Notifier().degraded("auth fail")

    assert len(captured) == 1
    assert captured[0][0] == "notify-send"
    assert "drive-sync degraded" in captured[0]
    assert "auth fail" in captured[0]


def test_degraded_skips_notify_send_in_headless(
    monkeypatch, isolated_env, fake_sockets
):
    """Sessão sem DISPLAY/DBUS — sd_notify segue, notify-send vira no-op."""
    monkeypatch.setenv("NOTIFY_SOCKET", "/run/notify.sock")
    captured: list = []
    monkeypatch.setattr(
        "drive_sync.notifier.subprocess.run",
        lambda *a, **kw: captured.append(a) or MagicMock(),
    )

    Notifier().degraded("foo")
    assert captured == []
    assert len(fake_sockets[0].sendto_calls) == 1


def test_degraded_fires_notify_send_with_only_dbus_set(
    monkeypatch, isolated_env, fake_sockets
):
    """Wayland headless: DBUS sem DISPLAY também é suficiente para notify-send."""
    monkeypatch.setenv("NOTIFY_SOCKET", "/run/notify.sock")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")
    captured: list = []
    monkeypatch.setattr(
        "drive_sync.notifier.subprocess.run",
        lambda args, **kw: captured.append(args) or MagicMock(),
    )

    Notifier().degraded("foo")
    assert len(captured) == 1 and captured[0][0] == "notify-send"


# ---------------------------------------------------------------------------
# No-op caminhos
# ---------------------------------------------------------------------------

def test_no_notify_socket_does_not_open_socket(monkeypatch, isolated_env, fake_sockets):
    Notifier().ready()
    assert fake_sockets == []


def test_empty_notify_socket_does_not_open_socket(
    monkeypatch, isolated_env, fake_sockets
):
    """NOTIFY_SOCKET vazio é tratado como ausente."""
    monkeypatch.setenv("NOTIFY_SOCKET", "")
    Notifier().ready()
    assert fake_sockets == []


# ---------------------------------------------------------------------------
# Linux abstract namespace
# ---------------------------------------------------------------------------

def test_abstract_socket_translates_at_to_nul(monkeypatch, isolated_env, fake_sockets):
    monkeypatch.setenv("NOTIFY_SOCKET", "@my-abstract")
    Notifier().ready()

    assert fake_sockets[0].sendto_calls == [(b"READY=1\n", "\0my-abstract")]


# ---------------------------------------------------------------------------
# Erro de socket é best-effort silencioso
# ---------------------------------------------------------------------------

def test_systemd_notify_does_not_spawn_subprocess(monkeypatch, isolated_env, fake_sockets):
    """Contrato ADR-003: sender é o MainPID, não child de subprocess.

    Proxy observável: nenhum `subprocess.run` é invocado pelo caminho do sd_notify.
    Regressão para `subprocess.run(["systemd-notify", ...])` (o bug original) ou
    qualquer outro fork-helper seria capturada aqui.
    """
    monkeypatch.setenv("NOTIFY_SOCKET", "/run/notify.sock")
    subprocess_calls: list = []
    monkeypatch.setattr(
        "drive_sync.notifier.subprocess.run",
        lambda *a, **kw: subprocess_calls.append(a) or MagicMock(),
    )

    Notifier().ready()
    assert subprocess_calls == []


def test_socket_oserror_is_silenced(monkeypatch, isolated_env):
    monkeypatch.setenv("NOTIFY_SOCKET", "/run/notify.sock")

    class _RaisingSocket:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def settimeout(self, v):
            pass

        def sendto(self, *a, **kw):
            raise OSError(13, "Permission denied")

    monkeypatch.setattr("drive_sync.notifier.socket.socket", _RaisingSocket)

    Notifier().ready()  # não deve propagar


def test_socket_timeout_is_silenced(monkeypatch, isolated_env):
    """TimeoutError herda de OSError e cai no mesmo except."""
    monkeypatch.setenv("NOTIFY_SOCKET", "/run/notify.sock")

    class _TimingOutSocket:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def settimeout(self, v):
            pass

        def sendto(self, *a, **kw):
            raise TimeoutError("connect timed out")

    monkeypatch.setattr("drive_sync.notifier.socket.socket", _TimingOutSocket)

    Notifier().ready()
