"""Tests para expiração de lock órfão de bisync via --max-lock (ADR-022, #88).

Modelo (corrigido pelo diagnóstico `debug`, brief:F9/F10):
- `--max-lock` é PREVENÇÃO write-time: grava `TimeExpires = now + janela` e renova
  enquanto o run vive. A expiração é governada pelo `TimeExpires` GRAVADO no `.lck`
  (`now > TimeExpires`), NÃO pelo flag `--max-lock` do leitor.
- Ultrapassar um lock EXPIRADO não é um proceed limpo: o rclone remove o lock E
  deleta as listings → aborta `rc=7 stale-listings`, que o braço rc=7 (ADR-019)
  então recupera de forma data-safe (fora do escopo deste teste).

Cobre:
- SP-T1: knob rclone.max_lock_seconds — parse/default/validação em load_config.
- SP-T2: --max-lock <N>s montado nas invocações de bisync quando knob > 0.
- SP-T3(a): MECANISMO — um lock com TimeExpires no PASSADO NÃO bloqueia rc=1 (é
  ultrapassado) e desemboca em rc=7 stale-listings (listings purgadas).
- SP-T3(b): CONTRASTE — um lock never-expire (TimeExpires futuro) bloqueia rc=1, e
  o flag --max-lock do leitor NÃO muda isso (pina brief:F9/F10 + S2/N1).

Cap de teste (ADR-022): a composição fim-a-fim (rc=7 → ADR-019 → sucesso) via
bisync_folder+remote e a renovação-enquanto-vivo (F3) não são re-testadas aqui —
cobertas pelos testes do ADR-019 + evidência viva do frame/debug.
"""
import asyncio
import json
import os
import shutil
import subprocess
import textwrap
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
    load_config,
)
from drive_sync.sync_engine import RcloneEngine, _state_marker_for, remote_uri_for


# ---------------------------------------------------------------------------
# SP-T1 — knob parse/default/validação (via load_config)
# ---------------------------------------------------------------------------

def _write_yaml(directory: Path, content: str) -> Path:
    path = directory / "config.yaml"
    path.write_text(textwrap.dedent(content))
    return path


def _base_folder() -> str:
    return """
        folders:
          - name: docs
            local_path: /tmp/docs
            remote_subpath: Documents
    """


def test_max_lock_default_is_3600(tmp_path):
    app = load_config(_write_yaml(tmp_path, _base_folder()))
    assert app.rclone.max_lock_seconds == 3600


def test_max_lock_explicit_parsed(tmp_path):
    app = load_config(_write_yaml(tmp_path, _base_folder() + """
        rclone:
          max_lock_seconds: 1800
    """))
    assert app.rclone.max_lock_seconds == 1800


def test_max_lock_zero_disables(tmp_path):
    app = load_config(_write_yaml(tmp_path, _base_folder() + """
        rclone:
          max_lock_seconds: 0
    """))
    assert app.rclone.max_lock_seconds == 0


def test_max_lock_minimum_boundary_120_accepted(tmp_path):
    app = load_config(_write_yaml(tmp_path, _base_folder() + """
        rclone:
          max_lock_seconds: 120
    """))
    assert app.rclone.max_lock_seconds == 120


@pytest.mark.parametrize("bad", [1, 50, 119, -1])
def test_max_lock_below_minimum_rejected(tmp_path, bad):
    cfg = _write_yaml(tmp_path, _base_folder() + f"""
        rclone:
          max_lock_seconds: {bad}
    """)
    with pytest.raises(ValueError, match=r"max_lock_seconds.*(120|>= 120)"):
        load_config(cfg)


# ---------------------------------------------------------------------------
# SP-T2 — --max-lock montado no cmd de bisync conforme o knob
# ---------------------------------------------------------------------------

def _app(max_lock_seconds: int) -> AppConfig:
    return AppConfig(
        rclone=RcloneConfig(
            remote_name="proton", remote_root="Sync", max_lock_seconds=max_lock_seconds
        ),
        folders=[],
        git=GitConfig(),
        watcher=WatcherConfig(),
        dedupe=DedupeConfig(),
        health_check=HealthCheckConfig(),
        logging=LoggingConfig(),
        coverage_audit=CoverageAuditConfig(),
        source_path=Path("/fake/config.yaml"),
    )


async def _capture_bisync_cmd(app: AppConfig, tmp_path: Path) -> list[str]:
    folder = FolderConfig(
        name="t", local_path=tmp_path / "data", remote_subpath="t", auto_exclude=False
    )
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    marker = _state_marker_for(folder.local_path, remote_uri_for(folder, app))
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()

    captured: list[list[str]] = []

    async def fake_run(cmd, timeout=None):
        captured.append(cmd)
        return (0, "", "")

    with patch("drive_sync.sync_engine._run", fake_run):
        await RcloneEngine(app).bisync_folder(folder)

    bisync_cmds = [c for c in captured if "bisync" in c]
    assert bisync_cmds, f"nenhum cmd de bisync capturado em {captured}"
    return bisync_cmds[0]


