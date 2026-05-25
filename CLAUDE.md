# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

`drive-sync` is a Python daemon that performs bidirectional sync of local folders with Proton Drive on Linux, using `rclone bisync` for transfer, `watchdog` (inotify) for change detection, and `systemd --user` for auto-start. The project exists because Proton has no native Linux sync client yet (planned for 2026).

## Installation & Setup

```bash
bash scripts/install.sh          # installs rclone, pipx-installs the package, sets up systemd unit
rclone config                    # configure the remote (name it "proton" or per config.yaml)
drive-sync --check               # validate config.yaml
systemctl --user start drive-sync
journalctl --user -u drive-sync -f
```

## Development Workflow

For dev, use an **isolated venv** — do NOT `pip install -e .` against system Python. On Fedora that overwrites the pipx-managed symlink at `~/.local/bin/drive-sync` and breaks the systemd unit (see "install: investigar regressão do symlink" in [BACKLOG.md](../BACKLOG.md)).

```bash
python -m venv .venv && .venv/bin/pip install -e .
.venv/bin/python -m drive_sync --check    # validate config
.venv/bin/python -m drive_sync --status   # snapshot do estado das pastas
.venv/bin/python -m drive_sync --once     # one sync pass, exit
.venv/bin/python -m drive_sync            # run as daemon
```

Production install health check: `pipx list` should show `drive-sync` without the "symlink missing or pointing to unexpected location" warning.

Logs go to `~/.local/state/drive-sync/drive-sync.log` by default. Set `logging.level: DEBUG` in config.yaml for verbose output.

test_command: `python -m pytest tests/ -v`

## Architecture

The daemon has four cooperating layers:

**`config.py`** — Loads and validates `~/.config/drive-sync/config.yaml` (XDG). Returns typed dataclasses (`AppConfig`, `FolderConfig`, `RcloneConfig`, etc.). All path strings are expanded via `os.path.expanduser`/`expandvars` at load time.

**`watcher.py`** — Wraps `watchdog.Observer` in a thread, debouncing filesystem events per folder. Posts folder names to an `asyncio.Queue` (thread-safe via `run_coroutine_threadsafe`). `owning_folder()` implements the sub-path deduplication: if `/A` and `/A/B` are both configured, events under `/A/B` only trigger B's job, not A's.

**`daemon.py`** (`SyncDaemon`) — Consumes the queue with N async workers (controlled by `asyncio.Semaphore`). An `_inflight` set prevents two concurrent jobs for the same folder. Also runs a periodic full-sync task at a configurable interval. Handles `SIGTERM`/`SIGINT` for clean systemd shutdown.

**`sync_engine.py`** (`RcloneEngine`) — Executes rclone as a subprocess via `asyncio.create_subprocess_exec`. `bisync_folder()` detects first-run via a local marker file and adds `--resync` automatically. `remote_uri_for()` builds the `<remote>:<root>/<subpath>` URI.

