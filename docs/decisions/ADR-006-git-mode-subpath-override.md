# ADR-006: Override de `git_mode` por subpath via expansão config-time (`subpath_overrides`)

**Data:** 2026-05-18
**Status:** Proposto

## Origem

- **Decisão base:** [ADR-004](ADR-004-cooldown-gate-periodic-full-sync.md) — pattern de campo opt-in em `FolderConfig` que altera comportamento sem mudar contratos de runtime; este ADR estende o pattern com schema novo (`subpath_overrides`) também em `FolderConfig`.
- **Investigação:** /debug e workaround em 2026-05-13/14 — `tjpa/pje-2.1` (sob `dev-projects` com `git_mode: bisync`) acumulou 3 tentativas de `rclone bisync --resync` falhando ~19h totais com `Code=2003`/`Code=2500`/`Code=2501`/`gopenpgp` no backend protondrive, enquanto outros subrepos do mesmo folder funcionaram de primeira. Workaround atual: extrair como folder top-level próprio com `git_mode: bundle` + exclude `tjpa/pje-2.1/**` no parent.

## Contexto

`FolderConfig` hoje tem `git_mode` único por folder (`off`/`bisync`/`bundle`). Repos com `.git/` cronicamente problemático no protondrive precisam cair em `bundle`; mas extrair o subdir como folder top-level próprio paga dois custos:

1. **Poluição visual no config:** duas entries com `local_path`/`remote_subpath` semi-redundantes (a sub é prefixo da parent), exclude manual `<subpath>/**` no parent. O fato de que "esta entry é override daquela" não é capturado — só sobreposição de paths.
2. **Acoplamento implícito de manutenção:** mover/renomear o parent exige editar ambas as entries de forma sincronizada.

Pré-requisito cumprido: [ADR-004](ADR-004-cooldown-gate-periodic-full-sync.md) (cooldown_seconds por folder) — sem ele, bundle re-uploadar a cada evento do watchdog seria proibitivo em repo `.git/` na casa dos GB.

## Decisão

Adicionar campo opt-in `subpath_overrides: list[SubpathOverride]` em `FolderConfig`, onde `SubpathOverride` carrega apenas `subpath: str` e `git_mode: str`. **Expansão acontece em `load_config`**: para cada override, criar um `FolderConfig` synthetic herdando do parent **exceto** `git_mode` (e ajustando `local_path`/`remote_subpath`/`name`/`fs_key`), e injetar automaticamente `f"{override.subpath}/**"` no `parent.exclude` (com log WARNING se o operador já declarou explicitamente — não duplica nem falha). Runtime (watcher, daemon, sync_engine, git_handler) **não sabe da existência de overrides** — vê apenas a lista plana de folders e roteia normalmente via `owning_folder()` do watcher (que já elege o ancestral mais específico).

Synthetic folder herda `enabled` do parent — desabilitar o parent desabilita o override implicitamente. Operador que precisa desligar apenas o override mantém o workaround folder-top-level (ou alterna explicitamente `enabled: false` no parent + duas entries top-level, caso o cenário emerja empiricamente).

`FolderConfig` ganha campo `fs_key: str` separado de `name`. **`name`** preserva a hierarquia (`dev-projects/tjpa-pje-2.1`) para identidade visível em logs, journal, queue, `_inflight`, `_degraded_folders`. **`fs_key`** é o slug filesystem-safe (`dev-projects-tjpa-pje-2.1`, com `/` → `-`) usado por callers que constroem paths em disco (`git_handler.bundle_path_for` via `bundles_dir / folder.fs_key`). Folder declarado direto no YAML tem `fs_key == name` (default não-disruptivo); apenas synthetic produz nome com `/`, e o loader popula `fs_key` automaticamente.

Razões:

- **Zero acoplamento de runtime**: o modelo "syntactic sugar" no loader preserva contratos existentes. Watcher/daemon/sync_engine não ganham branch novo. Risco de regressão localizado em `config.py` e validado por testes unitários do loader.
- **Resolve a dor concreta** sem inventar feature genérica: o caso `tjpa/pje-2.1` precisa só de `git_mode` distinto. A poluição visual de duas entries top-level vai embora.
- **Reutiliza `owning_folder()`**: dedup de paths já elege o ancestral mais específico. Folder synthetic com `local_path` aninhado entra no algoritmo sem modificação.
- **YAGNI explícito em outros campos**: cooldown_seconds, auto_exclude, exclude, debounce_seconds NÃO são overrideable. Operador que precisa de cooldown distinto continua com o workaround atual (folder top-level) — caso raro, paga a dor onde paga. Gatilho de revisão registrado.
- **Schema list-of-objects > dict path→mode**: a forma `subpath_overrides: [{subpath, git_mode}]` admite extensão futura (campos novos em `SubpathOverride`) sem migração breaking. O dict `subpath_modes: {path: mode}` seria mais conciso mas só permite override de `git_mode`; pé-de-pato pra qualquer extensão.

## Consequências

### Benefícios

- Caso `tjpa/pje-2.1` deixa de poluir o YAML com duas entries; vira 2 linhas dentro do `dev-projects`.
- Relação semântica "sub é override de parent" é capturada explicitamente no YAML.
- Cooldown_seconds do parent é herdado pelo synthetic — operador não precisa duplicar.
- Mover/renomear parent não exige tocar em entry separada.
- Runtime inalterado → risco de regressão concentrado em `config.py`.
- Sinalização de staleness ([ADR-005](ADR-005-folder-staleness-degraded.md)) opera per-folder pós-expansão — synthetic pode entrar em `[FOLDER_DEGRADED]` sem afetar o parent (e vice-versa).