def test_cmd_includes_max_lock_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    cmd = asyncio.run(_capture_bisync_cmd(_app(3600), tmp_path))
    assert "--max-lock" in cmd
    assert cmd[cmd.index("--max-lock") + 1] == "3600s"


def test_cmd_omits_max_lock_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    cmd = asyncio.run(_capture_bisync_cmd(_app(0), tmp_path))
    assert "--max-lock" not in cmd


# ---------------------------------------------------------------------------
# SP-T3 — mecanismo de expiração por TimeExpires gravado (integração rclone)
# ---------------------------------------------------------------------------

_LOCK_MSG = "prior lock file found"
_STALE_MSG = "cannot find prior Path1 or Path2 listings"

# Rótulos ISO com fuso; a data é o que importa para a comparação `now > TimeExpires`.
_TE_FUTURE = "2226-07-23T14:36:40-03:00"   # never-expire (escrito sem --max-lock)
_TE_PAST = "2000-01-01T00:02:00-03:00"     # já expirado (feature-written, dono morto)


def _init_pair_lck(workdir: Path, src: Path, dst: Path) -> Path:
    subprocess.run(
        ["rclone", "bisync", str(src), str(dst), "--resync",
         "--workdir", str(workdir), "--max-lock", "2m"],
        check=True, capture_output=True, text=True,
    )
    lsts = list(workdir.glob("*.path1.lst"))
    assert lsts, f"nenhum .lst criado em {workdir}"
    return workdir / f"{lsts[0].name[: -len('.path1.lst')]}.lck"


def _plant(lck: Path, time_expires: str) -> None:
    lck.write_text(json.dumps({
        "Session": lck.stem, "PID": "999999",
        "TimeRenewed": "2000-01-01T00:00:00-03:00", "TimeExpires": time_expires,
    }))
    old = 946702800  # mtime bem antigo (2000) — irrelevante p/ a decisão, mas realista
    os.utime(lck, (old, old))


def _run_bisync(src: Path, dst: Path, workdir: Path, max_lock: bool):
    cmd = ["rclone", "bisync", str(src), str(dst), "--workdir", str(workdir)]
    if max_lock:
        cmd += ["--max-lock", "2m"]
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


def _fresh_pair(base: Path):
    src, dst, work = base / "src", base / "dst", base / "work"
    for d in (src, dst, work):
        d.mkdir(parents=True, exist_ok=True)
    (src / "a.txt").write_text("hello")
    return src, dst, work


@pytest.mark.skipif(shutil.which("rclone") is None, reason="rclone não instalado")
def test_expired_lock_does_not_block_and_hits_stale_listings(tmp_path):
    """SP-T3(a): lock com TimeExpires no PASSADO NÃO bloqueia rc=1 (é ultrapassado);
    o run desemboca em rc=7 stale-listings (listings purgadas), o caminho que o
    braço rc=7/ADR-019 então recupera."""
    src, dst, work = _fresh_pair(tmp_path)
    lck = _init_pair_lck(work, src, dst)
    _plant(lck, _TE_PAST)

    rc, out = _run_bisync(src, dst, work, max_lock=True)
    assert _LOCK_MSG not in out, f"lock expirado NÃO devia bloquear rc=1:\n{out}"
    assert rc == 7 and _STALE_MSG in out, (
        f"esperava rc=7 stale-listings após ultrapassar o lock expirado; rc={rc}\n{out}"
    )


@pytest.mark.skipif(shutil.which("rclone") is None, reason="rclone não instalado")
def test_never_expire_lock_blocks_regardless_of_max_lock(tmp_path):
    """SP-T3(b): um lock never-expire (TimeExpires futuro) bloqueia rc=1, e o flag
    --max-lock do LEITOR não muda isso — a expiração é pelo TimeExpires gravado
    (brief:F9), por isso locks pré-existentes exigem migração (F10) e um lock
    vivo/renovado segue protegendo concorrência (S2/N1)."""
    for max_lock in (False, True):
        src, dst, work = _fresh_pair(tmp_path / f"ml_{max_lock}")
        lck = _init_pair_lck(work, src, dst)
        _plant(lck, _TE_FUTURE)

        rc, out = _run_bisync(src, dst, work, max_lock=max_lock)
        assert rc == 1 and _LOCK_MSG in out, (
            f"lock never-expire devia bloquear rc=1 (max_lock={max_lock}); rc={rc}\n{out}"
        )
