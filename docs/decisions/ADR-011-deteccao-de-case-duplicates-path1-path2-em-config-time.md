# ADR-011: Detecção de case-duplicates Path1↔Path2 em config-time

**Data:** 2026-06-08
**Status:** Proposto

## Origem

- **Investigação:** incidente library 2026-06-08T12:39:18 — `[library] bisync falhou (rc=7): they exist?` em folder `library` (Path1=`/storage/areas/`, Path2=`proton:Sync/library`, mode `plain`). Service permanece `active`, só library abortado. Peek empírico em `~/.cache/rclone/bisync/storage_areas..proton_Sync_library.path[12].lst-err` identificou 5 pares case-duplicates colidindo em `/storage/areas/` (`family↔Family`, `finances↔Finances`, `hobbies↔Hobbies`, `learning↔Learning`, `work↔Work`) resíduo de Onda 1 FS migration do meta-system; entry `Finances (Copy)` existe mas não colide com nenhum sibling (par-degenerado, descartado pelo validator). Contexto completo em `.claude/local/NOTES.md § 2026-06-08T16:42:26Z`.
- **Decisão base:** [ADR-010](ADR-010-validacao-config-time-auto-exclude-markers-codigo.md) — precedente direto do padrão "config-time `--check` rejeita declaração que diverge do estado do FS, com fatal-error e paths concretos". ADR-010 cobre build-artifacts (Python/JS/Rust); este ADR cobre uma classe distinta: filesystem-semantic mismatch (case-sensitivity Path1↔Path2).

## Contexto

Proton Drive é case-insensitive: `family/` e `Family/` no FS local (Linux, case-sensitive) mapeiam para a mesma entry no remote. rclone bisync detecta a colisão como "duplicate-name resolution required" e emite `Bisync critical error: ... rc=7` com mensagem genérica que o capture de stderr atual ([`sync_engine.py:133`](../../drive_sync/sync_engine.py), tail-truncate `[-500:]`) reduz a fragmentos cripticos (caso real: "they exist?"). A causa-raiz só fica visível por peek empírico no `.lst-err` do diretório de state do rclone — operador sem path concreto para resolver a partir do que vê no journal.

A degradação observável atual depende do `_check_folder_staleness` (ADR-005) marcar a pasta como `STATUS=degraded folders: <list>` após `watcher.folder_staleness_threshold_seconds` (default 12h). Operador descobre o problema horas depois, sem conexão direta com a causa-raiz, e precisa fazer forense manual em `~/.cache/rclone/bisync/` para identificar os pares.

O cenário não é hipotético: 6 pares foram introduzidos em `/storage/areas/` por uma única operação no meta-system (Onda 1 FS migration, ADR-002 PARA→functional). Qualquer FS migration que toque folders monitorados pode reintroduzir o padrão.

Cleanup case-duplicates é **responsabilidade do operador** (FS surgery cirúrgica: rename, merge, ou delete por par; decisões dependem do conteúdo de cada lado, fora do escopo do drive-sync). Drive-sync precisa **detectar e abortar com mensagem clara** surfando os pares concretos, alinhado com o princípio "controle do operador, não inferência" estabelecido em ADR-010.

## Decisão

Adicionar validador config-time `_validate_case_duplicates_against_remote(folder, max_depth)` em [`drive_sync/config.py`](../../drive_sync/config.py) análogo a `_validate_auto_exclude_against_code` (ADR-010). `drive-sync --check` rejeita com fatal-error quando o scan recursivo de `folder.local_path` (depth = `cfg.git.max_recursion_depth`, default 6) detecta siblings com colisão case-insensitive (`name.lower()` repetido entre **dirs e arquivos** sob o mesmo `dirpath` — Proton Drive trata os dois como mesma entry; qualquer sibling colidindo dispara rc=7, independente de ser pasta ou arquivo).

Razões:

- **Defesa em profundidade contra erro semântico do remote** — Proton case-insensitive é restrição externa estável (não há feature flag rclone ou config drive-sync que mude isso). Validação config-time detecta a divergência antes do daemon começar a syncar, antecipando a dor que hoje só aparece via degraded 12h depois.
- **Falha-fast no `--check`** — operador roda `--check` antes de start; gate antecipa a dor. Paridade com ADR-010 §Decisão.
- **Mensagem com pares concretos** — operador recebe `/storage/areas/family ↔ /storage/areas/Family` como bullet, não `rc=7: they exist?` como fragmento. Elimina o forense manual em `.lst-err`.
- **Sem escape hatch de policy** — diferente de ADR-010 (`auto_exclude: false` + `exclude:` manual é caminho legítimo), case-insensitive remote é fato semântico do Proton Drive: não há filtro `exclude:` que silencie o par sem o operador resolver no FS. Operador resolve no FS ou aceita que aquele folder não sincroniza. Justificativa para rejeitar warn-only como escape hatch de validação está em §Alternativas (razão distinta — perdível em CI/automação, paridade com ADR-010).
- **Escopo restrito a modos que bisync worktree** — aplica apenas a `git_handling: auto` (bisync do conteúdo não-repo) e `plain` (bisync puro). Skip silente em `bundle`/`skip` (esses modos não passam por `rclone bisync` do worktree). Paridade direta com ADR-010 §Decisão "Adicionalmente — escopo restrito a modos que usam bisync".
- **Reuso de `git.max_recursion_depth`** — uniformidade arquitetural; sem novo conceito de profundidade. Paridade com ADR-010 §Decisão (2).
- **`.git/` inteiro fora do escopo** — paridade com ADR-010 §Decisão (3); ADR-008 cobre repos git estruturalmente via `git_handling`. Hipotético `feature ↔ Feature` em `.git/refs/heads/` não é alvo deste validador.

Mensagem de erro multi-linha:

```
case-duplicates detectados em 'library' (Path1=/storage/areas):
Proton Drive é case-insensitive e trata cada par como mesma entry,
causando rclone safety abort (rc=7).

  - /storage/areas/family ↔ /storage/areas/Family
  - /storage/areas/finances ↔ /storage/areas/Finances
  - /storage/areas/hobbies ↔ /storage/areas/Hobbies
  - /storage/areas/learning ↔ /storage/areas/Learning
  - /storage/areas/work ↔ /storage/areas/Work

Cleanup é responsabilidade do operador (FS surgery). Decida rename,
merge ou delete por par; re-execute `--check`.
```

3-way colisão (`family/Family/FAMILY`) emite um bullet único com os 3 nomes `↔`-separados, não 3 bullets de pares (clareza editorial — operador vê a colisão completa).

## Consequências

### Benefícios

- Cenário do incidente library 2026-06-08 (rc=7 sem mensagem actionable + 12h até `_check_folder_staleness` sinalizar) vira erro de `--check` antes do start. Detecção move-se de "operador descobre degraded after 12h e faz peek em `.lst-err`" para "operador vê pares ao validar config".
- Defesa em profundidade complementa, não substitui, comportamento atual: bisync errors continuam non-recovering (invariante CLAUDE.md "bisync errors do NOT auto-recover"); validador config-time é uma camada nova de proteção pré-runtime.
- Mensagem instrui ação primária (cleanup operator-driven); sem auto-resolve preserva controle do operador sobre decisões de FS (rename `family→Family` vs `Family→family` vs merge é semântica de conteúdo, fora do drive-sync).
- Paridade arquitetural com ADR-010 reduz custo cognitivo — operador que entende um validator entende o outro; mesma estrutura, mesmo skip rules, mesmo padrão de mensagem.

### Trade-offs

- **I/O extra no `--check`** — scan recursivo até depth=6 em cada folder com modo `auto`/`plain`. Dois traversals sequenciais sobre a mesma subtree (`_validate_auto_exclude_against_code` + `_validate_case_duplicates_against_remote`), com page cache mitigando re-leitura mas não eliminando custo. Fusão em traversal único registrada como gatilho de revisão. Aceitável: `--check` é one-shot interativo, não loop quente.
- **Sem cobertura runtime — caso paradigmático recorre sem faceta 2** — o incidente library 2026-06-08 ocorreu **com daemon rodando** durante FS migration do meta-system, exatamente a janela que `--check` não cobre. Trade-off explícito: cobertura runtime tem custo de I/O alto a calibrar (faceta 2 separada no `BACKLOG.md`); operador que executa FS migration com daemon rodando continua exposto ao rc=7 genérico até faceta 2 mergeada. Mitigação operacional ritual: rodar `--check` após FS surgery em qualquer folder monitorado. ADR-011 entrega valor primário em re-validações pré-start (deploy de nova config, restart pós-incidente, audit periódico); cobertura mid-cycle é faceta 2.
- **Falha-fast vs continuidade — direção decidida em follow-up** — fronteira tratada em §Alternativas com duas alternativas paralelas (falha-fast global vs skip-folder + degraded). Status `Proposto` reflete a indecisão; próximo `/run-plan` (ou `/triage` triggered por incidente adicional com múltiplos folders simultaneamente) escolhe direção e move Status para `Aceito`.