**`git_handler.py`** — Used only when `git_mode: bundle`. Creates a git bundle that also captures uncommitted worktree state: it creates a snapshot commit in `refs/drive-sync/snapshot` using a temporary `GIT_INDEX_FILE` (so the user's index is never touched), bundles it alongside the full history, then deletes the snapshot ref. `restore_from_bundle()` reverses this, materializing worktree files using `git checkout-index` against a temporary index.

**`exclude_presets.py`** — Returns the list of rclone `--exclude` globs applied when `auto_exclude: true`. Covers Python, JS/TS, Rust, Go, Java, editor artifacts. Note: `.git/` itself is NOT excluded in `bisync` mode (the full repo needs to be cloud-usable), only transient git files inside `.git/`.

## Operational Invariants

Non-obvious behaviors that have caused multi-day incidents — preserve them:

- **rclone calls are serialized** ([ADR-001](../docs/decisions/ADR-001-serializar-chamadas-rclone.md)): an `asyncio.Lock` wraps `_run` in `sync_engine.py`. Worker parallelism is an illusion — all useful work is rclone, which runs one at a time. Avoids a token-refresh race in the protondrive backend ([rclone#7381](https://github.com/rclone/rclone/issues/7381)) that otherwise invalidates `client_uid` every ~5 days and forces manual TOTP reauth.
- **bisync errors do NOT auto-recover**: `sync_engine.py:134-137` is an explicit decision — when rclone reports "Must run --resync to recover", the daemon logs and moves on. Manual `rclone bisync ... --resync` is required (mirror the daemon's flags from journalctl).
- **Error logs are tail-truncated**: `sync_engine.py:133` records `err.strip()[-500:]`. Cryptic fragments like `"xist?"` are tail-only — real cause is earlier in stderr. Read full log lines, or capture stderr separately when reproducing.
- **systemd unit hardening was relaxed** ([ADR-002](../docs/decisions/ADR-002-relaxar-hardening-systemd-protondrive.md)): `ProtectSystem=strict` was removed because it triggered spurious EROFS in rclone+protondrive on large folders. Don't re-add without re-running the ADR's experiment.
- **Daemon degraded em falha de auth (complementa, não substitui, o invariante `bisync errors do NOT auto-recover`)** ([ADR-003](../docs/decisions/ADR-003-type-notify-sinalizacao-degraded.md)): erros bisync genéricos continuam logando e seguindo; apenas falha de auth classificada (pares `(Code, Status)` em `_AUTH_CODES` no `sync_engine.py`, endpoint `/auth/v4` com ou sem prefixo `/api/`) dispara pausa global dos workers e sinalização via `systemctl status` (`STATUS=degraded: ...`), `notify-send` e log tagueado `[AUTH_DEGRADED]`. Recuperação manual depende do `kind` reportado: `invalid_credentials`/`captcha_required` → `rclone config update proton 2fa <code>` + `systemctl --user restart drive-sync`; `refresh_token_invalid` → restart simples (auto-resolve no próximo ciclo); `rate_limited` → aguardar gate da Proton relaxar (~30-60min sem tráfego) antes do restart, evitar marteladas que aprofundam o gate. Sem auto-resume em nenhum caso — flakiness lateral da Proton pode mascarar problemas residuais.
- **Cooldown por folder gate-keia também o periodic full-sync** ([ADR-004](../docs/decisions/ADR-004-cooldown-gate-periodic-full-sync.md)): `cooldown_seconds` em `FolderConfig` é opt-in (default 0 = desligado). Quando > 0, o gate é avaliado no worker (`daemon.py:_worker`) **antes** do `_inflight_lock` e absorve tanto eventos do watcher quanto ciclos da safety-net `watcher.periodic_full_sync_seconds` para aquela pasta. Janela conta from-start (`_last_sync_at` é setado antes do `_process_folder`), não from-finish — falha cedo não estende a janela. Estado in-memory (`_last_sync_at`, `_cooldown_scheduled`, `_cooldown_tasks`); sem persistência cross-restart — restart gera no máximo 1 upload extra. Motivação principal: `git_mode: bundle` em repo com `.git/` na casa dos GB (rclone não faz delta upload de blob no backend protondrive).
- **Staleness per-folder sinaliza degraded sem pausar** ([ADR-005](../docs/decisions/ADR-005-folder-staleness-degraded.md) + [ADR-007](../docs/decisions/ADR-007-staleness-monotonic-suspend-aware.md)): pasta sem `_process_folder` retornando sucesso há mais que `watcher.folder_staleness_threshold_seconds` (default 12h, opt-out via 0) entra em `_degraded_folders` e dispara `Notifier.folder_degraded` (log `[FOLDER_DEGRADED]` + `notify-send`) + `STATUS=degraded folders: <lista>` via sd_notify. **Distinto de ADR-003** (auth global com pausa) — staleness é per-folder, sem pausa de workers; STATUS é agregada no daemon (`_compose_status_payload`) com precedência auth > folder. Reset por sucesso (silencioso, sem `notify-send` de recuperação). Detecção piggyback no `_periodic_full_sync` (gate de auth-degraded executado antes); requer `periodic_full_sync_seconds > 0` (validado no `config.load_config`). **Dual-clock (ADR-007)**: gate consulta monotonic (`time.monotonic()`, alinhado com ADR-004 — suspend congela com o processo, evita falso-positivo após suspend > threshold); reason em `min(elapsed_wall, elapsed_mono)` preserva "horas reais" cap por gap de cadência. Restart re-avalia a janela contra `_daemon_start_monotonic` (erra para falso-negativo pós-restart por até threshold ativas — folder com falha real preexistente fica invisível na primeira janela; trade-off aceito vs. falso-positivo de ADR-005 original). Estado in-memory (`_last_successful_sync_at`, `_last_successful_sync_at_mono`, `_degraded_folders`, `_daemon_start_time`, `_daemon_start_monotonic`).

## git_mode Semantics

| Mode | What syncs | When to use |
|---|---|---|
| `off` | Everything (no excludes) | Non-code folders (Documents, Pictures) |
| `bisync` (default) | Full worktree + `.git/`, minus build artifacts | All code folders |
| `bundle` | Only `.gitbundle` file | Repos with very large `.git/` history |

The `bundle` mode is opt-in. `bisync` is the preferred default because it syncs uncommitted changes and keeps the cloud copy as a usable git repo.

Cada folder pode adicionalmente declarar `subpath_overrides: [{subpath, git_mode}]` para subpastas específicas — expansão acontece em `load_config` e o runtime vê apenas a lista plana ([ADR-006](../docs/decisions/ADR-006-git-mode-subpath-override.md)).

## Config Location

Runtime config: `~/.config/drive-sync/config.yaml` (or `$XDG_CONFIG_HOME/drive-sync/config.yaml`).

The reference config at [config/config.yaml](config/config.yaml) shows the full schema with all available options. Every section except `folders` is optional and has defaults in the dataclasses.

## Key Paths (Runtime)

- Config: `~/.config/drive-sync/config.yaml`
- Logs: `~/.local/state/drive-sync/drive-sync.log`
- bisync state markers: `~/.cache/rclone/bisync/`
- Git bundles (bundle mode): `~/.cache/drive-sync/bundles/<task>/<rel>.gitbundle`
- systemd unit: `~/.config/systemd/user/drive-sync.service`

## Decision Log & Backlog

- ADRs: `docs/decisions/ADR-*.md` — durable architectural decisions.
- Plans: `docs/plans/*.md` — pre-fact implementation plans, kept after merge.
- Backlog: `BACKLOG.md` — `## Próximos` and `## Concluídos` (kept as institutional memory; do not prune).

## Pragmatic Toolkit

<!-- pragmatic-toolkit:config -->
```yaml
paths:
  plans_dir: local
test_command: "uv run pytest -q --no-header"
```
