# ADR-008: drive-sync abandona bisync para repos git; bundle vira modo de backup local-only

**Data:** 2026-06-01
**Status:** Proposto

## Origem

- **Investigação:** dois incidentes na fronteira drive-sync↔git em <72h fecharam a tese de que `rclone bisync` não tem semântica de operação git:
  - **2026-06-01** (NOTES.md `2026-06-01T14:29:31Z`): 6 ADRs archived das Ondas I+J do `pragmatic-dev-toolkit` ressuscitaram untracked em `docs/decisions/` porque o remote Proton ainda tinha as versões pré-archive. Daemon parado desde então.
  - **2026-05-31** (BACKLOG.md linha 27): `dev-projects` pós FS migration onda-1 abortou bisync por "too many deletes" (104k→36k). `--resync` esperando "local wins" na verdade copiou ~32k arquivos pré-migration do remote para o local.
- **Decisão base:** [ADR-006](ADR-006-git-mode-subpath-override.md) — assumia `git_mode: bisync` como modo principal e adicionava granularidade (`subpath_overrides`) só para subpaths cronicamente problemáticos. Esta ADR inverte a base: `bisync` sai do menu para repos git; `auto-detect → skip|bundle` vira regra.

## Contexto

`rclone bisync` reconcilia dois lados por presença/ausência de arquivo. Não fala git: não consulta `git log`, não tem deletion marker para arquivos archived via `git mv`, não distingue "deletado intencionalmente" de "ainda não sincronizado". Para repos sob bisync, qualquer operação git que move/remove arquivo (`git rm`, `git mv`, merge que apaga, archive batch) produz "deleção local" que o bisync interpreta como "remote ainda tem → restaurar".

Incidentes recentes são instâncias do mesmo padrão estrutural — não falhas de config de exclude, não regressão de versão do rclone, não bug do backend protondrive. A correção também não é mecânica de mais um exclude: é remover repos git do escopo do bisync.

Estado real do host coletado em `/triage` 2026-06-01 (relevante para a decisão):

- 17 repos git sob `/storage/dev/projects/` (todos com remote no GitHub).
- 1 repo git ao nível do folder em `/storage/dev/scripts/` (local-only, sem remote — caso real de "backup só se via drive-sync").
- 1 repo em `/storage/notes/logseq` (`.git` como arquivo via gitdir redirect, com remote).
- Folders não-git (`Documents`, `Pictures/*`, `Videos`, `library`) continuam beneficiando-se de bisync — escopo desta ADR é restrito a repos git.

Pré-requisitos cumpridos:

- Infra de scan recursivo: `git_handler.find_git_repos` + `recursive_detection: true` + `max_recursion_depth: 6` (default 6). Sem código novo de descoberta.
- Bundle mode estável e exercitado: `git_handler.create_bundle` + dispatch via `_sync_git_folder`.
- Lock global (ADR-001) protege contra concorrência durante o flip.

## Decisão

**`FolderConfig.git_handling: str = "auto"`** substitui `git_mode` no escopo "como tratar repos git dentro do folder". Valores: `auto|skip|bundle|plain` — `plain` significa "bisync do worktree sem excludes git" (semântica que `git_mode: off` carregava no schema antigo, com nome explícito ao invés de ambíguo). `git_mode` em qualquer valor (`bisync|bundle|off`) deixa de ser opção válida; loader rejeita com erro descritivo apontando playbook.

**Detecção em modo `auto`:** loader varre o folder via `find_git_repos`; para cada repo descoberto executa `git -C <repo> remote -v`. Vazio → mode `bundle` (repo é candidato a backup); com ≥1 remote → mode `skip` (GitHub/equivalente é o backup; drive-sync não toca). Override caso-a-caso via `repo_overrides: list[RepoOverride]` análogo a `subpath_overrides` (ADR-006), com campos `repo_subpath: str` e `mode: Literal["skip", "bundle"]`.

**Dispatch no runtime:**

- Paths classificados como `skip` viram excludes adicionais injetados pelo loader no rclone bisync do parent folder (`--exclude /<repo_rel>/**`).
- Paths classificados como `bundle` reusam o caminho existente de `_sync_git_folder` em bundle mode.
- Folder com `git_handling: skip` global pula `_process_folder` inteiramente (log `[FOLDER_SKIP]`, sinaliza `_last_successful_sync_at` para não disparar staleness — ADR-005).

**Migração:** `git_mode` em qualquer valor (`bisync|bundle|off`) rejeitado com erro descritivo apontando playbook (`docs/operations/playbook-flip-git-handling.md`). Sem coerce silencioso — falha-fast simétrica em todos os valores legados; operador edita config uma vez para o schema novo (incluindo os ~11 folders não-git que hoje declaram `git_mode: off` e passam a usar `git_handling: plain`). Playbook documenta ordem específica para folders contendo repo local-only: config-first (declarar `git_handling: bundle` ou `auto` no folder local-only, restart, esperar primeiro bundle subir com sucesso no Proton) e só **depois** `rclone purge` nos paths bisync-only. Inverter a ordem deixaria o repo local-only sem nenhum backup cloud durante a janela entre purge e primeiro bundle.

