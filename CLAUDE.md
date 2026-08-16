# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> **Constelação:** este repo é um nó da constelação pessoal do operador (hub: [`meta-system`](https://github.com/fppfurtado/meta-system)). Pertencimento + relações cross-repo em [`catalog-info.yaml`](catalog-info.yaml).

## Overview

`drive-sync` is a Python daemon that performs bidirectional sync of local folders with Proton Drive on Linux, using `rclone bisync` for transfer, `watchdog` (inotify) for change detection, and `systemd --user` for auto-start. The project exists because Proton has no native Linux sync client yet (planned for 2026).

## Installation & Setup

```bash
bash scripts/install.sh          # pipx installs editable + systemd unit (ADR-009)
rclone config                    # configure the remote (name it "proton" or per config.yaml)
drive-sync --check               # validate config.yaml
systemctl --user start drive-sync
journalctl --user -u drive-sync -f
```

Para atualizar pós `git pull` (ritual self-update, ADR-009):

```bash
bash scripts/update.sh           # git pull --ff-only + systemctl --user restart drive-sync
```

**Migração one-shot** (uma vez por host, pós-merge de ADR-009): se você tinha drive-sync instalado pelo `install.sh` pré-ADR-009 (modo snapshot, sem `-e`), rode `bash scripts/install.sh` uma vez para aplicar `-e --force` retroativamente — entry-point idempotente cobre o caso (mesmo caminho que fresh install). Confirmação: `pipx list` mostra `drive-sync` sem warning `symlink missing or pointing to unexpected location`; `which drive-sync` retorna symlink (não arquivo regular).

