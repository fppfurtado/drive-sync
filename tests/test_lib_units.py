"""#102: sync_units (scripts/lib-units.sh) re-instala só as units que mudaram.

Roda o bash real contra um diretório temporário, com um `systemctl` falso no
PATH que registra as chamadas — nada toca o systemd do host.
"""

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "scripts" / "lib-units.sh"
UNITS = ["drive-sync.service", "drive-sync-watchdog.service", "drive-sync-watchdog.timer"]


def _run(dest: Path, tmp_path: Path) -> tuple[list[str], list[str]]:
    """Roda sync_units; devolve (UNITS_CHANGED, chamadas ao systemctl)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    calls = tmp_path / "systemctl.calls"
    fake = bin_dir / "systemctl"
    fake.write_text(f'#!/usr/bin/env bash\necho "$*" >> "{calls}"\n')
    fake.chmod(0o755)
    script = f'set -euo pipefail; source "{LIB}"; sync_units "{REPO}" "{dest}"; echo "${{UNITS_CHANGED[*]}}"'
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    out = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, check=True)
    logged = calls.read_text().splitlines() if calls.exists() else []
    calls.unlink(missing_ok=True)
    return out.stdout.split(), logged


def test_fresh_install_copies_all_and_reloads(tmp_path):
    dest = tmp_path / "systemd-user"
    changed, calls = _run(dest, tmp_path)
    assert changed == UNITS
    assert calls == ["--user daemon-reload"]
    for unit in UNITS:
        assert (dest / unit).read_bytes() == (REPO / "systemd" / unit).read_bytes()


def test_up_to_date_is_noop_without_reload(tmp_path):
    dest = tmp_path / "systemd-user"
    _run(dest, tmp_path)
    changed, calls = _run(dest, tmp_path)
    assert changed == []
    assert calls == []


def test_only_divergent_unit_is_recopied(tmp_path):
    dest = tmp_path / "systemd-user"
    _run(dest, tmp_path)
    (dest / "drive-sync.service").write_text("[Service]\nTimeoutStartSec=45\n")
    changed, calls = _run(dest, tmp_path)
    assert changed == ["drive-sync.service"]
    assert calls == ["--user daemon-reload"]
    assert (dest / "drive-sync.service").read_bytes() == (REPO / "systemd" / "drive-sync.service").read_bytes()
