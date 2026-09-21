"""Testes do driver `--dry-run-resync` (#94).

Cobrem os dois findings do review no-anchor (F1/F2) e o contrato de exit code,
que é o que um script consome:

    0 = seguro (no-op ou só-adições) · 1 = tem overwrite · 2 = erro / sem veredito

O caso crítico é F1: uma falha de auth NUNCA pode sair com 1 — esse código
significa "TEM OVERWRITE", e um script o leria como veredito de divergência
destrutiva quando na verdade não houve veredito algum.
"""
from __future__ import annotations

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
from drive_sync.dry_run_resync import render_report, run_dry_run_resync
from drive_sync.listing_diff import Divergence
from drive_sync.sync_engine import AuthDegradedError, StuckJobError


def _app(tmp_path: Path, max_job_runtime_seconds: int = 7200) -> AppConfig:
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    return AppConfig(
        rclone=RcloneConfig(
            remote_name="proton",
            remote_root="Sync",
            max_job_runtime_seconds=max_job_runtime_seconds,
        ),
        folders=[
            FolderConfig(
                name="alvo",
                local_path=tmp_path / "data",
                remote_subpath="alvo",
                auto_exclude=False,
            )
        ],
        git=GitConfig(),
        watcher=WatcherConfig(),
        dedupe=DedupeConfig(),
        health_check=HealthCheckConfig(),
        logging=LoggingConfig(),
        coverage_audit=CoverageAuditConfig(),
        source_path=Path("/fake/config.yaml"),
    )


# --- contrato de exit code -------------------------------------------------

def test_unknown_folder_exits_2(tmp_path, capsys):
    assert run_dry_run_resync(_app(tmp_path), "nao-existe") == 2
    assert "não existe no config" in capsys.readouterr().out


def test_auth_failure_exits_2_never_1(tmp_path, capsys):
    """F1: exit 1 significa TEM OVERWRITE — uma falha de auth não pode usá-lo."""
    async def boom(cmd, timeout=None):
        raise AuthDegradedError("invalid_credentials", 8002, "401 Unauthorized")

    with patch("drive_sync.dry_run_resync._run", boom):
        rc = run_dry_run_resync(_app(tmp_path), "alvo")

    assert rc == 2, "falha de auth jamais pode sair com 1 (= TEM OVERWRITE)"
    out = capsys.readouterr().out
    assert "autenticação degradada" in out
    assert "nenhum veredito produzido" in out


def test_stuck_job_exits_2_never_1(tmp_path, capsys):
    async def boom(cmd, timeout=None):
        raise StuckJobError(7200)

    with patch("drive_sync.dry_run_resync._run", boom):
        rc = run_dry_run_resync(_app(tmp_path), "alvo")

    assert rc == 2
    assert "nenhum veredito produzido" in capsys.readouterr().out


def test_rclone_failure_reports_cause_not_symptom(tmp_path, capsys):
    """rc≠0 sem listing: reporta o stderr do rclone, não 'listing ausente'."""
    async def failed(cmd, timeout=None):
        return (7, "", "ERROR : cannot find prior Path1 or Path2 listings")

    with patch("drive_sync.dry_run_resync._run", failed):
        rc = run_dry_run_resync(_app(tmp_path), "alvo")

    assert rc == 2
    assert "cannot find prior Path1" in capsys.readouterr().out


# --- F2: o teto de runtime é propagado -------------------------------------

def test_timeout_is_passed_to_run(tmp_path):
    """F2: sem teto, um rclone pendurado travaria um uso scriptado para sempre."""
    seen: list[float | None] = []

    async def capture(cmd, timeout=None):
        seen.append(timeout)
        return (1, "", "falhou")

    with patch("drive_sync.dry_run_resync._run", capture):
        run_dry_run_resync(_app(tmp_path, max_job_runtime_seconds=1234), "alvo")

    assert seen == [1234]


def test_timeout_zero_means_no_limit(tmp_path):
    seen: list[float | None] = []

    async def capture(cmd, timeout=None):
        seen.append(timeout)
        return (1, "", "falhou")

    with patch("drive_sync.dry_run_resync._run", capture):
        run_dry_run_resync(_app(tmp_path, max_job_runtime_seconds=0), "alvo")

    assert seen == [None]


# --- relatório -------------------------------------------------------------

def test_report_noop():
    out = render_report("f", "proton:Sync/f", Divergence(identical=5))
    assert "NO-OP" in out


def test_report_additive_names_paths_and_verdict():
    div = Divergence(only_path1=["novo.txt"], identical=3)
    out = render_report("f", "proton:Sync/f", div)
    assert "SÓ-ADIÇÕES" in out
    assert "novo.txt" in out
    assert "OVERWRITE" not in out.split("VEREDITO")[1]


def test_report_overwrite_is_explicit_about_destruction():
    div = Divergence(overwrites=["conflito.txt"], identical=3)
    out = render_report("f", "proton:Sync/f", div)
    assert "TEM OVERWRITE" in out
    assert "conflito.txt" in out
    assert "destruindo" in out


def test_report_truncates_long_lists():
    div = Divergence(only_path1=[f"f{i}.txt" for i in range(50)])
    out = render_report("f", "proton:Sync/f", div)
    assert "e mais 30" in out