Logs em `~/.local/state/drive-sync/drive-sync.log` por default. Set `logging.level: DEBUG` em config.yaml para verbose.

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
- **bisync errors do NOT auto-recover**: `sync_engine.py:240-245` is an explicit decision — when rclone reports "Must run --resync to recover", the daemon logs and moves on. Manual `rclone bisync ... --resync` is required (mirror the daemon's flags from journalctl).
- **Error logs surface first `ERROR:` line + full stderr in `~/.local/state/drive-sync/`** ([ADR-012](../docs/decisions/ADR-012-captura-stderr-completo-rclone-per-call-site.md)): cada um dos 4 call-sites de erro de rclone em `sync_engine.py` (mkdir, bisync, upload-bundle, download-bundle) emite log no formato `[<folder>] [<TAG>] [<contexto opcional>:] <summary> (full stderr: <path>)` onde `<TAG>` ∈ `{MKDIR_FAIL, BISYNC_FAIL, BUNDLE_UPLOAD_FAIL, BUNDLE_DOWNLOAD_FAIL}` (uniformidade com `[AUTH_DEGRADED]`/`[FOLDER_DEGRADED]` de ADR-003/005). `<contexto opcional>` varia por call-site (ex.: `rc=%d` em bisync, `<src> → <dst>` em upload-bundle); `<summary>` é a primeira linha `ERROR:` do stderr; `<path>` é `~/.local/state/drive-sync/last-stderr-<op>-<folder_slug>[-<sub_slug>].log` com stderr completo (overwrite, retenção 1-run). `cat <path>` resolve forense em segundos. Fallback no `<summary>` ao tail-truncate (`err.strip()[-500:]`) quando stderr não contém linha `ERROR:` (ex.: rclone abort prematuro); o arquivo `<path>` é sempre escrito com stderr completo. Investigation primary: `journalctl --user -u drive-sync --grep "BISYNC_FAIL"`.
- **systemd unit hardening was relaxed** ([ADR-002](../docs/decisions/ADR-002-relaxar-hardening-systemd-protondrive.md)): `ProtectSystem=strict` was removed because it triggered spurious EROFS in rclone+protondrive on large folders. Don't re-add without re-running the ADR's experiment.
- **Daemon degraded em falha de auth (complementa, não substitui, o invariante `bisync errors do NOT auto-recover`)** ([ADR-003](../docs/decisions/ADR-003-type-notify-sinalizacao-degraded.md)): erros bisync genéricos continuam logando e seguindo; apenas falha de auth classificada (pares `(Code, Status)` em `_AUTH_CODES` no `sync_engine.py`, endpoint `/auth/v4` com ou sem prefixo `/api/`) dispara pausa global dos workers e sinalização via `systemctl status` (`STATUS=degraded: ...`), `notify-send` e log tagueado `[AUTH_DEGRADED]`. Recuperação manual depende do `kind` reportado: `invalid_credentials`/`captcha_required` → `rclone config update proton 2fa <code>` + `systemctl --user restart drive-sync`; `refresh_token_invalid` → restart simples (auto-resolve no próximo ciclo); `rate_limited` → aguardar gate da Proton relaxar (~30-60min sem tráfego) antes do restart, evitar marteladas que aprofundam o gate. Sem auto-resume em nenhum caso — flakiness lateral da Proton pode mascarar problemas residuais.
- **Cooldown por folder gate-keia também o periodic full-sync** ([ADR-004](../docs/decisions/ADR-004-cooldown-gate-periodic-full-sync.md)): `cooldown_seconds` em `FolderConfig` é opt-in (default 0 = desligado). Quando > 0, o gate é avaliado no worker (`daemon.py:_worker`) **antes** do `_inflight_lock` e absorve tanto eventos do watcher quanto ciclos da safety-net `watcher.periodic_full_sync_seconds` para aquela pasta. Janela conta from-start (`_last_sync_at` é setado antes do `_process_folder`), não from-finish — falha cedo não estende a janela. Estado in-memory (`_last_sync_at`, `_cooldown_scheduled`, `_cooldown_tasks`); sem persistência cross-restart — restart gera no máximo 1 upload extra. Motivação principal: `git_mode: bundle` em repo com `.git/` na casa dos GB (rclone não faz delta upload de blob no backend protondrive).
- **Staleness per-folder sinaliza degraded sem pausar** ([ADR-005](../docs/decisions/ADR-005-folder-staleness-degraded.md) + [ADR-007](../docs/decisions/ADR-007-staleness-monotonic-suspend-aware.md)): pasta sem `_process_folder` retornando sucesso há mais que `watcher.folder_staleness_threshold_seconds` (default 12h, opt-out via 0) entra em `_degraded_folders` e dispara `Notifier.folder_degraded` (log `[FOLDER_DEGRADED]` + `notify-send`) + `STATUS=degraded folders: <lista>` via sd_notify. **Distinto de ADR-003** (auth global com pausa) — staleness é per-folder, sem pausa de workers; STATUS é agregada no daemon (`_compose_status_payload`) com precedência auth > folder. Reset por sucesso (silencioso, sem `notify-send` de recuperação). Detecção piggyback no `_periodic_full_sync` (gate de auth-degraded executado antes); requer `periodic_full_sync_seconds > 0` (validado no `config.load_config`). **Dual-clock (ADR-007)**: gate consulta monotonic (`time.monotonic()`, alinhado com ADR-004 — suspend congela com o processo, evita falso-positivo após suspend > threshold); reason em `min(elapsed_wall, elapsed_mono)` preserva "horas reais" cap por gap de cadência. Restart re-avalia a janela contra `_daemon_start_monotonic` (erra para falso-negativo pós-restart por até threshold ativas — folder com falha real preexistente fica invisível na primeira janela; trade-off aceito vs. falso-positivo de ADR-005 original). Estado in-memory (`_last_successful_sync_at`, `_last_successful_sync_at_mono`, `_degraded_folders`, `_daemon_start_time`, `_daemon_start_monotonic`).
- **Esgotamento inotify degrada para poll-only, não crasha** ([ADR-013](docs/decisions/ADR-013-degrade-poll-only-esgotamento-inotify.md)): `FilesystemWatcher.start()` converte `OSError` `ENOSPC`/`EMFILE` em `WatchLimitError` — try estreito na superfície inotify (`schedule`/`start`) somente, para não classificar disco-cheio como esgotamento de watches; libera watches parciais best-effort passo-a-passo (sem watcher parcial). `daemon._start_watcher` captura e segue **sem watcher** — workers + periodic full-sync operam, todo sync vem do ciclo periódico. Materialização de `local_path` ausente é do daemon (`_ensure_local_paths`, roda nos dois modos). Morte de observer/emitter **em runtime** é detectada por `_check_watcher_liveness` (piggyback no periodic) → mesmo degrade sinalizado. Sinalização tripla padrão: log `[WATCHER_DEGRADED]` + `notify-send` + `STATUS=degraded: watcher off (...) — poll-only a cada <N>s` (precedência STATUS: auth > watcher > folders). Gate: exige `periodic_full_sync_seconds > 0` — com periodic desligado o erro segue **fatal** (exit limpo rc=1; daemon up sem mecanismo de sync seria no-op silencioso). Sem auto-recover: recuperar tempo real = resolver a pressão (`fs.inotify.max_user_watches` ou reduzir escopo observado) + restart. Investigação: `journalctl --user -u drive-sync --grep "WATCHER_DEGRADED"`.
- **Case-sensitivity Path1↔Path2 é detectada config-time** ([ADR-011](docs/decisions/ADR-011-deteccao-de-case-duplicates-path1-path2-em-config-time.md)): `drive-sync --check` aborta quando scan recursivo do `folder.local_path` (depth = `git.max_recursion_depth`, default 6) detecta siblings com colisão case-insensitive (`name.lower()` repetido entre dirs e arquivos sob o mesmo `dirpath`). Proton Drive é case-insensitive — `family/` + `Family/` (ou `README.md` + `readme.md`) no FS local mapeiam para mesma entry no remote, gerando rclone safety abort `rc=7` ("they exist?") sem mensagem actionable. Cleanup é responsabilidade do operador (rename/merge/delete); sem escape hatch de policy (case-insensitive remote é fato semântico do remote, sem filtro `exclude:` que silencie o par sem o operador resolver no FS). Aplica apenas a `git_handling: auto|plain` (modos que bisync worktree); `bundle`/`skip` skipados. `.git/` inteiro fora do escopo (ADR-008 cobre estruturalmente). Comportamento `raise ValueError` em `load_config` é provisório enquanto ADR-011 está `Proposto` — direção falha-fast global vs skip-folder + degraded em aberto, §Alternativas articula as duas como paralelas.

## git_handling Semantics

Substitui `git_mode` legado ([ADR-008](docs/decisions/ADR-008-abandonar-bisync-repos-git.md)). Repos git com remote saem do sync (GitHub é o backup); repos local-only (sem remote) ganham bundle. Não-git permanece em bisync.

| Mode | What syncs | When to use |
|---|---|---|
| `auto` (default) | Scan `.git/` + `git remote -v` decide per repo descoberto | Folders com repos git mistos (default) |
| `skip` | Nada (folder pulado inteiro, marca sucesso) | Folder inteiro fora do escopo de sync |
| `bundle` | Apenas `.gitbundle` por repo | Folder com repo local-only ao nível raiz; ou histórico `.git/` na casa dos GB |
| `plain` | Tudo (worktree puro, sem excludes git) | Folders não-git (Documents, Pictures, library, videos) |

`auto` é o caminho-comum: loader varre `find_git_repos` (recursivo até `max_recursion_depth=6`); para cada repo, `git remote -v` vazio → bundle (no_remote), com remote → skip (has_remote). Override caso-a-caso via `repo_overrides: [{repo_subpath, mode: skip|bundle}]`. Bisync do conteúdo não-repo no folder usa `--exclude /<repo_subpath>/**` por repo classificado.

**Caveat WIP-cross-device:** o default histórico `bisync` (pré-ADR-008) era documentado como "preferred because it syncs uncommitted changes". Esse caminho deixa de existir em `auto`: repos com remote saem do sync. Operador que precisa de WIP-cross-device: (a) commit + push GitHub explícito; (b) `repo_overrides: [{repo_subpath: X, mode: bundle}]` força bundle (preserva HEAD + branches, mas não index/worktree). Gatilho de revisão registrado em ADR-008 §Gatilhos.

**Caveat proxy `git remote -v` falsificável:** classifier confia em "≥1 remote = backup externo existe". Remote configurado mas não-funcional (fork deletado, mirror read-only, nunca pushed) vira `skip` silencioso. Mitigação: log enriquecido `[REPO_SKIP] <repo> (has_remote: <url>)` permite operador grepar journal e detectar remotes suspeitos; override via `repo_overrides: [{repo_subpath, mode: bundle}]` força bundle quando operador sabe que o remote é fictício. `journalctl --user -u drive-sync --grep "REPO_"` dá visibilidade do dispatch.

**Caveat repo-em-repo:** se `folder.local_path` é ele próprio um repo E contém sub-repos aninhados, ambos são classificados separadamente — bundle do root captura conteúdo dos sub-repos como diretórios normais, sub-repos individuais também recebem bundle próprio (dupla cobertura). Configuração rara; aceita como trade-off conhecido.

**Flip detection:** estado in-memory `_last_classification: dict[folder_name, dict[repo_subpath, mode]]`; mudança de mode entre ciclos dispara log `[REPO_MODE_FLIP]` (WARNING) + `Notifier.repo_mode_flip` (notify-send). Primeiro ciclo pós-restart silencioso (estado vazio).

**Migração de config:** `git_mode` em qualquer valor (`bisync|bundle|off`) é rejeitado pelo loader (falha-fast simétrica). Playbook completo em [docs/operations/playbook-flip-git-handling.md](docs/operations/playbook-flip-git-handling.md).

Cada folder pode adicionalmente declarar `subpath_overrides: [{subpath, git_handling}]` para subpastas arbitrárias — expansão acontece em `load_config` e o runtime vê apenas a lista plana ([ADR-006](docs/decisions/ADR-006-git-mode-subpath-override.md)). Coexistência com `repo_overrides`: precedência por path descoberto (repo_overrides ganha em paths classificados como repo; subpath_overrides aplica fora dessa interseção).

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