**Coexistência com `subpath_overrides` (ADR-006):** `repo_overrides` e `subpath_overrides` coexistem com precedência documentada — para cada path classificado, `repo_overrides` ganha quando o path é um repo git descoberto (match exato via `repo_subpath` relativo ao parent folder); `subpath_overrides` aplica em qualquer subpath não coberto por `repo_overrides`, e em campos não-`mode` que `repo_overrides` não exponha. Loader valida sobreposição como hoje (rejeita `(a, a/b)` em pares dentro da mesma lista — extensão da regra de ADR-006). Preserva a extensibilidade que ADR-006 §Razões abriu deliberadamente: operador que precisa override de campo não-`mode` (ex.: `cooldown_seconds` por subpath) continua via `subpath_overrides` sem trade-off contra `git_handling: auto`.

Razões:

- **Causa-raiz estrutural, não tunável:** bisync não tem como ganhar semântica de git sem reescrita upstream. Excludes, presets, `--max-delete` thresholds tratam sintomas — incidentes vão recorrer enquanto repos git ficarem sob bisync.
- **GitHub já é backup pra 17/18 repos:** custo zero adicional pra abandonar bisync neles. Drive-sync deixa de duplicar responsabilidade.
- **Bundle resolve repos local-only:** caso `dev-scripts` (e futuros) ganha backup via artefato único, sem conflito possível (1 arquivo, 1 mtime).
- **Auto-detect minimiza superfície de config:** operador não cataloga 17 repos. `git remote -v` é determinístico e barato.
- **Híbrido > puros:** auto puro flipa silenciosamente quando operador adiciona/remove remote (lixo órfão no Proton); explícito puro força catalogação verbose. `repo_overrides` cobre exceções sem perder a ergonomia.
- **Falha-fast simétrica em config legado:** `git_mode` em qualquer valor (`bisync|bundle|off`) rejeitado no loader. Sem coerce silencioso assimétrico que mascararia a mudança semântica de `off` (antiga "bisync sem excludes") para `plain` (mesma semântica, nome explícito). Config local é gitignored (`config/config.yaml` é a única instância), edição manual é trivial.
- **Reuso de infra:** `find_git_repos`, `create_bundle`, `_sync_git_folder` bundle mode, lock global ADR-001 — superfície de regressão fica em `config.py` + classifier novo em `git_handler.py` + injeção de excludes em `sync_engine.bisync_folder`.

## Consequências

### Benefícios

- Classe de incidente "bisync ressuscita arquivo arquivado/movido/deletado via git" desaparece (causa-raiz removida, não mitigada).
- Lock global rclone (ADR-001) alivia naturalmente: 4 folders deixam de bisync (`dev-projects`, `dev-scripts`, `dotfiles`, `logseq-vault`) — concorrência cai sem mudança de código.
- Storage no Proton encolhe: 17 repos git deixam de duplicar histórico já presente no GitHub.
- `--force-push` CLI (BACKLOG.md linha 27) torna-se obsoleto antes de implementar — não há mais bisync sobre git para corrigir post-FS-migration.
- Item `docs: playbook para FS migrations` (BACKLOG.md linha 31) absorvido como subset do `playbook-flip-git-handling.md`.
- Repos local-only ganham backup formal (caso `dev-scripts` hoje só sobreviveria com bisync funcionando — risco silencioso pré-mudança).

### Trade-offs