### Trade-offs

- `cooldown_seconds` por override não é configurável (herda do parent). Operador que precisa cooldown distinto mantém o workaround folder-top-level. Aceitável: caso real (tjpa/pje-2.1) compartilha cooldown 10800s com `dev-projects` parent.
- `FolderConfig` ganha campo extra `fs_key` por causa do `/` no `name` synthetic. Trade-off vs. alternativas: (a) sanitizar globalmente `name` para `-` perderia hierarquia no log; (b) deixar `/` no `name` e sanitizar nos callers espalha lógica de sanitização. `fs_key` separado preserva log bonito ao custo de 1 campo dual-purpose; folders declarados direto no YAML têm `fs_key == name`, então o campo é invisível na esmagadora maioria dos casos.
- Operador que declara `subpath_overrides` E `exclude: <subpath>/**` simultaneamente não falha — loader injeta apenas se ausente, e emite log WARNING informando que o exclude manual virou redundante. Diferença consciente vs. ADR-005 (que rejeita estritamente `staleness>0 + periodic=0`): aqui a migração do workaround é cosmética (operador pode trazer o exclude antigo durante a transição sem quebrar config), e o WARNING dá explicitness sem ser fail-loud. Sinal objetivo: `journalctl --user -u drive-sync --grep "exclude redundante"`.

### Limitações

- Sem nested overrides. Loader rejeita `subpath_overrides` em folder synthetic (não-recursivo); operador não pode override de override. Aceitável — caso não emerge empiricamente.
- Sem overlapping subpaths no mesmo parent. Loader rejeita pares `(a, a/b)` ou `(a/b, a)` com erro `"sobreposição não permitida"`. Aceitável — sobreposição produziria globs redundantes no `parent.exclude` (`a/**` cobrindo `a/b/**`) e ambiguidade de routing resolvida só implicitamente por `owning_folder()`; rejeitar é mais claro. Caso emerja empiricamente, reabrir e considerar delegação a `owning_folder()`.
- Sem auto-fallback após N falhas. Decisão consciente: config explícito > inferência (alinha com a doutrina do projeto). Operador escolhe `git_mode: bundle` quando aprende que o subpath é problemático.
- Migração do config local é manual (operador edita `~/.config/drive-sync/config.yaml` pós-merge). O config de exemplo em `config/config.yaml.example` é atualizado pelo plano para mostrar a forma nova; não há script de migração automática (config local é gitignored, fora do escopo do código).

## Alternativas consideradas

### Schema dict `subpath_modes: {<path>: <mode>}`

Mais conciso (1 linha por override no YAML), mas só permite override de `git_mode`. Qualquer extensão futura (override de outro campo, metadata por entry) exigiria dict-de-dict ou migração breaking pra lista. **Recusada** — pé-de-pato no schema. List-of-objects custa 3 linhas a mais por override e admite extensão linear.

### Override múltiplos campos (git_mode + auto_exclude + exclude + cooldown_seconds)

Mais flexível mas paga complexidade: 4 campos com merge logic per override, decisões de "campo ausente herda do parent vs vira default da dataclass" pra cada um. Justificável só se houver caso real. **Recusada por YAGNI** — caso atual (`tjpa/pje-2.1`) só precisa de `git_mode`. Gatilho de revisão registrado: novo caso real com cooldown distinto ⇒ reabrir.

### Runtime dispatch (watcher/daemon resolve subpath em tempo de execução)

Watcher's `owning_folder()` ou daemon's `_process_folder` consultariam o override e bifurcariam logic. **Recusada** — aumenta coupling de runtime (toca código rodando, expande superfície de regressão), enquanto a expansão config-time mantém o problema no loader (validado por testes unitários determinísticos).

### Auto-fallback após N falhas consecutivas

Daemon detecta N abortos de `bisync` num mesmo subpath e auto-migra pra `bundle`. **Recusada** — exige threshold + storage de estado por subárvore + classificador de erro. Config explícito é mais conservador e alinhado com a doutrina do projeto ("controle do operador, não inferência").

## Gatilhos de revisão

- **Novo caso real de override exigindo campo além de `git_mode`** (ex.: cooldown_seconds distinto): estender `SubpathOverride` com o campo necessário e ADR-006 ganha emenda registrando o novo campo + critério de inclusão. Sinal objetivo: ≥1 operador reportando que mantém workaround folder-top-level por causa de cooldown distinto, ou ≥2 issues abertos pedindo override de campo diferente.
- **Nested overrides (override de override) virando caso real**: hoje rejeitado. Reabrir se houver caso onde um subpath problemático tem ele mesmo sub-subpath com modo distinto. Sinal: pelo menos 1 operador descrevendo o cenário.
- **Volume de overrides por folder crescer**: hoje suportamos N. Se `>5` overrides numa entry virarem comum, re-avaliar legibilidade do YAML e considerar arquivo de override separado por folder. Sinal: `grep -c "subpath:" config/config.yaml.example` na entry de algum folder > 5.

## Referências

- Plano de execução: [docs/plans/git-mode-subpath-override.md](../plans/git-mode-subpath-override.md)
- ADR de pattern base: [ADR-004](ADR-004-cooldown-gate-periodic-full-sync.md) — campo opt-in em `FolderConfig` que altera comportamento.
- CLAUDE.md → "git_mode Semantics" — descrição dos 3 modos (`off`/`bisync`/`bundle`).