### Limitações

- **Não cobre crescimento pós-`--check`** — paridade com ADR-010 §Limitações: defesa em profundidade é gate inicial, não invariante runtime. Faceta 2 (runtime probe pre-bisync) é o follow-up registrado em `BACKLOG.md`.
- **Detecta apenas siblings sob mesmo parent** — `family/` e `subdir/Family/` não colidem no Proton (paths diferentes), corretamente ignorados. Mas case-duplicates em **paths estruturalmente equivalentes mas com case diferente nos parents** (`A/file` + `a/file`, onde `A/` e `a/` já são case-duplicates) são detectados pelo nível superior; nada a corrigir.
- **`.git/` inteiro fora do escopo (lacuna em modo `plain`)** — para folders com `git_handling: plain` apontando para repo git (modo legítimo segundo ADR-008 tabela), case-duplicates em `.git/refs/heads/` (ex.: branches `feature` + `Feature` git-criados) produzem mesmo rc=7 sem cobertura deste validador. ADR-008 classifica modo, não previne case-duplicates internas ao `.git/`. Cenário raro (operador com 2 branches case-only diferentes é exótico), mas registrado como gatilho de revisão em vez de ampliar escopo agora (default conservador).
- **`local_path` inexistente skip silente** — paridade com ADR-010 §Limitações; preserva fluxo bootstrap.

### Mitigações

- **Mensagem com paths absolutos `↔`-separados** elimina forense manual em `.lst-err`.
- **3-way colisão agrupada** em um bullet evita inflar a mensagem para operador com cenários degenerados.
- **Falha-fast no parent antes de expansão de `subpath_overrides`** (ordem do `load_config`): syntactic overrides herdam estado limpo; mensagem aponta o folder original, não a synthetic entry.

## Alternativas consideradas

### Runtime probe pre-bisync por ciclo

Scan executado antes de cada `rclone bisync` em `sync_engine.py`. Cobre mutações pós-`--check` (caso real do incidente). **Recusada como faceta 1**: custo de I/O — `/storage/areas/` com 61285 entries top-level + recursive depth=6 representa potencialmente milhões de dirs por cycle. Validador config-time entrega a maior parte do valor (operador roda `--check` após FS surgery por ritual) com custo zero no daemon. Reservada como **faceta 2 separada** no `BACKLOG.md` para calibração futura (top-level only, depth menor, ou trigger lazy on rc=7).

### Classificador rc=7 reactive (parser stderr)

Análogo ao classificador auth de ADR-003 (`_AUTH_CODES`). Parser de stderr discriminando rc=7 case-duplicates vs outras causas rc=7; emite STATUS estruturado pós-falha. **Recusada como faceta 1**: reactive (já após o safety abort, bisync state precisa de `--resync` manual); proativa (config-time) entrega mensagem actionable antes do dano. Reservada como **faceta 3 separada** no `BACKLOG.md` como defesa em profundidade reactive (caso facetas 1 e 2 probes percam algo: race condition, ecossistema/cenário não previsto).

### Auto-resolve por rename

Drive-sync renomeia automaticamente um lado do par (ex.: `Family` → `Family-conflict-<timestamp>`) para destravar bisync. **Recusada**: viola princípio "controle do operador, não inferência" estabelecido em ADR-010. Operador decide qual lado preservar com base no conteúdo (qual `family/` tem dados válidos, qual é vazio resíduo de migração) — drive-sync não tem essa informação. Auto-resolve seria intervenção destrutiva mascarada como conveniência.

### Warn-only no `--check`

Imprime warning mas retorna 0. **Recusada** por paridade com ADR-010 §Alternativas Direção (2): warnings perdíveis em CI/automação; incidente comprova que sinalização silenciosa é equivalente a sem sinalização. Operador headless (fixup do `mr` cross-repo, scripts de bootstrap) não vê warnings interativos.

