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

```bash
pip install -e .                 # editable install (alternative to pipx for dev)
python -m drive_sync --check     # validate config without starting daemon
python -m drive_sync --once      # run one sync pass and exit (useful for testing)
python -m drive_sync             # run as daemon (Ctrl+C to stop)
python -m drive_sync -c path/to/config.yaml  # use alternate config
```

Logs go to `~/.local/state/drive-sync/drive-sync.log` by default. Set `logging.level: DEBUG` in config.yaml for verbose output.

There is currently no test suite.

## Architecture

The daemon has four cooperating layers:

**`config.py`** — Loads and validates `~/.config/drive-sync/config.yaml` (XDG). Returns typed dataclasses (`AppConfig`, `FolderConfig`, `RcloneConfig`, etc.). All path strings are expanded via `os.path.expanduser`/`expandvars` at load time.

**`watcher.py`** — Wraps `watchdog.Observer` in a thread, debouncing filesystem events per folder. Posts folder names to an `asyncio.Queue` (thread-safe via `run_coroutine_threadsafe`). `owning_folder()` implements the sub-path deduplication: if `/A` and `/A/B` are both configured, events under `/A/B` only trigger B's job, not A's.

**`daemon.py`** (`SyncDaemon`) — Consumes the queue with N async workers (controlled by `asyncio.Semaphore`). An `_inflight` set prevents two concurrent jobs for the same folder. Also runs a periodic full-sync task at a configurable interval. Handles `SIGTERM`/`SIGINT` for clean systemd shutdown.

**`sync_engine.py`** (`RcloneEngine`) — Executes rclone as a subprocess via `asyncio.create_subprocess_exec`. `bisync_folder()` detects first-run via a local marker file and adds `--resync` automatically. `remote_uri_for()` builds the `<remote>:<root>/<subpath>` URI.

**`git_handler.py`** — Used only when `git_mode: bundle`. Creates a git bundle that also captures uncommitted worktree state: it creates a snapshot commit in `refs/drive-sync/snapshot` using a temporary `GIT_INDEX_FILE` (so the user's index is never touched), bundles it alongside the full history, then deletes the snapshot ref. `restore_from_bundle()` reverses this, materializing worktree files using `git checkout-index` against a temporary index.

**`exclude_presets.py`** — Returns the list of rclone `--exclude` globs applied when `auto_exclude: true`. Covers Python, JS/TS, Rust, Go, Java, editor artifacts. Note: `.git/` itself is NOT excluded in `bisync` mode (the full repo needs to be cloud-usable), only transient git files inside `.git/`.

## git_mode Semantics

| Mode | What syncs | When to use |
|---|---|---|
| `off` | Everything (no excludes) | Non-code folders (Documents, Pictures) |
| `bisync` (default) | Full worktree + `.git/`, minus build artifacts | All code folders |
| `bundle` | Only `.gitbundle` file | Repos with very large `.git/` history |

The `bundle` mode is opt-in. `bisync` is the preferred default because it syncs uncommitted changes and keeps the cloud copy as a usable git repo.

## Config Location

Runtime config: `~/.config/drive-sync/config.yaml` (or `$XDG_CONFIG_HOME/drive-sync/config.yaml`).

The reference config at [config/config.yaml](config/config.yaml) shows the full schema with all available options. Every section except `folders` is optional and has defaults in the dataclasses.

## Key Paths (Runtime)

- Config: `~/.config/drive-sync/config.yaml`
- Logs: `~/.local/state/drive-sync/drive-sync.log`
- bisync state markers: `~/.cache/rclone/bisync/`
- Git bundles (bundle mode): `~/.cache/drive-sync/bundles/<task>/<rel>.gitbundle`
- systemd unit: `~/.config/systemd/user/drive-sync.service`
