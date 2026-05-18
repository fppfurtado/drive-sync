# Plano — Override de `git_mode` por subpath dentro de folder

## Contexto

Hoje `git_mode` é por folder inteiro. Repos com `.git/` cronicamente problemático no rclone+protondrive forçam o operador a extrair o subdir em folder top-level próprio só pra trocar de modo — caso real registrado: em 2026-05-13 e 14, `tjpa/pje-2.1` acumulou 3 tentativas de `bisync --resync` falhando ~19h totais com `Code=2003`/`Code=2500`/`Code=2501`/`gopenpgp`, enquanto outros subrepos do mesmo folder funcionaram de primeira. Workaround atual: entry top-level `tjpa-pje-2.1` no config.yaml com `git_mode: bundle` + exclude `tjpa/pje-2.1/**` em `dev-projects`. Funciona mas é poluição visual e perde a relação "override of parent".

Pré-requisito cumprido: rate-limit por folder ([ADR-004](../decisions/ADR-004-cooldown-gate-periodic-full-sync.md), 2026-05-14) — sem ele, bundle re-uploadar a cada evento do watchdog seria proibitivo em repo `.git/` na casa dos GB.

**ADRs candidatos:** ADR-006 (será criado — registra schema + escolha YAGNI de "só `git_mode` overrideable"), ADR-004 (alinhamento de pattern: campo opt-in em `FolderConfig` que altera comportamento sem mudar contratos de runtime).

**Linha do backlog:** config: permitir override de `git_mode` por subpath dentro de um folder — hoje `git_mode` é por folder inteiro (ex.: dev-projects=bisync se aplica a todo `/storage/3. Resources/Projects/`). Repos com `.git/` cronicamente problemático (caso real em 2026-05-13 a 14: `tjpa/pje-2.1` acumulou 3 tentativas de bisync `--resync` falhando ~19h totais com `Code=2003`/`Code=2500`/`Code=2501`/`gopenpgp` no rclone+protondrive, enquanto outros subrepos do mesmo folder e a library [git_mode=off] funcionaram de primeira) deveriam poder cair em `git_mode: bundle` ([git_handler.py](../../drive_sync/git_handler.py), CLAUDE.md → "git_mode Semantics") sem o operador ter que extrair o subdir em folder top-level separado.

## Resumo da mudança

**Modelo escolhido — "syntactic sugar" no load_config**: `subpath_overrides` é expandido em tempo de leitura do YAML em N `FolderConfig` synthetic, cada uma herdando do parent **exceto** `git_mode` (e ajustando `local_path`/`remote_subpath`/`name`/`fs_key`). O parent ganha automaticamente o glob `<subpath>/**` no `exclude` (com log WARNING se o operador declarou redundante). Runtime (watcher/daemon/sync_engine) **não sabe da existência de overrides** — vê apenas a lista plana de folders e roteia normalmente via `owning_folder()` (que já elege o ancestral mais específico).

Schema novo em `FolderConfig`: campo `fs_key: str` separado de `name`. `name` preserva hierarquia (`dev-projects/tjpa-pje-2.1`) para logs/queue/journal; `fs_key` é o slug filesystem-safe (`dev-projects-tjpa-pje-2.1`) usado pelos callers de path em disco. Folders declarados direto no YAML default `fs_key = name` (não-disruptivo); synthetic é o único caso onde divergem.

**Entra:**
- `SubpathOverride` dataclass (`subpath: str`, `git_mode: str`) em `config.py`.
- Campo opt-in `subpath_overrides: list[SubpathOverride] = []` em `FolderConfig`.
- Expansão em `load_config`: para cada `(parent, override)`, criar synthetic `FolderConfig` herdando demais campos do parent; setar `local_path = parent.local_path / override.subpath`, `remote_subpath = f"{parent.remote_subpath}/{override.subpath}"`, `name = f"{parent.name}/<sanitized-subpath>"` (ex.: `dev-projects/tjpa-pje-2.1`).
- Mutação automática: `parent.exclude` ganha `f"{override.subpath}/**"` se ainda não presente (idempotente — operador pode declarar redundante sem erro).
- Validações no loader: `subpath` não-vazio e relativo (não começa com `/`, sem `..`); `git_mode` em `{off,bisync,bundle}`; subpaths únicos dentro do mesmo parent; nome synthetic não colide com folder existente.
- Documentação no `config/config.yaml.example` mostrando o caso `dev-projects` → `tjpa/pje-2.1`.

**Fica de fora:**
- Override de outros campos (`auto_exclude`, `exclude`, `cooldown_seconds`, `debounce_seconds`). YAGNI consciente — ADR-006 declara a restrição e os gatilhos de revisão. Caso operador precise de cooldown diferente no override, mantém o workaround atual (folder top-level próprio).
- Auto-fallback após N falhas (decisão registrada no BACKLOG/ADR-006: config explícito > inferência).
- Nested overrides (subpath override de subpath override). Fora de escopo; loader valida que subpaths não se aninhem entre si dentro do mesmo parent.