- **Perde "cópia cloud de uncommitted changes"** documentada em CLAUDE.md → "git_mode Semantics" como razão principal do default `bisync`. O custo desse trade-off é incerto: o operador atual não reporta workflow dependente, mas a documentação histórica trata a feature como valiosa — não é seguro afirmar "pouco exercitada empiricamente". Mitigação se a dor emergir: commit + push GitHub explícito para WIP cross-device; ou `repo_overrides: [{repo_subpath: X, mode: bundle}]` em repos onde a perda dói (bundle preserva HEAD + branches mas não index/worktree). Gatilho de revisão registrado.
- **Perde "repo navegável no Proton web"**: bundle é binário, GitHub é a interface navegável. Aceitável: operador usa GitHub.
- **Bundle re-uploadar em mudança**: rclone não faz delta upload de blob no backend protondrive. Cooldown (ADR-004) já mitiga; recomendação no playbook: declarar `cooldown_seconds: 10800` (3h) em folders predominantemente bundle.
- **Flip silencioso quando operador adiciona remote a repo local-only**: ciclo seguinte vê repo com remote → mode `skip`; bundle anterior fica órfão no Proton (não removido automaticamente). Mitigação: log explícito `[REPO_MODE_FLIP] <repo>: now has remote, switching to skip — bundle órfão em <path>`. Operador limpa manual. Caso reverso (`git remote remove origin`) idem com `flip to bundle`. Não tem prevenção arquitetural — config explícito via `repo_overrides` permanece como escape hatch se operador antecipa instabilidade do remote.
- **`subpath_overrides` (ADR-006) ganha invariante novo de precedência**: `repo_overrides` ganha em paths classificados como repo descoberto; `subpath_overrides` aplica fora dessa interseção (subpaths não-repo, ou campos não-`mode`). Caso original `tjpa/pje-2.1` (ADR-006 § Origem) é coberto naturalmente pelo auto-detect (repo tem remote no GitHub → `skip`); override de ADR-006 fica obsoleto pra esse caso específico. Casos exclusivos de ADR-006 (override de campo em subpath não-repo, ou extensões futuras de `SubpathOverride` como `cooldown_seconds`) ficam intactos com `git_handling: auto` graças à precedência documentada.

### Limitações

- **Auto-detect via `git remote -v` é proxy para "backup externo existe"** — proxy razoável mas falsificável. Remote configurado mas não-funcional (nunca pushed, fork deletado, mirror read-only, URL de arquivo desmontado) vira `skip` silencioso sem refutar o pressuposto "este repo está backupado externamente". Mitigação: log enriquecido `[REPO_SKIP] <repo> (has_remote: <url>)` permite operador grepar journal e detectar remotes suspeitos; override via `repo_overrides: [{repo_subpath, mode: bundle}]` força bundle quando operador sabe que o remote é fictício/inacessível. Não há heurística arquitetural barata para "remote funcional" sem chamada de rede (`git ls-remote` custa I/O em todo scan); fica como gatilho de revisão se virar dor empírica.
- **Sem auto-cleanup do remote pós-flip**: daemon não roda `rclone purge` por conta própria nos paths que saem do sync. Playbook manual. Razão: ação destrutiva no remote sem confirmação operador é fora da doutrina ("controle do operador, não inferência").
- **Sem auto-fallback de bundle para skip (ou vice-versa) após erro**: classificação é por `git remote -v` na hora do scan; falhou? Erro propagado, sem retry com modo diferente. Alinhado com ADR-006 § "Sem auto-fallback após N falhas".
- **Repos sob worktree git (`.git` é arquivo)**: `git -C <dir> remote -v` delega ao superproject — funciona, mas worktree de repo com remote vira `skip` (que pode não ser intenção do operador se a worktree é experimental). Override via `repo_overrides` resolve. Documentar no CLAUDE.md.
- **Submodules**: mesma forma de `.git` arquivo. Tratamento idêntico ao worktree.
- **Repo `git init` sem commits**: `create_bundle` no-op silencioso quando não há HEAD. Sem erro, sem upload. Aceitável.

### Mitigações

- **Observabilidade do flip**: log `[REPO_SKIP] <repo> (has_remote: <url>)`, `[REPO_BUNDLE] <repo> (no_remote|override)`, `[REPO_MODE_FLIP] <repo>: <old>→<new>` em todo ciclo classify. URL no log permite operador detectar remote-configurado-mas-não-funcional via grep. **Flips de mode (`REPO_MODE_FLIP`) disparam `notify-send`** (canal igual a staleness ADR-005, reuso de `Notifier`) — operador percebe na hora, não meses depois quando lixo cumulativo aparece no Proton. `journalctl --user -u drive-sync --grep "REPO_"` complementa para histórico/inspeção retrospectiva.
- **`drive-sync --status` mostra classificação corrente** por folder: lista de repos descobertos + mode atual. Sem isso, operador precisa grep journal.
- **`drive-sync --check` falha-fast** em `git_mode: bisync` apontando playbook + ADR-008. Reduz tempo até operador entender migração.

## Alternativas consideradas

### Manter bisync com excludes mais agressivos

Adicionar `.git/`, `**/docs/decisions/archive/**`, padrões similares ao `auto_exclude`. **Recusada**: trata sintomas. Próximo incidente vem por padrão de move/archive não previsto. ADR-006 já registrou que config explícito > inferência — manter bisync é manter superfície que exige exclude mecânico contra ações git futuras.

### Bundle para todo repo git (sem skip)

Trata todos repos como local-only do ponto de vista do drive-sync. **Recusada**: 17 dos 18 repos têm remote no GitHub; bundle deles duplicaria histórico já backupado. Custo: storage Proton e CPU re-bundle por mudança. Para esses, `skip` é estritamente superior.

### Auto-detect puro sem `repo_overrides`