### Skip do folder afetado + degraded ao start (alternativa paralela, não decidida)

`--check` reporta os pares mas retorna 0; folder com case-duplicates entra em `_degraded_folders` (canal ADR-005, reason `case-duplicates: N pares`); daemon inicia e os outros folders rodam normalmente. Reaproveita infra ADR-005 sem novo canal de sinalização.

Argumentos a favor (pattern ADR-005): per-folder sem pausa global é o pattern estabelecido para sintomas localizados — "as outras pastas continuaram sincronizando OK durante os 7 dias do `dev-projects`. Pausar tudo penalizaria pastas saudáveis sem benefício" (ADR-005 §Razões). 1 folder com case-duplicates não justifica bloquear daemon inteiro.

Argumentos a favor de **falha-fast global** (decisão central, §Decisão linhas 26-34): operador headless precisa sinalização indubitável de que algo precisa ação manual antes do daemon começar a syncar; daemon parcialmente rodando mascara o problema (operador vê service `active`, não percebe que folder X está fora); paridade com ADR-010 §Decisão (config inválida bloqueia start) reduz custo cognitivo no menu de validators.

**Status `Proposto` reflete esta indecisão.** Próximo `/run-plan` que implementa o validator ou `/triage` triggered por incidente adicional (múltiplos folders simultaneamente em FS migration ampla) escolhe direção e move Status para `Aceito`. Critério de desempate provável: surgimento de cenário real onde falha-fast bloqueia operador em produção (penalidade desproporcional) → skip-folder ganha; ausência desse cenário em N meses → falha-fast confirma.

### Marker estilo ADR-010 (constante de "case-duplicates conhecidos")

Lista hardcoded de pares problemáticos (`family/Family`, `work/Work` etc.). **Recusada**: case-duplicates emergem dinamicamente de operações do operador (FS migrations, organização ad-hoc); lista estática seria sempre incompleta. Detecção sintática por `name.lower()` é correta-por-construção; cobre qualquer par sem manutenção.

## Gatilhos de revisão

- **Proton publicar cliente Linux nativo case-aware** ou rclone backend protondrive ganhar feature de "case mapping" — eliminaria a assimetria Path1↔Path2 e tornaria este ADR obsoleto. Acompanhar rclone#7381 e rclone#issues novos referenciando case-handling no backend protondrive.
- **Faceta 2 (runtime probe) materializada** — config-time + runtime juntos podem permitir consolidar este ADR num único "case-sensitivity policy". Avaliar substituição vs extensão na época.
- **Incidente de case-duplicates dentro de `.git/`** em folder modo `plain` (branches `feature` + `Feature` no mesmo repo, ou hipotético em `.git/objects/`) — sinal: aparecimento de `rc=7` em folder `plain` cujo `local_path` é repo git e cujo bisync inclui `.git/refs/`. Trigger: ampliar escopo do validador para `.git/refs/` em modo `plain`, OU registrar lacuna como aceita (cenário muito raro).
- **Latência do `--check` excede ≥2× a baseline atual** após inclusão do validador — investigar se os dois traversals (`_validate_auto_exclude_against_code` + `_validate_case_duplicates_against_remote`) podem ser fundidos em walk único. Paridade com gatilho de ADR-010.

## Referências

- [ADR-010](ADR-010-validacao-config-time-auto-exclude-markers-codigo.md) — precedente direto do padrão validador config-time `--check` + fatal-error com paths concretos.
- [ADR-008](ADR-008-abandonar-bisync-repos-git.md) — divisor estrutural para `.git/` (fora do escopo deste validador) e modos que bisync worktree (`auto`/`plain` aplicam, `bundle`/`skip` skipados).
- [ADR-005](ADR-005-folder-staleness-degraded.md) — sintoma observável atual (degraded 12h depois sem mensagem actionable), substituído proativamente por este validador no caminho-comum.
- [`.claude/local/NOTES.md § 2026-06-08T16:42:26Z`](../../.claude/local/NOTES.md) — peek empírico no `.lst-err` + mapeamento das 4 sub-decisões (a/b/c/d) que originaram este ADR e as facetas 2/3 no backlog.
- [rclone#7381](https://github.com/rclone/rclone/issues/7381) — issue upstream sobre comportamento do backend protondrive; trigger de revisão se resolvido com feature de case mapping.