**Migração esperada pós-merge** (operador, não dentro do plano): no `~/.config/drive-sync/config.yaml`, remover entry top-level `tjpa-pje-2.1` e o exclude manual `tjpa/pje-2.1/**` de `dev-projects`; adicionar `subpath_overrides: [{subpath: "tjpa/pje-2.1", git_mode: "bundle"}]` em `dev-projects`. Cooldown de 10800s do `dev-projects` é herdado automaticamente.

## Arquivos a alterar

### Bloco 1 — config: schema + expansão de synthetic folders {reviewer: code}

- `drive_sync/config.py`:
  - Nova dataclass `SubpathOverride(subpath: str, git_mode: str)`.
  - `FolderConfig` ganha dois campos:
    - `subpath_overrides: list[SubpathOverride] = field(default_factory=list)` — só populado em parents lidos do YAML; synthetic tem `[]` (não-recursivo).
    - `fs_key: str` — slug filesystem-safe, default `fs_key == name` quando o operador não declara. Loader popula automaticamente para synthetic (substitui `/` por `-`).
  - `load_config`: dentro do loop `for entry in folders_raw`, após construir o parent `FolderConfig`, expandir overrides:
    - Validar cada override: `subpath` não-vazio, não-absoluto (não começa com `/`), sem `..`, git_mode em `{off,bisync,bundle}`.
    - Validar unicidade de subpath dentro do parent (set).
    - Para cada override, criar synthetic `FolderConfig`:
      - `name = f"{parent.name}/{override.subpath}"` (preserva hierarquia)
      - `fs_key = name.replace("/", "-")` (filesystem-safe)
      - `local_path = parent.local_path / override.subpath`
      - `remote_subpath = f"{parent.remote_subpath}/{override.subpath}".strip("/")`
      - `git_mode = override.git_mode`
      - `subpath_overrides = []` (não-recursivo)
      - Demais campos copiados do parent (`enabled`, `auto_exclude`, `exclude`, `debounce_seconds`, `cooldown_seconds`).
    - Mutação do `parent.exclude`: se `f"{override.subpath}/**"` ausente, adicionar; se presente, emitir `log.warning("[%s] exclude redundante de %r — injetado automaticamente por subpath_overrides (ADR-006)", parent.name, glob)`. Não falha, não duplica.
  - Adicionar tanto parent quanto synthetic no `folders` final. Validação global de unicidade de `name` cobre colisão com folder declarado explicitamente.
- **Callers de filesystem path** que hoje usam `folder.name` precisam migrar pra `folder.fs_key`:
  - `drive_sync/daemon.py`: `bundles_dir / folder.name` (no fluxo de bundle) → `bundles_dir / folder.fs_key`.
  - `drive_sync/git_handler.py`: `bundle_path_for` (e callers próximos) — auditar e trocar para `fs_key`.
  - Identidade/log (queue.put, `_inflight`, `_last_sync_at`, `_degraded_folders`, log.info `[%s]`) **continua usando `name`** — folder synthetic aparece como `[dev-projects/tjpa-pje-2.1]` em logs e em `[FOLDER_DEGRADED]` (ADR-005).

- `config/config.yaml.example`: novo bloco comentado sob `folders:`. Exemplo:
  ```yaml
  - name: dev-projects
    local_path: /storage/3. Resources/Projects/
    remote_subpath: dev/projects
    git_mode: bisync
    auto_exclude: true
    cooldown_seconds: 10800
    subpath_overrides:
      # tjpa/pje-2.1 tem .git/ cronicamente problemático no protondrive (Code=2003/
      # 2500/2501/gopenpgp). git_mode: bundle evita re-upload do worktree inteiro.
      # ADR-006: só git_mode é overrideable; demais campos herdam do parent.
      - subpath: tjpa/pje-2.1
        git_mode: bundle
  ```

### Bloco 2 — testes {reviewer: qa}

- `tests/test_config.py` (estender):
  - Override expande em 1 synthetic folder com `git_mode` correto e demais campos herdados (cooldown, auto_exclude, exclude do parent, enabled).
  - Synthetic `name = "parent/subpath"` (preserva `/`), `fs_key = "parent-subpath"` (filesystem-safe).
  - Folder declarado direto (sem override) tem `fs_key == name`.
  - Parent ganha glob `<subpath>/**` no exclude após expansão (se ausente).
  - Coexistência graceful: operador declara `exclude: <subpath>/**` E `subpath_overrides`. Loader NÃO duplica E emite WARNING ("exclude redundante de ... — injetado automaticamente por subpath_overrides"); assert via `caplog`.
  - Synthetic `local_path` e `remote_subpath` corretos (path concat).
  - 2 overrides no mesmo parent expandem em 2 synthetic + 2 entries no parent.exclude.
  - Validação: subpath vazio → ValueError com mensagem específica.
  - Validação: subpath absoluto (começa com `/`) → ValueError.
  - Validação: subpath com `..` → ValueError.
  - Validação: git_mode inválido no override → ValueError.
  - Validação: 2 overrides com mesmo subpath no mesmo parent → ValueError.
  - Validação: synthetic name colide com folder declarado explicitamente no YAML → ValueError (cobertura via `seen_names`).
  - Folder synthetic `subpath_overrides` é sempre `[]` (não-recursivo).