Sem override explícito; detecção sempre por `git remote -v`. **Recusada**: flip silencioso quando operador adiciona/remove remote é mais frequente que parece (forks experimentais, repos privados que mudam para públicos, etc.). `repo_overrides` é escape hatch raro mas necessário; custo de adicionar é ~1 dataclass + parser.

### Explícito puro (catalogar cada repo no config)

Operador declara mode por repo. **Recusada**: 17+ repos sob `dev-projects` exigiriam catalogação verbose. `git remote -v` é determinístico e barato — auto-detect é a forma natural; override cobre exceções.

### Runtime classification em vez de config-time

Classifier chamado a cada `_process_folder` em vez de no `load_config`. **Considerada**: detecta adições/remoções de remote sem restart. Recusada-por-ora: complexidade extra (cache, invalidação, observability) sem caso real. ADR-006 escolheu config-time pelo mesmo motivo (`expansão acontece em load_config`). Reabrir se flips silenciosos virarem dor frequente.

### Coerce silencioso de `git_mode: bisync` → `git_handling: auto`

Loader aceita config antiga e migra in-memory. **Recusada**: mascara semântica nova; operador não percebe que precisa rodar `rclone purge` do remote. Falha-fast com mensagem apontando playbook é mais alinhada com a doutrina ("explicit over magical").

## Gatilhos de revisão

- **Operador reporta perda de workflow cross-device por ausência de WIP-cloud**: ≥1 reporte concreto de "abri laptop B e perdi WIP que estaria no Proton sob bisync" → considerar `git_handling: bundle_with_wip` (bundle inclui stash temporário de uncommitted changes via `git stash create`) ou documentar `repo_overrides: [{repo_subpath: X, mode: bundle}]` como caminho-recomendado em CLAUDE.md para repos onde WIP-cross-device importa.
- **Remote configurado mas não-funcional virar dor empírica**: ≥1 incidente real de repo perdido por fork deletado / mirror read-only / push nunca feito → considerar `git ls-remote` no classifier ou heurística `git rev-list <remote>/HEAD..HEAD` para detectar branches não-pushed. Custo de I/O extra justificado só com sinal real.
- **Flip silencioso virar dor frequente apesar de `notify-send`**: ≥2 incidentes de bundle órfão no Proton mesmo com flips notificados — reabrir e considerar `drive-sync --check-orphans` (read-only) que cruza `rclone lsf <remote_root>/<folder>` com `classify_repos(folder)` e lista diff.
- **`rclone bisync` ganhar semântica git** (improvável): upstream adiciona deletion markers ou modo `--git-aware`. Reabrir como decisão de "voltar para bisync em repos com remote opcional para uncommitted-WIP-backup".
- **Caso real de override exigindo campo além de `mode`** em `repo_overrides` (ex.: cooldown distinto por repo): estender `RepoOverride` com o campo; ADR-008 ganha emenda. Sinal: ≥1 operador reportando workaround para forçar comportamento distinto via dois folders separados.
- **Backend não-protondrive** (S3, Google Drive nativo): se backend ganhar delta upload de blob, `bundle` deixa de pagar custo de re-upload integral; `cooldown_seconds` virar opcional em folders bundle. Sinal: rclone release notes documentando suporte.
- **Volume de `repo_overrides` por folder crescer**: hoje suporta N. Se `>5` overrides numa entry virarem comum, re-avaliar legibilidade (mesmo gatilho registrado em ADR-006 § "Volume de overrides por folder crescer"). Sinal: `grep -c "repo_subpath:" config/config.yaml.example` > 5 numa entry.

## Referências

- Plano de execução: [`.claude/local/plans/git-handling-auto-detect.md`](../../.claude/local/plans/git-handling-auto-detect.md) (modo local — não versionado).
- ADR de pattern base: [ADR-006](ADR-006-git-mode-subpath-override.md) — campo opt-in em `FolderConfig`; estendido aqui com mutual-exclusion vs. `git_handling: auto`.
- ADR de cooldown: [ADR-004](ADR-004-cooldown-gate-periodic-full-sync.md) — pré-requisito pra bundle em repos com `.git/` grande.
- ADR de lock global: [ADR-001](ADR-001-serializar-chamadas-rclone.md) — alivia naturalmente com bisync removido de git.
- ADR de staleness: [ADR-005](ADR-005-folder-staleness-degraded.md) — invariante de "folder com `git_handling: skip` não dispara staleness" preserva semântica.
- CLAUDE.md → "git_mode Semantics" — atualizada pelo plano de execução para "git_handling Semantics".
- Incidente trigger: `.claude/local/NOTES.md` entrada `2026-06-01T14:29:31Z`.
- Incidente correlato: `BACKLOG.md` ## Próximos linha 27 (`--force-push` CLI) — virou obsoleto antes de implementar.
