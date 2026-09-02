"""Tests for sync_engine — remote URI building and bisync behaviour."""
import asyncio
import logging
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from drive_sync.config import (
    AppConfig,
    CoverageAuditConfig,
    DedupeConfig,
    FolderConfig,
    GitConfig,
    HealthCheckConfig,
    LoggingConfig,
    RcloneConfig,
    WatcherConfig,
)
from drive_sync.sync_engine import (
    _DEFAULT_INFRA_STORM_THRESHOLD,
    _DEFAULT_INFRA_WINDOW_SECONDS,
    AuthDegradedError,
    RcloneEngine,
    _classify_rclone_stderr,
    _is_stale_listings,
    _is_too_many_deletes,
    _dryrun_resync_is_noop,
    _state_marker_for,
    _configure_infra_detection,
    _infra_storm_active,
    _infra_window,
    _record_infra_signals,
    _reset_infra_window,
    _run,
    _state_marker_for,
    remote_uri_for,
)


@pytest.fixture(autouse=True)
def _isolate_infra_state():
    # Estado module-level (janela de 5xx + config do detector) persiste entre
    # testes; reseta para não vazar storm/threshold de um teste para outro.
    _reset_infra_window()
    _configure_infra_detection(
        _DEFAULT_INFRA_STORM_THRESHOLD, _DEFAULT_INFRA_WINDOW_SECONDS
    )
    yield
    _reset_infra_window()
    _configure_infra_detection(
        _DEFAULT_INFRA_STORM_THRESHOLD, _DEFAULT_INFRA_WINDOW_SECONDS
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _app(remote_name: str = "proton", remote_root: str = "Sync") -> AppConfig:
    return AppConfig(
        rclone=RcloneConfig(remote_name=remote_name, remote_root=remote_root),
        folders=[],
        git=GitConfig(),
        watcher=WatcherConfig(),
        dedupe=DedupeConfig(),
        health_check=HealthCheckConfig(),
        logging=LoggingConfig(),
        coverage_audit=CoverageAuditConfig(),
        source_path=Path("/fake/config.yaml"),
    )


def _folder(name: str = "docs", remote_subpath: str = "Documents", *, auto_exclude: bool = False, local_path: Path | None = None) -> FolderConfig:
    return FolderConfig(
        name=name,
        local_path=local_path or Path(f"/tmp/{name}"),
        remote_subpath=remote_subpath,
        auto_exclude=auto_exclude,
    )


async def _fake_ok(cmd: list[str], timeout: float | None = None) -> tuple[int, str, str]:
    return (0, "", "")


async def _fake_err(cmd: list[str], timeout: float | None = None) -> tuple[int, str, str]:
    return (1, "", "rclone: something went wrong")


def _bisync_calls(captured: list[list[str]]) -> list[list[str]]:
    """Filtra apenas as chamadas de bisync (exclui mkdir)."""
    return [c for c in captured if "bisync" in c]


def _mkdir_calls(captured: list[list[str]]) -> list[list[str]]:
    return [c for c in captured if "mkdir" in c]


def _carries_empty_2fa(cmd: list[str]) -> bool:
    """True se o cmd passa `--protondrive-2fa` seguido de string vazia (#61/SP-T7)."""
    for i, tok in enumerate(cmd):
        if tok == "--protondrive-2fa":
            return i + 1 < len(cmd) and cmd[i + 1] == ""
    return False


# ---------------------------------------------------------------------------
# remote_uri_for
# ---------------------------------------------------------------------------

def test_remote_uri_basic():
    uri = remote_uri_for(_folder(remote_subpath="Documents"), _app(remote_name="proton", remote_root="Sync"))
    assert uri == "proton:Sync/Documents"


def test_remote_uri_with_sub():
    uri = remote_uri_for(_folder(remote_subpath="Code"), _app(remote_name="drive", remote_root="Root"), sub="repo.gitbundle")
    assert uri == "drive:Root/Code/repo.gitbundle"


def test_remote_uri_sub_none_omitted():
    uri = remote_uri_for(_folder(remote_subpath="Photos"), _app(), sub=None)
    assert uri == "proton:Sync/Photos"


def test_remote_uri_strips_extra_slashes():
    app = _app(remote_root="Sync")
    folder = FolderConfig(name="x", local_path=Path("/tmp/x"), remote_subpath="nested/path")
    assert remote_uri_for(folder, app) == "proton:Sync/nested/path"


# ---------------------------------------------------------------------------
# bisync_folder — first run adds --resync, subsequent run does not
# ---------------------------------------------------------------------------

async def test_first_run_adds_resync_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    app = _app()
    engine = RcloneEngine(app)
    folder = _folder(local_path=tmp_path / "local")
    captured: list[list[str]] = []

    async def fake_run(cmd, timeout=None):
        captured.append(cmd)
        return (0, "", "")

    with patch("drive_sync.sync_engine._run", fake_run):
        result = await engine.bisync_folder(folder)

    assert result is True
    bisync_cmds = _bisync_calls(captured)
    assert len(bisync_cmds) == 1
    assert "--resync" in bisync_cmds[0]


async def test_subsequent_run_omits_resync(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    app = _app()
    engine = RcloneEngine(app)
    folder = _folder(local_path=tmp_path / "local")

    remote = remote_uri_for(folder, app)
    marker = _state_marker_for(folder.local_path, remote)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()

    captured: list[list[str]] = []

    async def fake_run(cmd, timeout=None):
        captured.append(cmd)
        return (0, "", "")

    with patch("drive_sync.sync_engine._run", fake_run):
        result = await engine.bisync_folder(folder)

    assert result is True
    bisync_cmds = _bisync_calls(captured)
    assert len(bisync_cmds) == 1
    assert "--resync" not in bisync_cmds[0]


async def test_failed_bisync_returns_false(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    engine = RcloneEngine(_app())
    folder = _folder(local_path=tmp_path / "local")

    # mkdir succeeds, bisync fails
    async def fake_run(cmd, timeout=None):
        if "mkdir" in cmd:
            return (0, "", "")
        return (1, "", "rclone: something went wrong")

    with patch("drive_sync.sync_engine._run", fake_run):
        result = await engine.bisync_folder(folder)

    assert result is False


async def test_success_creates_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    app = _app()
    engine = RcloneEngine(app)
    folder = _folder(local_path=tmp_path / "local")

    with patch("drive_sync.sync_engine._run", _fake_ok):
        await engine.bisync_folder(folder)

    marker = _state_marker_for(folder.local_path, remote_uri_for(folder, app))
    assert marker.exists()


async def test_failure_does_not_create_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    app = _app()
    engine = RcloneEngine(app)
    folder = _folder(local_path=tmp_path / "local")

    with patch("drive_sync.sync_engine._run", _fake_err):
        await engine.bisync_folder(folder)

    marker = _state_marker_for(folder.local_path, remote_uri_for(folder, app))
    assert not marker.exists()


async def test_auto_exclude_appends_preset_patterns(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    engine = RcloneEngine(_app())
    folder = _folder(local_path=tmp_path / "code", auto_exclude=True)
    captured: list[list[str]] = []

    async def fake_run(cmd, timeout=None):
        captured.append(cmd)
        return (0, "", "")

    with patch("drive_sync.sync_engine._run", fake_run):
        await engine.bisync_folder(folder)

    cmd = _bisync_calls(captured)[0]
    assert "--exclude" in cmd
    assert "node_modules/**" in cmd


async def test_auto_exclude_false_skips_presets(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    engine = RcloneEngine(_app())
    folder = _folder(local_path=tmp_path / "code", auto_exclude=False)
    captured: list[list[str]] = []

    async def fake_run(cmd, timeout=None):
        captured.append(cmd)
        return (0, "", "")

    with patch("drive_sync.sync_engine._run", fake_run):
        await engine.bisync_folder(folder)

    cmd = _bisync_calls(captured)[0]
    assert "node_modules/**" not in cmd


# ---------------------------------------------------------------------------
# _ensure_remote_dir — mkdir é chamado antes do bisync
# ---------------------------------------------------------------------------

async def test_mkdir_called_before_bisync(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    engine = RcloneEngine(_app())
    folder = _folder(local_path=tmp_path / "local")
    captured: list[list[str]] = []

    async def fake_run(cmd, timeout=None):
        captured.append(cmd)
        return (0, "", "")

    with patch("drive_sync.sync_engine._run", fake_run):
        await engine.bisync_folder(folder)

    assert len(_mkdir_calls(captured)) == 1
    assert captured.index(_mkdir_calls(captured)[0]) < captured.index(_bisync_calls(captured)[0])


async def test_mkdir_failure_aborts_bisync(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    engine = RcloneEngine(_app())
    folder = _folder(local_path=tmp_path / "local")
    captured: list[list[str]] = []

    async def fake_run(cmd, timeout=None):
        captured.append(cmd)
        if "mkdir" in cmd:
            return (1, "", "rclone: mkdir failed")
        return (0, "", "")

    with patch("drive_sync.sync_engine._run", fake_run):
        result = await engine.bisync_folder(folder)

    assert result is False
    assert len(_bisync_calls(captured)) == 0


async def test_mkdir_uses_correct_remote(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    app = _app(remote_name="proton", remote_root="Sync")
    engine = RcloneEngine(app)
    folder = _folder(remote_subpath="dev/projects", local_path=tmp_path / "local")
    captured: list[list[str]] = []

    async def fake_run(cmd, timeout=None):
        captured.append(cmd)
        return (0, "", "")

    with patch("drive_sync.sync_engine._run", fake_run):
        await engine.bisync_folder(folder)

    mkdir_cmd = _mkdir_calls(captured)[0]
    assert "proton:Sync/dev/projects" in mkdir_cmd


# ---------------------------------------------------------------------------
# _run — serialização de chamadas concorrentes (ADR-001, rclone#7381)
# ---------------------------------------------------------------------------

async def test_run_serializes_concurrent_calls(monkeypatch):
    """Duas chamadas concorrentes a _run não se sobrepõem temporalmente."""
    intervals: list[tuple[float, float]] = []

    class _FakeProc:
        returncode = 0

        async def communicate(self):
            start = time.perf_counter()
            await asyncio.sleep(0.05)
            end = time.perf_counter()
            intervals.append((start, end))
            return (b"", b"")

    async def fake_subprocess_exec(*args, **kwargs):
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess_exec)

    await asyncio.gather(_run(["rclone", "one"]), _run(["rclone", "two"]))

    assert len(intervals) == 2
    (start1, end1), (start2, end2) = sorted(intervals)
    assert start2 >= end1 - 0.010, (
        f"Chamadas concorrentes se sobrepõem: "
        f"call1=({start1:.4f},{end1:.4f}), call2=({start2:.4f},{end2:.4f})"
    )


async def test_run_releases_lock_on_subprocess_exception(monkeypatch):
    """Exceção dentro do `async with` libera o lock — chamada seguinte não trava."""
    call_count = {"n": 0}

    async def fake_subprocess_exec(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise OSError("rclone binary missing")

        class _FakeProc:
            returncode = 0

            async def communicate(self):
                return (b"", b"")

        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess_exec)

    with pytest.raises(OSError):
        await _run(["rclone", "first"])

    rc, _out, _err = await asyncio.wait_for(_run(["rclone", "second"]), timeout=1.0)
    assert rc == 0


# ---------------------------------------------------------------------------
# _classify_rclone_stderr — detecção de falha de auth conhecida
# ---------------------------------------------------------------------------

# Amostras reais — preservar literais.
# Path histórico (2026-05-11): /api/auth/v4/...
_STDERR_8002 = (
    'CRITICAL: Failed to create file system for "proton:Sync/dev/projects": '
    "couldn't initialize a new proton drive instance: 422 POST "
    "https://mail.proton.me/api/auth/v4/2fa: Incorrect login credentials. "
    "Please try again. (Code=8002, Status=422)"
)
_STDERR_9001 = (
    'CRITICAL: Failed to create file system for "proton:Sync/library": '
    "couldn't initialize a new proton drive instance: 422 POST "
    "https://mail.proton.me/api/auth/v4: For security reasons, please complete "
    "CAPTCHA. (Code=9001, Status=422)"
)
# Path atual (2026-05-24): /auth/v4/... — Proton mudou sem aviso.
_STDERR_8002_NEW_PATH = (
    'CRITICAL: Failed to create file system for "proton:Sync/dev/projects": '
    "422 POST https://drive-api.proton.me/auth/v4/2fa: Incorrect login credentials. "
    "(Code=8002, Status=422)"
)
_STDERR_10013 = (
    "Bisync critical error: march failed with 5 error(s): first error: failed to "
    "refresh auth: failed to refresh auth, de-auth: 400 POST "
    "https://drive-api.proton.me/auth/v4/refresh: Invalid refresh token "
    "(Code=10013, Status=400)"
)
_STDERR_2028 = (
    "Failed to mkdir: couldn't create directory: 422 POST "
    "https://i.proton.me/auth/v4: Our systems detected unusual activity targeting "
    "your account. To protect you from potential compromise, we have temporarily "
    "limited access to it. If this persists or you believe this is in error, "
    "please contact us at https://proton.me/support/appeal-abuse "
    "(Code=2028, Status=422)"
)


def test_classify_invalid_credentials_8002():
    err = _classify_rclone_stderr(_STDERR_8002)
    assert err is not None
    assert err.kind == "invalid_credentials"
    assert err.code == 8002
    assert "Code=8002" in err.stderr_tail


def test_classify_captcha_required_9001():
    err = _classify_rclone_stderr(_STDERR_9001)
    assert err is not None
    assert err.kind == "captcha_required"
    assert err.code == 9001
    assert "Code=9001" in err.stderr_tail


def test_classify_refresh_token_invalid_10013():
    # Caso real do incidente 2026-05-24 16:58 — antes da extensão do classificador,
    # este par retornava None e o daemon entrava em loop frenético sem pause.
    err = _classify_rclone_stderr(_STDERR_10013)
    assert err is not None
    assert err.kind == "refresh_token_invalid"
    assert err.code == 10013
    assert "Code=10013" in err.stderr_tail


def test_classify_rate_limited_2028():
    # Caso real do incidente 2026-05-24 17:01 — CAPTCHA gate / suspect activity.
    err = _classify_rclone_stderr(_STDERR_2028)
    assert err is not None
    assert err.kind == "rate_limited"
    assert err.code == 2028
    assert "appeal-abuse" in err.stderr_tail


def test_classify_endpoint_without_api_prefix():
    # Bug latente revelado em 2026-05-24: Proton mudou de /api/auth/v4 para /auth/v4
    # sem aviso. Antes da correção do endpoint matcher, mesmo Code=8002 escapava.
    err = _classify_rclone_stderr(_STDERR_8002_NEW_PATH)
    assert err is not None
    assert err.kind == "invalid_credentials"
    assert err.code == 8002


def test_classify_returns_none_for_unknown_code_status_pair():
    # Substituto da invariante negativa perdida ao inverter
    # test_classify_returns_none_for_other_status_code: par fora de _AUTH_CODES
    # deve retornar None mesmo em endpoint de auth válido.
    stderr = "POST https://drive-api.proton.me/auth/v4 (Code=9999, Status=422)"
    assert _classify_rclone_stderr(stderr) is None


def test_classify_robust_to_noisy_pairs_in_stderr():
    # Validação direta da escolha do regex composto sobre genérico + lookup:
    # par ruidoso antes do par alvo no stderr não deve silenciar o classificador.
    stderr = (
        "socket: connection reset (Code=9999, Status=500); "
        "auth retry: https://drive-api.proton.me/auth/v4 (Code=8002, Status=422)"
    )
    err = _classify_rclone_stderr(stderr)
    assert err is not None
    assert err.kind == "invalid_credentials"
    assert err.code == 8002


def test_classify_picks_first_known_pair_when_multiple_present():
    # Documenta first-match wins quando ≥2 pares alvo aparecem no mesmo stderr.
    # Regressão se alguém trocar `re.search` por `re.findall + last`.
    stderr = (
        "https://drive-api.proton.me/auth/v4 (Code=8002, Status=422); "
        "retry: (Code=10013, Status=400)"
    )
    err = _classify_rclone_stderr(stderr)
    assert err is not None
    assert err.code == 8002


def test_classify_endpoint_and_pair_in_disjoint_contexts():
    # Design: classificador é permissivo — endpoint e par alvo validados por
    # `re.search` independentes; rclone não emite stderr ambíguo na prática,
    # então adjacência não é exigida. Teste documenta a invariante via assert.
    stderr = (
        "GET https://drive-api.proton.me/auth/v4/info: ok. "
        "Later error from /drive/v2/list (Code=8002, Status=422)"
    )
    err = _classify_rclone_stderr(stderr)
    assert err is not None
    assert err.kind == "invalid_credentials"


def test_classify_returns_none_for_non_auth_error():
    assert _classify_rclone_stderr("rclone: directory not found") is None


def test_classify_returns_none_without_full_anchor():
    # Substring "Code=8002" sem o "(...Status=422)" completo — não casa.
    assert _classify_rclone_stderr("logged Code=8002 somewhere /api/auth/v4") is None


def test_classify_returns_none_without_auth_endpoint():
    # Códigos batem mas o path não contém /auth/v4 — descarta.
    stderr = "POST https://api.proton.me/drive/v2/foo (Code=8002, Status=422)"
    assert _classify_rclone_stderr(stderr) is None


# ---------------------------------------------------------------------------
# _is_stale_listings — assinatura rc=7 recuperável por --resync (SP-T2 · #47)
# ---------------------------------------------------------------------------

# Amostra real (incidente library 2026-08-27) — preservar literal.
_STDERR_STALE_LISTINGS = (
    "ERROR : Bisync critical error: cannot find prior Path1 or Path2 listings, "
    "likely due to critical error on prior run\n"
    "ERROR : Bisync aborted. Must run --resync to recover."
)


def test_is_stale_listings_matches_real_signature():
    assert _is_stale_listings(_STDERR_STALE_LISTINGS) is True


def test_is_stale_listings_matches_either_phrase_alone():
    # Robustez: qualquer uma das duas frases-âncora basta.
    assert _is_stale_listings("cannot find prior Path1 or Path2 listings") is True
    assert _is_stale_listings("Bisync aborted. Must run --resync to recover.") is True


def test_is_stale_listings_false_for_case_duplicates_rc7():
    # OUTRA causa de rc=7 (colisão case-insensitive, ADR-011) NÃO deve casar —
    # discriminação é o ponto: o abort de case-duplicates não é auto-recuperável
    # por --resync (exige cleanup no FS pelo operador).
    stderr = (
        "ERROR : Bisync critical error: rclone aborted: found case-insensitive "
        "duplicate names (family/ vs Family/) — resolve before syncing"
    )
    assert _is_stale_listings(stderr) is False


def test_is_stale_listings_false_for_generic_bisync_error():
    assert _is_stale_listings("ERROR : too many deletes (>50%) — Safety abort") is False
    assert _is_stale_listings("rclone: directory not found") is False


# ---------------------------------------------------------------------------
# _is_too_many_deletes — assinatura rc=1 too-many-deletes (advice safe · #52)
# ---------------------------------------------------------------------------

# Amostra real (incidente dev-projects 2026-05-31, ADR-012) — preservar literal.
_STDERR_TOO_MANY_DELETES = (
    "ERROR : Safety abort: too many deletes (>50%, 68412 of 104018) on Path1 "
    '"/storage/dev/projects/". Run with --force if desired'
)


def test_is_too_many_deletes_matches_real_signature():
    assert _is_too_many_deletes(_STDERR_TOO_MANY_DELETES) is True


def test_is_too_many_deletes_false_for_stale_listings_rc7():
    # A assinatura irmã (rc=7 stale-listings, benigno/auto-recuperável) NÃO deve
    # casar — discriminação é o ponto: too-many-deletes é divergência real e
    # perigosa, não pode herdar o caminho de auto-resync do rc=7.
    assert _is_too_many_deletes(_STDERR_STALE_LISTINGS) is False


def test_is_too_many_deletes_false_for_generic_bisync_error():
    assert _is_too_many_deletes("rclone: directory not found") is False
    assert _is_too_many_deletes("") is False


# ---------------------------------------------------------------------------
# bisync_folder — advice safe [BISYNC_SAFETY_ABORT] no rc=1 too-many-deletes (#52)
# ---------------------------------------------------------------------------

async def test_too_many_deletes_emits_safety_abort_advice(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    engine = RcloneEngine(_app())
    folder = _folder(local_path=tmp_path / "local")

    async def fake_run(cmd, timeout=None):
        if "mkdir" in cmd:
            return (0, "", "")
        return (1, "", _STDERR_TOO_MANY_DELETES)

    with caplog.at_level(logging.WARNING, logger="drive_sync.sync_engine"):
        with patch("drive_sync.sync_engine._run", fake_run):
            result = await engine.bisync_folder(folder)

    # Sinalização, não recuperação: o folder segue degradado (invariante preservado).
    assert result is False
    safety = [r for r in caplog.records if "[BISYNC_SAFETY_ABORT]" in r.getMessage()]
    assert len(safety) == 1
    msg = safety[0].getMessage()
    # O advice NÃO ecoa a dica `--force` cega e aponta pro branch rc=1 do playbook.
    assert "playbook-bisync-recovery.md" in msg
    assert "PERDA DE DADOS" in msg


async def test_generic_bisync_error_no_safety_abort_advice(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    engine = RcloneEngine(_app())
    folder = _folder(local_path=tmp_path / "local")

    async def fake_run(cmd, timeout=None):
        if "mkdir" in cmd:
            return (0, "", "")
        return (1, "", "rclone: something unrelated went wrong")

    with caplog.at_level(logging.WARNING, logger="drive_sync.sync_engine"):
        with patch("drive_sync.sync_engine._run", fake_run):
            result = await engine.bisync_folder(folder)

    assert result is False
    assert not [r for r in caplog.records if "[BISYNC_SAFETY_ABORT]" in r.getMessage()]


# ---------------------------------------------------------------------------
# _run levanta AuthDegradedError quando stderr matcha
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "stderr_sample,expected_code,expected_kind",
    [
        (_STDERR_8002, 8002, "invalid_credentials"),
        (_STDERR_9001, 9001, "captcha_required"),
        (_STDERR_10013, 10013, "refresh_token_invalid"),
        (_STDERR_2028, 2028, "rate_limited"),
    ],
)
async def test_run_raises_auth_degraded_on_matching_stderr(
    monkeypatch, stderr_sample, expected_code, expected_kind
):
    class _FakeProc:
        returncode = 1

        async def communicate(self):
            return (b"", stderr_sample.encode("utf-8"))

    async def fake_exec(*args, **kwargs):
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(AuthDegradedError) as excinfo:
        await _run(["rclone", "mkdir", "proton:Sync/dev/projects"])

    assert excinfo.value.code == expected_code
    assert excinfo.value.kind == expected_kind


async def test_run_does_not_raise_on_non_auth_failure(monkeypatch):
    class _FakeProc:
        returncode = 1

        async def communicate(self):
            return (b"", b"rclone: directory not found")

    async def fake_exec(*args, **kwargs):
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    rc, _out, err = await _run(["rclone", "lsd", "proton:nonexistent"])
    assert rc == 1
    assert "not found" in err


# ---------------------------------------------------------------------------
# AuthDegradedError propaga via _ensure_remote_dir e bisync_folder
# ---------------------------------------------------------------------------

async def test_bisync_folder_propagates_auth_error(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    engine = RcloneEngine(_app())
    folder = _folder(local_path=tmp_path / "local")

    async def fake_run(cmd, timeout=None):
        raise AuthDegradedError(kind="invalid_credentials", code=8002, stderr_tail="...")

    with patch("drive_sync.sync_engine._run", fake_run):
        with pytest.raises(AuthDegradedError):
            await engine.bisync_folder(folder)


# ---------------------------------------------------------------------------
# auth_probe — propaga AuthDegradedError, silencia outros erros
# ---------------------------------------------------------------------------

async def test_auth_probe_propagates_auth_error():
    engine = RcloneEngine(_app())

    async def fake_run(cmd, timeout=None):
        raise AuthDegradedError(kind="captcha_required", code=9001, stderr_tail="...")

    with patch("drive_sync.sync_engine._run", fake_run):
        with pytest.raises(AuthDegradedError):
            await engine.auth_probe()


async def test_auth_probe_silences_non_auth_error():
    engine = RcloneEngine(_app())

    async def fake_run(cmd, timeout=None):
        raise OSError("network unreachable")

    with patch("drive_sync.sync_engine._run", fake_run):
        await engine.auth_probe()  # não deve levantar


# ---------------------------------------------------------------------------
# SP-T2 — janela de flakiness transitória (_record_infra_signals) · #35 + #46
# ---------------------------------------------------------------------------


def test_record_infra_signals_registers_one_timestamp_per_5xx():
    # EARS SP-T2: WHEN um _run retorna rc≠0 com N ocorrências Status=5xx no
    # stderr, the system SHALL registrar N timestamps na janela.
    _reset_infra_window()
    stderr = (
        "503 GET https://drive-api.proton.me/core/v4/users: 503 Service "
        "Unavailable (Code=0, Status=503)\n"
        "502 POST https://drive-api.proton.me/auth/v4/info: 502 Bad Gateway "
        "(Code=0, Status=502)\n"
        "500 GET https://zrh-storage.proton.me/storage/blocks: Internal server "
        "error (Code=500, Status=500)"
    )
    n = _record_infra_signals(stderr)
    assert n == 3
    assert len(_infra_window) == 3


def test_record_infra_signals_ignores_non_5xx():
    # Par auth 8002/422 (não-5xx) não polui a janela.
    _reset_infra_window()
    stderr = "422 POST https://drive-api.proton.me/auth/v4 (Code=8002, Status=422)"
    n = _record_infra_signals(stderr)
    assert n == 0
    assert len(_infra_window) == 0


def test_record_infra_signals_records_empty_upload_2003():
    # EARS SP-T8 (#79/F8): WHEN um _run retorna rc≠0 cujo stderr contém
    # `400 Code=2003 Upload file empty`, SHALL registrar um timestamp na janela
    # (como faz para Status=5xx) — é um sinal de storm apesar de ser 400.
    _reset_infra_window()
    stderr = (
        "400 POST https://zrh-storage.proton.me/storage/blocks: Upload file "
        "empty (Code=2003, Status=400)"
    )
    n = _record_infra_signals(stderr)
    assert n == 1
    assert len(_infra_window) == 1


def test_record_infra_signals_counts_5xx_and_2003_together():
    # 5xx e 2003 co-ocorrendo no mesmo stderr somam.
    _reset_infra_window()
    stderr = (
        "502 POST .../storage/blocks (Code=0, Status=502)\n"
        "400 POST .../storage/blocks: Upload file empty (Code=2003, Status=400)"
    )
    n = _record_infra_signals(stderr)
    assert n == 2
    assert len(_infra_window) == 2


# ---------------------------------------------------------------------------
# SP-T3 — classificação context-aware (par auth × storm de 5xx) · #35 + #46
# ---------------------------------------------------------------------------


def test_classify_8002_during_storm_is_proton_infra():
    # EARS SP-T3: IF um par auth casa E count(janela) >= threshold, THEN
    # classificar kind=proton_infra (colateral da flakiness transitória).
    _configure_infra_detection(threshold=3, window_seconds=600.0)
    for _ in range(3):
        _record_infra_signals("... 503 Service Unavailable (Code=0, Status=503)")
    err = _classify_rclone_stderr(_STDERR_8002)
    assert err is not None
    assert err.kind == "proton_infra"
    assert err.code == 8002  # o code original é preservado


def test_classify_8002_isolated_stays_invalid_credentials():
    # EARS SP-T3: IF a janela está abaixo do threshold, THEN manter o kind de
    # credencial (8002 isolado, sem storm precedente).
    _configure_infra_detection(threshold=3, window_seconds=600.0)
    err = _classify_rclone_stderr(_STDERR_8002)
    assert err is not None
    assert err.kind == "invalid_credentials"


def test_classify_storm_below_threshold_stays_credential():
    # Storm parcial (abaixo do threshold) não reclassifica.
    _configure_infra_detection(threshold=5, window_seconds=600.0)
    for _ in range(4):
        _record_infra_signals("... (Code=0, Status=502)")
    err = _classify_rclone_stderr(_STDERR_8002)
    assert err.kind == "invalid_credentials"


def test_infra_window_prunes_expired_entries():
    # F2 (Review): a janela é DESLIZANTE — entradas mais velhas que
    # window_seconds são podadas por _infra_storm_active. Sem a poda, um storm
    # antigo contaria para sempre.
    _reset_infra_window()
    _configure_infra_detection(threshold=1, window_seconds=600.0)
    _infra_window.append(time.monotonic() - 10_000)  # entrada expirada
    assert _infra_storm_active() is False  # podada → 0 >= 1 é False
    _record_infra_signals("… 503 Service Unavailable (Code=0, Status=503)")
    assert _infra_storm_active() is True  # entrada fresca conta → 1 >= 1


# ---------------------------------------------------------------------------
# SP-T9 — gatilho signature-gated (storm-colateral SEM par _AUTH_CODES) · #79
# ---------------------------------------------------------------------------

# Assinaturas de storm que NÃO casam _AUTH_CODES (o gap do F7):
_STDERR_RAW_500_AUTH = (
    "500 POST https://drive-api.proton.me/auth/v4/info: Internal server error "
    "(Code=0, Status=500)"
)
_STDERR_502_BLOCK = (
    "502 POST https://zrh-storage.proton.me/storage/blocks: 502 Bad Gateway "
    "(Code=0, Status=502)"
)
_STDERR_2003_UPLOAD = (
    "400 POST https://zrh-storage.proton.me/storage/blocks: Upload file empty "
    "(Code=2003, Status=400)"
)


def _arm_storm(n: int = 3) -> None:
    _configure_infra_detection(threshold=n, window_seconds=600.0)
    for _ in range(n):
        _record_infra_signals("… 503 Service Unavailable (Code=0, Status=503)")


def test_classify_raw_500_auth_during_storm_is_proton_infra():
    # EARS SP-T9: storm ativo + assinatura de storm (5xx cru em /auth/v4, SEM
    # par _AUTH_CODES) → proton_infra. Antes do #79 isto era registrado na
    # janela mas nunca disparava (F7).
    _arm_storm()
    err = _classify_rclone_stderr(_STDERR_RAW_500_AUTH)
    assert err is not None
    assert err.kind == "proton_infra"


def test_classify_block_502_during_storm_is_proton_infra():
    # EARS SP-T9 (fecha F5/#46): block 5xx endpoint-agnóstico dispara o back-off.
    _arm_storm()
    err = _classify_rclone_stderr(_STDERR_502_BLOCK)
    assert err is not None
    assert err.kind == "proton_infra"


def test_classify_2003_during_storm_is_proton_infra():
    # EARS SP-T9 + F8: a assinatura 2003 (também um sinal de storm) dispara.
    _arm_storm()
    err = _classify_rclone_stderr(_STDERR_2003_UPLOAD)
    assert err is not None
    assert err.kind == "proton_infra"


def test_classify_storm_signature_without_active_storm_is_none():
    # EARS SP-T9 (preserva S3): a MESMA assinatura SEM storm armado não pausa.
    _reset_infra_window()  # janela vazia → sem storm
    assert _classify_rclone_stderr(_STDERR_502_BLOCK) is None
    assert _classify_rclone_stderr(_STDERR_RAW_500_AUTH) is None


def test_classify_non_storm_error_during_storm_is_none():
    # Segurança: um erro que NÃO carrega assinatura de storm não é mascarado
    # como proton_infra só porque um storm está ativo.
    _arm_storm()
    assert _classify_rclone_stderr("rclone: directory not found") is None


def test_classify_isolated_auth_code_no_storm_unchanged_by_sp_t9():
    # Regressão: o caminho 1 (par auth isolado sem storm) segue invalid_credentials.
    _reset_infra_window()
    err = _classify_rclone_stderr(_STDERR_8002)
    assert err is not None
    assert err.kind == "invalid_credentials"


# ---------------------------------------------------------------------------
# SP-T10 — precedência de divergência-genuína (trava de segurança S6) · #79
# ---------------------------------------------------------------------------


def test_classify_too_many_deletes_during_storm_defers_not_proton_infra():
    # EARS SP-T10 (S6): IF o stderr casa `too many deletes` (rc=1) E storm ativo
    # E há Status=5xx no mesmo stderr, THEN retornar None (NÃO proton_infra) —
    # deixando o abort alcançar o handler [BISYNC_SAFETY_ABORT] do bisync_folder.
    # Sem esta trava, o raise de proton_infra em _run pularia o branch rc≠0 e o
    # advice de perda-de-dados seria suprimido.
    _arm_storm()
    stderr = (
        "502 POST https://zrh-storage.proton.me/storage/blocks (Code=0, Status=502)\n"
        "ERROR: Safety abort: too many deletes (>50%) - Run with --force if desired"
    )
    assert _is_too_many_deletes(stderr) is True  # pré-condição do teste
    assert _infra_storm_active() is True
    assert _classify_rclone_stderr(stderr) is None


def test_classify_storm_without_divergence_still_proton_infra():
    # Contraprova: mesma janela armada, um 5xx SEM assinatura de divergência
    # segue virando proton_infra (a trava S6 é específica, não desliga o SP-T9).
    _arm_storm()
    err = _classify_rclone_stderr(_STDERR_502_BLOCK)
    assert err is not None
    assert err.kind == "proton_infra"


# ---------------------------------------------------------------------------
# #61 / SP-T7 (J4) — força `--protondrive-2fa ""` em TODA chamada rclone.
# Um `2fa` estático no rclone.conf é inútil no cold reauth e só produz o `8002`
# enganoso; o flag explícito-vazio sobrescreve o config SEM escrever o arquivo
# (dissolve a corrida de escrita — o motivo do defer original de SP-T7).
# ---------------------------------------------------------------------------

def test_base_cmd_forces_empty_2fa():
    engine = RcloneEngine(_app())
    cmd = engine._base_cmd()
    assert _carries_empty_2fa(cmd)
    # explícito-vazio (não ausente): o token logo após o flag é "" exatamente.
    idx = cmd.index("--protondrive-2fa")
    assert cmd[idx + 1] == ""


def test_base_cmd_preserves_global_flags_alongside_2fa():
    app = _app()
    app.rclone.global_flags = ["--transfers", "4"]
    engine = RcloneEngine(app)
    cmd = engine._base_cmd()
    assert "--transfers" in cmd and "4" in cmd
    assert _carries_empty_2fa(cmd)


async def test_all_rclone_calls_carry_empty_2fa(tmp_path, monkeypatch):
    """mkdir E bisync — todos os sites de invocação levam o flag. mkdir era o
    único que bypassava _base_cmd() antes de #61; o teste trava a regressão."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    engine = RcloneEngine(_app())
    folder = _folder(local_path=tmp_path / "local")
    captured: list[list[str]] = []

    async def fake_run(cmd, timeout=None):
        captured.append(cmd)
        return (0, "", "")

    with patch("drive_sync.sync_engine._run", fake_run):
        await engine.bisync_folder(folder)

    assert _mkdir_calls(captured), "mkdir deve ter sido chamado"
    assert _bisync_calls(captured), "bisync deve ter sido chamado"
    for cmd in captured:
        assert _carries_empty_2fa(cmd), f"cmd sem --protondrive-2fa vazio: {cmd}"


# ---------------------------------------------------------------------------
# #45 — max-runtime kill switch por job rclone.
# ---------------------------------------------------------------------------
from drive_sync.sync_engine import StuckJobError  # noqa: E402


async def test_run_kills_stuck_job_and_raises():
    """Job que estoura o timeout é morto rápido (não espera o processo terminar)
    e levanta StuckJobError carregando o limite."""
    start = time.monotonic()
    with pytest.raises(StuckJobError) as ei:
        await _run(["sleep", "10"], timeout=0.3)
    elapsed = time.monotonic() - start
    assert elapsed < 5.0, f"não matou rápido: {elapsed}s (esperava ~0.3s + grace)"
    assert ei.value.timeout_seconds == 0.3


async def test_run_no_timeout_completes_normally():
    rc, _out, _err = await _run(["true"], timeout=None)
    assert rc == 0


async def test_run_finishes_before_timeout_not_killed():
    rc, _out, _err = await _run(["true"], timeout=30)
    assert rc == 0


def test_job_timeout_folder_override_wins():
    app = _app()
    app.rclone.max_job_runtime_seconds = 7200
    engine = RcloneEngine(app)
    folder = _folder()
    folder.max_job_runtime_seconds = 60
    assert engine._job_timeout(folder) == 60.0


def test_job_timeout_inherits_global_when_folder_none():
    app = _app()
    app.rclone.max_job_runtime_seconds = 100
    engine = RcloneEngine(app)
    folder = _folder()  # max_job_runtime_seconds = None
    assert engine._job_timeout(folder) == 100.0


def test_job_timeout_disabled_returns_none():
    # global 0 → desligado
    app = _app()
    app.rclone.max_job_runtime_seconds = 0
    assert RcloneEngine(app)._job_timeout(_folder()) is None
    # folder 0 sobrepõe global não-zero → desligado só para o folder
    app2 = _app()
    app2.rclone.max_job_runtime_seconds = 7200
    folder = _folder()
    folder.max_job_runtime_seconds = 0
    assert RcloneEngine(app2)._job_timeout(folder) is None


async def test_bisync_passes_folder_timeout_to_every_run(tmp_path, monkeypatch):
    """mkdir E bisync recebem o timeout resolvido do folder (#45)."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    app = _app()
    app.rclone.max_job_runtime_seconds = 111
    engine = RcloneEngine(app)
    folder = _folder(local_path=tmp_path / "local")
    seen: list[float | None] = []

    async def fake_run(cmd, timeout=None):
        seen.append(timeout)
        return (0, "", "")

    with patch("drive_sync.sync_engine._run", fake_run):
        await engine.bisync_folder(folder)

    assert seen, "esperava ao menos mkdir + bisync"
    assert all(t == 111.0 for t in seen), f"timeout não propagado: {seen}"


# ---------------------------------------------------------------------------
# Auto-recuperação gated de rc=7 stale-listings (SP-T4 · #47 · ADR-019)
# ---------------------------------------------------------------------------

# Amostras de dry-run (SP-T1 — docs/spikes/SP-T1-autoresync-dryrun-parse.md).
_DRYRUN_NOOP = (
    "NOTICE: - Path2  Resync is copying files to  - Path1\n"
    "NOTICE: - Path1  Resync is copying files to  - Path2\n"
    "Transferred:   \t          0 B / 0 B, -, 0 B/s, ETA -\n"
    "Checks:                 6 / 6, 100%, Listed 16\n"
)
_DRYRUN_DIVERGENT = (
    "NOTICE: - Path2  Resync is copying files to  - Path1\n"
    "NOTICE: only-remote.txt: Skipped copy as --dry-run is set (size 18)\n"
    "Transferred:   \t         23 B / 23 B, 100%, 0 B/s, ETA -\n"
    "Transferred:            1 / 1, 100%\n"
)
_STALE_STDERR = (
    "ERROR : Bisync critical error: cannot find prior Path1 or Path2 listings\n"
    "ERROR : Bisync aborted. Must run --resync to recover."
)


def _dryrun_calls(captured):
    return [c for c in captured if "bisync" in c and "--dry-run" in c]


def _real_resync_calls(captured):
    return [c for c in captured if "bisync" in c and "--resync" in c and "--dry-run" not in c]


def _make_stuck_fake_run(captured, *, dryrun_out, resync_rc=0):
    """fake _run: mkdir ok; bisync plano → rc=7 stale; --dry-run → dryrun_out (stderr);
    --resync real → resync_rc."""
    async def fake_run(cmd, timeout=None):
        captured.append(cmd)
        if "mkdir" in cmd:
            return (0, "", "")
        if "bisync" in cmd:
            if "--dry-run" in cmd:
                return (0, "", dryrun_out)
            if "--resync" in cmd:
                return (resync_rc, "", "" if resync_rc == 0 else "resync failed")
            return (7, "", _STALE_STDERR)  # bisync plano preso
        return (0, "", "")
    return fake_run


def _prime_marker(app, folder):
    marker = _state_marker_for(folder.local_path, remote_uri_for(folder, app))
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()  # marker existe → bisync não é first-run (simula folder preso)
    return marker


def test_dryrun_resync_is_noop_parse():
    assert _dryrun_resync_is_noop(_DRYRUN_NOOP) is True
    assert _dryrun_resync_is_noop(_DRYRUN_DIVERGENT) is False
    # Fail-safe: output vazio / sem sinal de stats → NÃO no-op.
    assert _dryrun_resync_is_noop("") is False
    assert _dryrun_resync_is_noop("Transferred: 5 B / 5 B") is False


async def test_autoresync_recovers_on_noop_dryrun(tmp_path, monkeypatch):
    # S1: rc=7 stale-listings + dry-run prova no-op → roda --resync real → sucesso
    # na mesma chamada, sem intervenção manual.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    app = _app()
    engine = RcloneEngine(app)
    folder = _folder(local_path=tmp_path / "local")
    _prime_marker(app, folder)
    captured: list[list[str]] = []
    with patch("drive_sync.sync_engine._run", _make_stuck_fake_run(captured, dryrun_out=_DRYRUN_NOOP)):
        result = await engine.bisync_folder(folder)
    assert result is True
    assert len(_dryrun_calls(captured)) == 1, "esperava um pre-check dry-run"
    assert len(_real_resync_calls(captured)) == 1, "esperava um --resync real pós-prova"


async def test_autoresync_skips_on_divergent_dryrun(tmp_path, monkeypatch):
    # S2/C2: dry-run reporta transfer/would-copy (divergência) → NÃO dispara
    # --resync real; permanece degradado. ZERO transfer/delete pela auto-recuperação.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    app = _app()
    engine = RcloneEngine(app)
    folder = _folder(local_path=tmp_path / "local")
    _prime_marker(app, folder)
    captured: list[list[str]] = []
    with patch("drive_sync.sync_engine._run", _make_stuck_fake_run(captured, dryrun_out=_DRYRUN_DIVERGENT)):
        result = await engine.bisync_folder(folder)
    assert result is False
    assert len(_dryrun_calls(captured)) == 1, "esperava o pre-check dry-run"
    assert _real_resync_calls(captured) == [], "NÃO deveria disparar --resync real em divergência"


async def test_autoresync_guard_one_attempt_per_episode(tmp_path, monkeypatch):
    # D4: 2 ciclos presos no mesmo episódio → só o 1º tenta o dry-run; o 2º pula
    # (guard), sem re-disparar o dry-run num folder divergente.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    app = _app()
    engine = RcloneEngine(app)
    folder = _folder(local_path=tmp_path / "local")
    _prime_marker(app, folder)
    captured: list[list[str]] = []
    fake = _make_stuck_fake_run(captured, dryrun_out=_DRYRUN_DIVERGENT)
    with patch("drive_sync.sync_engine._run", fake):
        r1 = await engine.bisync_folder(folder)
        r2 = await engine.bisync_folder(folder)
    assert r1 is False and r2 is False
    assert len(_dryrun_calls(captured)) == 1, "2º ciclo não deveria re-tentar o dry-run (guard)"


async def test_autoresync_disabled_by_killswitch(tmp_path, monkeypatch):
    # D5: auto_resync_stale_listings=False → comportamento legado (nenhum dry-run).
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    app = _app()
    app.rclone.auto_resync_stale_listings = False
    engine = RcloneEngine(app)
    folder = _folder(local_path=tmp_path / "local")
    _prime_marker(app, folder)
    captured: list[list[str]] = []
    with patch("drive_sync.sync_engine._run", _make_stuck_fake_run(captured, dryrun_out=_DRYRUN_NOOP)):
        result = await engine.bisync_folder(folder)
    assert result is False
    assert _dryrun_calls(captured) == [], "kill-switch off → nenhum dry-run"


async def test_autoresync_guard_cleared_on_success(tmp_path, monkeypatch):
    # Guard limpo em sucesso: após recuperar, o marker sai do set → um novo episódio
    # preso ganha tentativa fresca.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    app = _app()
    engine = RcloneEngine(app)
    folder = _folder(local_path=tmp_path / "local")
    marker = _prime_marker(app, folder)
    captured: list[list[str]] = []
    with patch("drive_sync.sync_engine._run", _make_stuck_fake_run(captured, dryrun_out=_DRYRUN_NOOP)):
        await engine.bisync_folder(folder)
    assert marker not in engine._autoresync_attempted, "sucesso deve limpar o guard"


async def test_autoresync_resync_failure_stays_degraded(tmp_path, monkeypatch):
    # F2 (review): dry-run prova no-op mas o --resync real falha (rc≠0) → NÃO
    # recupera, permanece degradado (marker fica no guard: houve veredito, não sucesso).
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    app = _app()
    engine = RcloneEngine(app)
    folder = _folder(local_path=tmp_path / "local")
    marker = _prime_marker(app, folder)
    captured: list[list[str]] = []
    fake = _make_stuck_fake_run(captured, dryrun_out=_DRYRUN_NOOP, resync_rc=1)
    with patch("drive_sync.sync_engine._run", fake):
        result = await engine.bisync_folder(folder)
    assert result is False
    assert len(_real_resync_calls(captured)) == 1, "tentou o --resync real pós-prova"
    assert marker in engine._autoresync_attempted, "veredito alcançado → tentativa consumida"


async def test_autoresync_guard_not_burned_on_exception(tmp_path, monkeypatch):
    # F1 (review): se o dry-run levanta exceção (ex.: AuthDegradedError de storm
    # remanescente — o mesmo storm que causou o rc=7), o guard NÃO é queimado →
    # próximo ciclo pode re-tentar. Sem isso, um blip barraria auto-resync até restart.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    app = _app()
    engine = RcloneEngine(app)
    folder = _folder(local_path=tmp_path / "local")
    marker = _prime_marker(app, folder)

    async def fake_run(cmd, timeout=None):
        if "mkdir" in cmd:
            return (0, "", "")
        if "bisync" in cmd and "--dry-run" in cmd:
            raise AuthDegradedError("proton_infra", 8002, "storm remanescente")
        if "bisync" in cmd:
            return (7, "", _STALE_STDERR)
        return (0, "", "")

    with patch("drive_sync.sync_engine._run", fake_run):
        with pytest.raises(AuthDegradedError):
            await engine.bisync_folder(folder)
    assert marker not in engine._autoresync_attempted, "exceção não deve consumir a tentativa"