### Bloco 3 — docs {reviewer: doc}

- `README.md`: bullet `folders[]` em "Configuração" ganha menção a `subpath_overrides` com 1 frase de propósito + link conceitual a ADR-006.
- `.claude/CLAUDE.md` → **`## git_mode Semantics`** (alvo canônico — tabela já existe lá): adicionar 1 frase logo após a tabela: "Cada folder pode adicionalmente declarar `subpath_overrides: [{subpath, git_mode}]` para subpastas específicas — expansão acontece em `load_config` e o runtime vê apenas a lista plana — [ADR-006](../docs/decisions/ADR-006-git-mode-subpath-override.md)." Tabela em si não muda (3 modos seguem válidos).

## Verificação end-to-end

- `python -m pytest tests/ -v` passa (incluindo novos testes em `test_config.py`).
- `python -m drive_sync --check` aceita config com `subpath_overrides` e rejeita configs malformadas com mensagens específicas das validações.
- `grep -rn "subpath_overrides\|SubpathOverride" drive_sync/ config/ docs/ tests/ README.md` cobre os 6 paths esperados (config.py, config.yaml.example, ADR-006, plano, testes, README.md).
- `python -c "from drive_sync.config import load_config; cfg = load_config(...); print([f.name for f in cfg.folders])"` em config com 1 override mostra `[parent, parent/sub]`.

## Verificação manual

Operador, após merge, em config local:

1. `systemctl --user stop drive-sync`.
2. Em `~/.config/drive-sync/config.yaml`:
   - Remover entry top-level `tjpa-pje-2.1` (que existe hoje como workaround).
   - Em `dev-projects`, remover linha de `exclude: tjpa/pje-2.1/**` (será re-injetado automaticamente).
   - Em `dev-projects`, adicionar:
     ```yaml
     subpath_overrides:
       - subpath: tjpa/pje-2.1
         git_mode: bundle
     ```
3. `drive-sync --check` aceita o config.
4. `drive-sync --status` lista a mesma contagem de antes do workaround: o `tjpa-pje-2.1` standalone vira o synthetic `dev-projects/tjpa-pje-2.1` (mesmo número de entries no output).
5. `systemctl --user start drive-sync`.
6. Próximo ciclo de safety-net periódico (`journalctl --user -u drive-sync`): linhas devem aparecer separadas:
   - `[dev-projects] Iniciando job (modo=bisync)` — sem tocar `tjpa/pje-2.1/**` (exclude injected).
   - `[dev-projects/tjpa-pje-2.1] Iniciando job (modo=bundle)` — só bundle.
7. Confirmar que `dev-projects` bisync completa em tempo similar ao histórico (sem regressão de exclude).
8. Confirmar que `dev-projects/tjpa-pje-2.1` faz upload do bundle (não bisync — não deve aparecer rclone bisync no journal pra esse path).

## Notas operacionais

- **Ordem dos blocos**: 1 → 2 → 3. Bloco 2 depende de 1. Bloco 3 pode rodar em paralelo após 1 fechar (precisa apenas do número final do ADR-006 que `/new-adr` atribui).
- **Migração não está no plano** (nem em testes nem em arquivos a alterar) — é ação operacional pós-merge no `~/.config/drive-sync/config.yaml`, que é gitignored. O config de exemplo em `config/config.yaml.example` JÁ documenta o caminho novo, então é o documento canônico de "como configurar".
- **Atenção do reviewer do Bloco 1**: confirmar que a expansão é idempotente — operador declarando `exclude: tjpa/pje-2.1/**` E `subpath_overrides: [{subpath: tjpa/pje-2.1, ...}]` simultaneamente não deve duplicar nem falhar; loader emite WARNING. Testes do Bloco 2 cobrem (assert via `caplog`).
- **Atenção do reviewer do Bloco 1**: separação `name` vs `fs_key` decidida no /triage (ADR-006). `name` preserva `/` (`dev-projects/tjpa-pje-2.1`) em logs/queue/journal; `fs_key` é o slug filesystem-safe (`dev-projects-tjpa-pje-2.1`) usado por callers de path em disco. Revisar que **todos** os callers de filesystem migraram para `fs_key` (audit suggested: `bundles_dir`, `gitbundle` paths, marker files se existirem) e que callers de identidade (queue.put, log `[%s]`, `_inflight`) **continuam** com `name`. Folders top-level declarados direto têm `fs_key == name` (não-disruptivo para configs existentes).
- **Asimetria entre `drive-sync-folder list` e `--status`**: `drive-sync-folder list` (`scripts/folder-sync-folder`) lê o YAML cru, sem expandir overrides — operador verá a entry `dev-projects` listada **sem** o synthetic. `drive-sync --status` (que itera `cfg.folders` pós-load) mostrará o synthetic separadamente. Comportamento aceitável: a fonte da verdade do operador pra configurar é o YAML (interface de edição), e `--status` é interface de inspeção de runtime. Documentar em 1 frase no README.md se for o caminho do reviewer do Bloco 3.
