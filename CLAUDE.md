# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> **Constelação:** este repo é um nó da constelação pessoal do operador (hub: [`meta-system`](https://github.com/fppfurtado/meta-system)). Pertencimento + relações cross-repo em [`catalog-info.yaml`](catalog-info.yaml).

> A mneme é o KB soberano do operador (instância `~/mneme`). Quando a tarefa se beneficia de contexto/decisões prévias do ecossistema, consulte on-demand: `mneme index rebuild >/dev/null && mneme ground --query "<tema>" --json` (cada item vem com `trust`; `unverified` é status, não defeito).

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
- **bisync errors do NOT auto-recover** (com UMA exceção restrita — ver abaixo): erro genérico de bisync loga e segue; recovery manual `rclone bisync ... --resync` é o caminho. **Recovery playbook** (rc=7 stale-listings + rc=1 too-many-deletes, data-safe order): [docs/operations/playbook-bisync-recovery.md](docs/operations/playbook-bisync-recovery.md) — prefer deleting the per-pair marker to force a live-flags `--resync` over transcribing flags by hand. **Exceção restrita ([ADR-019](docs/decisions/ADR-019-auto-resync-gated-rc7-stale-listings.md), #47):** um abort **rc=7 stale-listings** (`cannot find prior Path1 or Path2 listings` — estado `.lst` morto, dados intactos) é **auto-recuperado gated-por-prova**: `bisync_folder` roda `--resync --dry-run`, e **só** se a saída provar que a união é no-op (0 transfers / 0 deletes — `_dryrun_resync_is_noop`) reconstrói o baseline com o `--resync` real; divergência real ou dry-run ambíguo → NÃO age, permanece degradado (fail-safe, preserva o espírito do invariante para o caso perigoso). 1-tentativa/episódio; tag `[BISYNC_AUTORESYNC]`; kill-switch `rclone.auto_resync_stale_listings` (default true). OUTRAS causas de rc=7 (case-duplicates ADR-011) e rc=1 too-many-deletes seguem manuais — mas o abort **rc=1 too-many-deletes** emite advice safe tagueado `[BISYNC_SAFETY_ABORT]` (#52): em vez de deixar só a dica `--force` do rclone (que propaga as deleções → perda de dados), loga um contra-advice apontando pro branch rc=1 do playbook (sinalização, **não** recuperação — divergência real segue manual, invariante preservado). Investigação: `journalctl --user -u drive-sync --grep "BISYNC_AUTORESYNC"` (auto-resync rc=7) · `--grep "BISYNC_SAFETY_ABORT"` (advice rc=1).
- **Error logs surface first `ERROR:` line + full stderr in `~/.local/state/drive-sync/`** ([ADR-012](../docs/decisions/ADR-012-captura-stderr-completo-rclone-per-call-site.md)): cada um dos 4 call-sites de erro de rclone em `sync_engine.py` (mkdir, bisync, upload-bundle, download-bundle) emite log no formato `[<folder>] [<TAG>] [<contexto opcional>:] <summary> (full stderr: <path>)` onde `<TAG>` ∈ `{MKDIR_FAIL, BISYNC_FAIL, BUNDLE_UPLOAD_FAIL, BUNDLE_DOWNLOAD_FAIL}` (uniformidade com `[AUTH_DEGRADED]`/`[FOLDER_DEGRADED]` de ADR-003/005). `<contexto opcional>` varia por call-site (ex.: `rc=%d` em bisync, `<src> → <dst>` em upload-bundle); `<summary>` é a primeira linha `ERROR:` do stderr; `<path>` é `~/.local/state/drive-sync/last-stderr-<op>-<folder_slug>[-<sub_slug>].log` com stderr completo (overwrite, retenção 1-run). `cat <path>` resolve forense em segundos. Fallback no `<summary>` ao tail-truncate (`err.strip()[-500:]`) quando stderr não contém linha `ERROR:` (ex.: rclone abort prematuro); o arquivo `<path>` é sempre escrito com stderr completo. Investigation primary: `journalctl --user -u drive-sync --grep "BISYNC_FAIL"`.
- **systemd unit hardening was relaxed** ([ADR-002](../docs/decisions/ADR-002-relaxar-hardening-systemd-protondrive.md)): `ProtectSystem=strict` was removed because it triggered spurious EROFS in rclone+protondrive on large folders. Don't re-add without re-running the ADR's experiment.
- **Daemon degraded em falha de auth (complementa, não substitui, o invariante `bisync errors do NOT auto-recover`)** ([ADR-003](../docs/decisions/ADR-003-type-notify-sinalizacao-degraded.md)): erros bisync genéricos continuam logando e seguindo; apenas falha de auth classificada (pares `(Code, Status)` em `_AUTH_CODES` no `sync_engine.py`, endpoint `/auth/v4` com ou sem prefixo `/api/`) dispara pausa global dos workers e sinalização via `systemctl status` (`STATUS=degraded: ...`), `notify-send` e log tagueado `[AUTH_DEGRADED]`. Recuperação manual depende do `kind` reportado: `invalid_credentials`/`captcha_required` → **reauth interativo** (`rclone config reconnect proton:` — TOTP usado **ao vivo** na janela de ~30s, **nunca** persistido no config; [ADR-017](docs/decisions/ADR-017-campo-2fa-nunca-persistido-flag-override.md)) + `systemctl --user restart drive-sync`; `refresh_token_invalid` → restart simples (auto-resolve no próximo ciclo); `rate_limited` → aguardar gate da Proton relaxar (~30-60min sem tráfego) antes do restart, evitar marteladas que aprofundam o gate. Sem auto-resume em nenhum caso **para kinds de credencial genuína** — flakiness lateral da Proton pode mascarar problemas residuais. **NÃO usar `rclone config update proton 2fa <code>`** (o antigo recovery): o daemon força `--protondrive-2fa ""` em toda chamada ([ADR-017](docs/decisions/ADR-017-campo-2fa-nunca-persistido-flag-override.md)), tornando qualquer `2fa` persistido inerte — um campo estático nunca serviu no cold reauth (TOTP expira em ~30s) e só plantava o `8002` enganoso. **Exceção restrita ([ADR-016](docs/decisions/ADR-016-classe-proton-infra-auto-resume-gated.md)):** um `8002`/erro-auth **colateral de um storm de 5xx** (janela deslizante em `sync_engine`) é reclassificado para `kind=proton_infra` (recovery = aguardar, sem reauth) e **auto-resume gated por probe** (sucesso `rc==0` retoma; falha persistente com provedor saudável escala para `auth_uncertain` e permanece pausado). Unifica auth-`8002` (#35) e block-`502/504` (#46). Config: `rclone.infra_storm_threshold` (5), `rclone.infra_window_seconds` (600).
- **Cooldown por folder gate-keia também o periodic full-sync** ([ADR-004](../docs/decisions/ADR-004-cooldown-gate-periodic-full-sync.md)): `cooldown_seconds` em `FolderConfig` é opt-in (default 0 = desligado). Quando > 0, o gate é avaliado no worker (`daemon.py:_worker`) **antes** do `_inflight_lock` e absorve tanto eventos do watcher quanto ciclos da safety-net `watcher.periodic_full_sync_seconds` para aquela pasta. Janela conta from-start (`_last_sync_at` é setado antes do `_process_folder`), não from-finish — falha cedo não estende a janela. Estado in-memory (`_last_sync_at`, `_cooldown_scheduled`, `_cooldown_tasks`); sem persistência cross-restart — restart gera no máximo 1 upload extra. Motivação principal: `git_mode: bundle` em repo com `.git/` na casa dos GB (rclone não faz delta upload de blob no backend protondrive).
- **Staleness per-folder sinaliza degraded sem pausar** ([ADR-005](../docs/decisions/ADR-005-folder-staleness-degraded.md) + [ADR-007](../docs/decisions/ADR-007-staleness-monotonic-suspend-aware.md)): pasta sem `_process_folder` retornando sucesso há mais que `watcher.folder_staleness_threshold_seconds` (default 12h, opt-out via 0) entra em `_degraded_folders` e dispara `Notifier.folder_degraded` (log `[FOLDER_DEGRADED]` + `notify-send`) + `STATUS=degraded folders: <lista>` via sd_notify. **Distinto de ADR-003** (auth global com pausa) — staleness é per-folder, sem pausa de workers; STATUS é agregada no daemon (`_compose_status_payload`) com precedência auth > folder. Reset por sucesso (silencioso, sem `notify-send` de recuperação). Detecção piggyback no `_periodic_full_sync` (gate de auth-degraded executado antes); requer `periodic_full_sync_seconds > 0` (validado no `config.load_config`). **Dual-clock (ADR-007)**: gate consulta monotonic (`time.monotonic()`, alinhado com ADR-004 — suspend congela com o processo, evita falso-positivo após suspend > threshold); reason em `min(elapsed_wall, elapsed_mono)` preserva "horas reais" cap por gap de cadência. Restart re-avalia a janela contra `_daemon_start_monotonic` (erra para falso-negativo pós-restart por até threshold ativas — folder com falha real preexistente fica invisível na primeira janela; trade-off aceito vs. falso-positivo de ADR-005 original). Estado in-memory (`_last_successful_sync_at`, `_last_successful_sync_at_mono`, `_degraded_folders`, `_daemon_start_time`, `_daemon_start_monotonic`).
- **Max-runtime kill switch por job rclone** ([ADR-018](docs/decisions/ADR-018-max-runtime-kill-switch-por-job-rclone.md)): um job rclone que ultrapassa `rclone.max_job_runtime_seconds` (default 7200/2h; 0 desliga; override per-`FolderConfig.max_job_runtime_seconds`, `None`=herda/`0`=off) é morto por `_run` (SIGTERM → graça `_STUCK_JOB_GRACE_SECONDS`=10s → SIGKILL, sempre com reap) que levanta `StuckJobError`. **Matar o processo no `_run`, não cancelar a corrotina** — o subprocess é quem segura o lock serializado (ADR-001); corrotina cancelada deixaria processo órfão segurando o lock. Timeout aplicado em `bisync`/`mkdir`/`copyto`-de-bundle; `about` do `auth_probe` fica **sem** timeout. Daemon captura `StuckJobError` em **`_process_folder`** (cobre `_worker` E `_periodic_full_sync`) → log `[STUCK_JOB]` + folder em `_degraded_folders`+`folder_degraded` (**reusa infra ADR-005, per-folder, SEM pausa global** — distinto de ADR-003; o kill já liberou o lock). Sem auto-resume (restart manual; sucesso limpa degraded). Motivação: incidente 2026-05-29 (bisync sem excludes segurou o lock 14h; ADR-005 sinalizou mas não agiu). Footgun: folder cujo sync legítimo excede o limite é morto todo ciclo → visível como `[STUCK_JOB]`, mitiga com override per-folder. Investigação: `journalctl --user -u drive-sync --grep "STUCK_JOB"`.
- **Esgotamento inotify degrada para poll-only, não crasha** ([ADR-013](docs/decisions/ADR-013-degrade-poll-only-esgotamento-inotify.md)): `FilesystemWatcher.start()` converte `OSError` `ENOSPC`/`EMFILE` em `WatchLimitError` — try estreito na superfície inotify (`schedule`/`start`) somente, para não classificar disco-cheio como esgotamento de watches; libera watches parciais best-effort passo-a-passo (sem watcher parcial). `daemon._start_watcher` captura e segue **sem watcher** — workers + periodic full-sync operam, todo sync vem do ciclo periódico. Materialização de `local_path` ausente é do daemon (`_ensure_local_paths`, roda nos dois modos). Morte de observer/emitter **em runtime** é detectada por `_check_watcher_liveness` (piggyback no periodic) → mesmo degrade sinalizado. Sinalização tripla padrão: log `[WATCHER_DEGRADED]` + `notify-send` + `STATUS=degraded: watcher off (...) — poll-only a cada <N>s` (precedência STATUS: auth > watcher > folders). Gate: exige `periodic_full_sync_seconds > 0` — com periodic desligado o erro segue **fatal** (exit limpo rc=1; daemon up sem mecanismo de sync seria no-op silencioso). Sem auto-recover: recuperar tempo real = resolver a pressão (`fs.inotify.max_user_watches` ou reduzir escopo observado) + restart. Investigação: `journalctl --user -u drive-sync --grep "WATCHER_DEGRADED"`.
- **Watchdog externo re-emite alerta enquanto o backup estiver ruim** ([ADR-014](docs/decisions/ADR-014-watchdog-dead-mans-switch-externo.md)): `drive-sync-watchdog.timer` (30min, `Persistent=true`) roda `drive-sync --watchdog` — 3 checks: serviço não-active (domina os demais) · `StatusText` degraded · frescor dos success markers (threshold reusa `folder_staleness_threshold_seconds`; marker ausente só alarma com serviço active há > threshold). Level-triggered por design: notify-send crítico RE-EMITIDO a cada ciclo (o fix do silêncio de 7 semanas do #19 — transição perdida não vira silêncio eterno) + journal da unit + exit 1 (`systemctl --user --failed`). Sinal, não ação — sem auto-remediação. Parada deliberada do daemon também alarma; manutenção planejada = parar também o timer. Investigação: `journalctl --user -u drive-sync-watchdog`.
- **Case-sensitivity Path1↔Path2 é detectada config-time** ([ADR-011](docs/decisions/ADR-011-deteccao-de-case-duplicates-path1-path2-em-config-time.md)): `drive-sync --check` aborta quando scan recursivo do `folder.local_path` (depth = `git.max_recursion_depth`, default 6) detecta siblings com colisão case-insensitive (`name.lower()` repetido entre dirs e arquivos sob o mesmo `dirpath`). Proton Drive é case-insensitive — `family/` + `Family/` (ou `README.md` + `readme.md`) no FS local mapeiam para mesma entry no remote, gerando rclone safety abort `rc=7` ("they exist?") sem mensagem actionable. Cleanup é responsabilidade do operador (rename/merge/delete); sem escape hatch de policy (case-insensitive remote é fato semântico do remote, sem filtro `exclude:` que silencie o par sem o operador resolver no FS). Aplica apenas a `git_handling: auto|plain` (modos que bisync worktree); `bundle`/`skip` skipados. `.git/` inteiro fora do escopo (ADR-008 cobre estruturalmente). Comportamento `raise ValueError` em `load_config` é provisório enquanto ADR-011 está `Proposto` — direção falha-fast global vs skip-folder + degraded em aberto, §Alternativas articula as duas como paralelas.
- **Órfãos de cobertura são avisados (warn, não fatal) config-time** ([ADR-015](docs/decisions/ADR-015-audit-cobertura-orfaos-config-time.md)): `drive-sync --check` roda `audit_coverage_orphans` (em `config.py`) — para cada diretório-**pai** de um `local_path` configurado, sinaliza filhos-diretório **com conteúdo** que nenhum folder cobre e não estão em `coverage_audit.allow` ("órfãos de cobertura" — conteúdo local não backup-eado). **Classe INVERSA de ADR-010/011**: aqueles validam malformação DENTRO de um folder declarado (fatal, causa rc=7); este pega conteúdo **não-declarado** (warn — omissão não quebra sync algum). `git_handling` é **ortogonal** (todo declarado é "conhecido", inclusive `bundle`/`skip`). Sinal, não ação: operador cobre (novo `folders:`) ou exclui (`coverage_audit.allow`, casa exato ou subpath). Opt-out via `coverage_audit.enabled: false`. Só config-time (arm runtime born-after-config deferido/armado, irmão do #37); ponto-cego = top-level totalmente novo sem sibling configurado (modelo A deferido, gate = #55). Motivação: `pictures/Screenshots` vivo fora do backup por ~3 meses (#54).

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

**Worktrees linkadas ficam fora do bundling** (#24): `.git` ARQUIVO com `gitdir → .git/worktrees/<n>` → `classify_repos` classifica `skip` estrutural (reason `linked_worktree`, sem consultar remote — segue virando `--exclude` no bisync, invariante ADR-008 preservado) e `_sync_git_folder` (modo `bundle` folder-level) filtra do bundling. Racional: história/branches vivem no repo principal (bundle do principal já captura os refs); bundle de worktree é duplicação GB-escala de estado efêmero. Submodule (`gitdir → .git/modules/<n>`) NÃO é afetado. `repo_overrides` mantém precedência total (força bundle de worktree se o operador quiser).

**Flip detection:** estado in-memory `_last_classification: dict[folder_name, dict[repo_subpath, mode]]`; mudança de mode entre ciclos dispara log `[REPO_MODE_FLIP]` (WARNING) + `Notifier.repo_mode_flip` (notify-send). Primeiro ciclo pós-restart silencioso (estado vazio).

**Migração de config:** `git_mode` em qualquer valor (`bisync|bundle|off`) é rejeitado pelo loader (falha-fast simétrica). Playbook completo em [docs/operations/playbook-flip-git-handling.md](docs/operations/playbook-flip-git-handling.md).

Cada folder pode adicionalmente declarar `subpath_overrides: [{subpath, git_handling}]` para subpastas arbitrárias — expansão acontece em `load_config` e o runtime vê apenas a lista plana ([ADR-006](docs/decisions/ADR-006-git-mode-subpath-override.md)). Coexistência com `repo_overrides`: precedência por path descoberto (repo_overrides ganha em paths classificados como repo; subpath_overrides aplica fora dessa interseção).

## Config Location

Runtime config: `~/.config/drive-sync/config.yaml` (or `$XDG_CONFIG_HOME/drive-sync/config.yaml`).

The reference config at [config/config.yaml.example](config/config.yaml.example) shows the full schema with all available options. Every section except `folders` is optional and has defaults in the dataclasses.

## Key Paths (Runtime)

- Config: `~/.config/drive-sync/config.yaml`
- Logs: `~/.local/state/drive-sync/drive-sync.log`
- bisync state markers: `~/.cache/rclone/bisync/`
- Git bundles (bundle mode): `~/.cache/drive-sync/bundles/<task>/<rel>.gitbundle`
- systemd unit: `~/.config/systemd/user/drive-sync.service`

## Decision Log & Backlog

- ADRs: `docs/decisions/ADR-*.md` — durable architectural decisions.
- Plans: `docs/plans/*.md` — pre-fact implementation plans, kept after merge.
- Tracker (trabalho aberto): **GitHub Issues** (`gh issue list`, label `backlog`) — migrado do `BACKLOG.md § Próximos` em 2026-08-24 (issues #35–#47). Filar novo item: `gh issue create` ou `/backlog capture`.
- Backlog: `BACKLOG.md` — `## Próximos` é só um ponteiro para o GitHub Issues; `## Concluídos` permanece como memória institucional (do not prune).

<!-- agent-kit operational-floor v12 — single source: agent-kit/onboarding/operational-floor.md
     Copy this whole block into your repo's own AGENTS.md / CLAUDE.md, below your own content.
     Extend BELOW the closing marker; never edit INSIDE the block. Re-copy when the version bumps.
     Distillation baseline — last verified faithful against the maintainer's root operational-floor
     section on 2026-07-17. v2 (2026-07-20) adds the `.worktrees/<slug>` worktree-location convention to
     the Session isolation rung, aligning it with the maintainer root floor (#338). v3 (2026-07-24)
     adds apply-`.worktreeinclude` (copy, not symlink) after every worktree create (#431). v4 (2026-07-28)
     adds `git submodule update --init` after worktree create for repos with a `.gitmodules` (#492).
     v5 (2026-08-03) adds the canonical-path-bound-tooling carve-out to the Session isolation rung (branch
     in-place where the tool reads a configured canonical path, e.g. a chezmoi source dir; #606). v6
     (2026-08-05) generalizes the Cycle-close capture sweep from "before landing" to the close of ANY
     session — a landing-free analysis/hygiene pass owes a disposition for each gap it surfaces too (#635).
     v7 (2026-08-10) sharpens the Cycle-close capture sweep from spirit to a checkable step: disposition is
     per-item to a durable surface (an aggregate "all handled" and a chat/close-summary/un-attested-prose
     mention are not durable), so an odd-class follow-up cannot slip through bulk reasoning (#708). v8
     (2026-08-16) adds the governing-Spec scan sub-step to the Declare-the-route rung: before a skip-Spec
     lightweight route commits, scan for an existing frozen Spec governing the touched files (#781). v9
     (2026-08-16) gives the Cycle-close capture sweep its during-flow FEED — the session findings ledger,
     appended at detection so process-class findings survive to close (#863) — and aligns the spec-file
     pattern list to the tools' full set (adds `spec_*.md`). v10 (2026-08-17) extends the
     Verify-before-destructive rung with the repo-archive case — verify `HEAD == @{u}` + a clean tree
     before archiving a repo that serves as a delete-backstop (#852; evidence mneme#194). v11
     (2026-08-17) adds the teardown half to the Session isolation rung: sweep gitignored working state
     out of a session worktree BEFORE removal — a bare `git worktree remove` silently deletes it (#840;
     evidence #798, n=3). v12 (2026-08-18) adds the Stage-by-explicit-paths rung: stage changed paths by
     name, never a broad `git add -A`/`add .`, so a stale worktree copy cannot silently revert a sibling
     session's landed work (#888).
     This generic block intentionally omits the maintainer-internal
     release/attestation rungs (a per-PR method self-check, issue-close-evidence, the PR attestation
     lines, and release-due surfacing for published units) — they depend on maintainer-specific tooling
     a norm-blind repo does not have. Re-verify when that floor section changes; the version bumps only
     when a *copied rung* changes (so an unchanged re-copy is never forced). -->

## Working norm (operational floor)

This repo follows the **throughline** working discipline. The rungs below are the always-on floor: they
bind every session that mutates git — from a one-line fix to a large build — and are **not** scaled down by
effort size (a smaller effort takes fewer steps, never a thinner floor). They run as **self-checks**; where
a deterministic guard is installed (see the last rung) it enforces the same rung, but the behavior binds
with or without it.

- **Session isolation.** <!-- rung:session-isolation --> Work in a dedicated git worktree — created **under `.worktrees/<slug>` inside the
  repo** (a gitignored directory), on a feature branch — from session start, solo included. The
  main/default worktree stays a neutral base: no session does feature work on it. Never run two
  git-mutating sessions against one working tree. If you start and find the base on another session's
  branch (or dirty with foreign work), spawn your own worktree — do not work there. After
  `git worktree add`, if the repo root has a `.worktreeinclude`, **copy** each listed path from the main
  worktree into the new one (copy, not symlink; halt if a listed path is missing in main) — gitignored
  agent context (`AGENTS.md`, `CLAUDE.md`, …) does not travel with the worktree otherwise. If the repo has
  a `.gitmodules`, also run `git submodule update --init --recursive` in the new worktree — submodules are
  **versioned** (a gitlink to a pinned commit) that `git worktree add` does **not** init, so a build that
  depends on them fails otherwise; **init, don't copy** (they are not `.worktreeinclude` entries).
  **Teardown is sweep-then-remove:** before removing a session worktree, sweep its gitignored working
  state OUT to the main tree — `git worktree remove` refuses on tracked modifications but **silently
  deletes gitignored files** (the in-worktree `.throughline/dossiers/`, plus any path a tracked
  `.worktreesweep` manifest at the repo root lists). A throughline install ships the sweep
  (`python3 sub-tools/worktree_sweep.py --repo <main-root> --worktree <worktree-path>`); without it,
  copy those paths out by hand before `git worktree remove`.
  **Carve-out — canonical-path-bound tooling:** for a repo whose tooling reads a *configured canonical
  absolute path* rather than the working tree (e.g. a chezmoi source dir), a `.worktrees/<slug>` worktree
  relocates edits where the tool never looks (no isolation, actively misleading) — **branch in-place** in
  the canonical dir instead; PR-per-cycle + the concurrency check still bind (guard the concurrency check —
  a bare pull/checkout on the shared dir can collide).
- **Stage by explicit paths.** <!-- rung:stage-by-explicit-paths --> Stage what you changed by naming the paths (`git add <path> …`) — never a
  broad `git add -A` / `git add .`. A broad add re-stages whatever the worktree's index happens to hold, so
  in a parallel-session repo a stale copy of a file a *sibling* session already landed gets silently
  re-committed at its old contents — reverting that sibling's work, with no merge conflict to warn you.
  Review `git status` / `git diff --cached` before committing; stage additions and deletions explicitly.
- **Concurrency check at pickup.** <!-- rung:concurrency-check-at-pickup --> Before working an existing tracker item, check whether a live session
  already owns it (a sibling worktree/branch for it, or a frozen design / open PR on the item). If one
  exists, stand down — do not produce competing artifacts; defer or coordinate.
- **Issue-first.** <!-- rung:issue-first --> A significant effort — one worth framing, or expected to outlive a session — opens its
  tracker item BEFORE work begins, so the effort is visible from the start and survives a dead session.
- **Declare the route before solution mechanics.** <!-- rung:declare-the-route-before-solution-mechanics --> For each item you pick up, name the route on two axes —
  problem space (frame the problem, vs work from an already-frozen problem brief) and solution space
  (a lightweight build, vs a full design pipeline) — each with a one-line reason, *before* touching the
  solution. A trivial fix names its door too; "worked ad-hoc, through no door" is never a valid route.
  Before committing a "lightweight build, skip Spec" route, check whether an existing frozen Spec already
  governs the code the change touches — a throughline install ships the deterministic scan
  (`spec_surface.py match --repo . --paths <touched files>`); without it, scan the repo's spec files
  (`spec-*.md` / `spec_*.md` / `*.spec.md`) by hand. One found → the route defaults to *amend that Spec*, not skip it.
- **PR-per-cycle; the agent never merges.** <!-- rung:pr-per-cycle-the-agent-never-merges --> Every cycle lands via a pull/merge request off the feature
  branch — never a direct push to the default branch. The agent stops at a merge-ready PR (checks green,
  review attested) and hands off; the **human performs the merge** (the terminal, least-reversible step).
- **Review before land (the land-gate).** <!-- rung:review-before-land-the-land-gate --> Built code lands on the default branch only after the judgment
  review pass ran — attested — or a reasoned waiver is on record. The bar is *ran-or-waived*, not
  findings-resolved: acting on findings stays human judgment; the gate only ensures review was not silently
  skipped.
- **Cycle-close capture sweep.** <!-- rung:cycle-close-capture-sweep --> An error, gap, drift, or optimization candidate detected MID-flow
  appends ONE line, at detection, to the session findings ledger — `.throughline/dossiers/session-findings-<slug>.md`
  in the working worktree (a worktree-less or in-place session → the same path at the repo root):
  create the directory if absent and keep it gitignored (a
  `.throughline/.gitignore` carrying `/dossiers/`); it is a CONVENTION, not a tool dependency — with no
  throughline install, any gitignored session-notes file serves the same role. A line is owed when the
  finding is not already tracked, outlives the session or names a class, and was not fixed in-flow with
  zero class-residue — so the close sweep reads a FILE, never working memory. Before removing the
  session worktree, sweep the ledger's lines to their dispositions (or copy the file out) — a bare
  worktree removal deletes it. At the close of ANY session — a landing cycle OR a
  landing-free analysis/review/hygiene pass that lands nothing — sweep the ledger PLUS any follow-ups, drifts, and gaps recalled at close (the ledger augments the
  sweep's feed, it never bars a late entry) and
  dispose each **per item, to a durable surface**: enumerate each surfaced follow-up (an odd-class one — a
  one-off "run `X`" mechanical step — counts too) and name where it durably lands (a filed tracker item, or a
  recorded reason it needs none), never swallowed — whether or not the session lands. An **aggregate** "all
  handled" and a mention in **chat, an ephemeral close summary, or un-attested prose** are NOT durable
  dispositions: reasoning over the class lets an odd-class item slip and land nowhere. A landing cycle records
  each where the change lands; a landing-free pass records each in the pass's own output.
- **Verify before any destructive action.** <!-- rung:verify-before-any-destructive-action --> Before `rm` / overwrite / `clean`, verify the target against
  the real tree (tracked-files / status / an explicit path check) — never a bare directory listing, which
  goes stale across branch churn. Before `git stash pop`/`apply`/`drop`, check `git stash list` and target
  an explicit `stash@{n}` (the stash stack is shared across all worktrees, so a bare pop may apply an alien
  stash onto the wrong tree); prefer read-only inspection when you only need to look. Same class —
  archiving a repo that serves as a delete-backstop (`gh repo archive` / forge Settings → Archive):
  archiving freezes the remote history read-only — verify `HEAD == @{u}` and a clean tree (nothing
  load-bearing untracked or uncommitted) BEFORE archiving, else the backstop freezes incomplete history
  and "delete-because-backed-up" stops being true.

**The methodology.** The full working cycle is **throughline** — one guided front door (**frame** the
problem) → solution design → **build** → **review** — available as installed agent skills. Bring a problem
to `frame`; it routes the rest. Diagnosis has its own depth branch (`debug`). Do not reach for a solution
before the problem is framed.

**Deterministic enforcement (optional; the floor degrades gracefully).** The rungs above are self-checks and
bind everywhere. Where you want a *hard* floor, add the deterministic layers separately: a pre-commit guard
that refuses commits on the default branch, a pre-push / CI land-gate that blocks unreviewed
throughline-built code (use the land-gate CI recipe your throughline install ships), and your forge's branch
protection. These are per-harness / per-platform add-ons, not a dependency of the norm — where they are
absent, the self-checks still bind.

<!-- /agent-kit operational-floor v12 -->
<!-- agent-kit session-boundary ritual v13 — single source: agent-kit/onboarding/session-boundary-ritual.md
     OPERATOR-PERSONAL extension (it names my own ecosystem tools — mneme, backlog, and the
     agent-kit substrate itself — and is agent-kit-local, never published to strangers). Copy this whole
     block into your repo's own AGENTS.md / CLAUDE.md, BELOW the operational-floor block's closing marker
     (this is the "extend below the marker" seam — it is deliberately NOT part of the minimal shared floor).
     Extend BELOW this block's own closing marker; never edit INSIDE the block. Re-copy when the version
     bumps.

     v13 (agent-kit #856 — mneme#194 Logseq-triad cutover): dropped the retired `meta-bridge` from the
     named ecosystem tools above; it is no longer a live surface. No operative change — the stub body
     enumerates no ritual axes (it points to the manifest); the correction is to this provenance header only.

     v12 (Spec session-ritual-delivery v2 — the #770 instance-1 reprocess of #771/#768): this block is now
     a STUB, not the enumeration. The authoritative, machine-readable enumeration of every ritual axis —
     both bounded contexts, with per-axis presence conditions, actions, write-twins, guidance depth, and
     the close checkpoint's deterministic signals — is the RITUAL MANIFEST:
       deployed:  ~/.claude/hooks/ritual-manifest.md   (read this one at runtime)
       canonical: agent-kit/onboarding/ritual-manifest.md
     Do NOT restate axes here or anywhere else — one home per context, by construction. -->

## Session-boundary ritual (open + close)

At the START and the END of a work session, execute the session-boundary ritual from its
**manifest** — `~/.claude/hooks/ritual-manifest.md` (fallback when a machine has no deploy: the
canonical `agent-kit/onboarding/ritual-manifest.md`). The ritual is **assisted, not automatic**:
entries surface and enumerate; judgment stays in the loop (`awaiting-confirmation` is a valid
disposition). **Graceful skip** is per-entry, via each entry's `presence:` condition — an absent
surface is an explicit `no-op`, never an error and never a silent omission.

- **OPEN:** the `phase: open` entries normally arrive ALREADY INJECTED in context by the
  `ritual_open_surface.py` SessionStart hook (mechanical delivery — session-start momentum cannot
  skip it). Execute the injected checklist before any front-door skill or first work act, reporting
  each entry inline. If no checklist was injected (hook unwired / manifest absent on this machine),
  read the manifest's open entries directly and do the same.
- **CLOSE:** on the operator's wrap-up cue, compose BOTH contexts from the manifest:
  1. the `repo-close` entries — the loose-end sweep entrypoint (`backlog:session-close` or this
     repo's equivalent) plus the pointer entries the repo's own floor/CI already enforce;
  2. the `operator-boundary` `phase: close` entries — executed per their manifest guidance.
  Emit ONE disposition line per axis (both contexts) in a fenced `ritual-close-dispositions` block:
  `<axis-id>: ran|skipped-with-reason|no-op|awaiting-confirmation [— reason]`. An omitted axis is a
  violation; an aggregate "all clear" is non-conforming.
- **Backstop:** the deterministic close checkpoint (SessionEnd → next SessionStart) reads the SAME
  manifest's embedded signals — a missed write-back axis surfaces at the next open; dispose it then.

<!-- /agent-kit session-boundary ritual v13 -->
