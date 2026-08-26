# Problem Brief: recuperação data-safe de safety-aborts recuperáveis do bisync

- Frozen at: — (withheld, draft)
- Mode: verifiable
- Distilled from dossier: recovery-safety-abort-rc1-bulk-move (`.throughline/dossiers/`)
- Brief version: — (withheld pending approval)
- Status: DRAFT — freeze pending approval
- Amendments: none

## Problem statement
Quando o estado do bisync de um folder é invalidado — seja por perda das listagens `.lst` (rc=7 stale-listings) ou por uma mudança em massa legítima que remove >50% dos itens da visão do bisync (rc=1 too-many-deletes) — o folder aborta a cada ciclo e fica degradado **indefinidamente**, e não existe um procedimento de recuperação único e data-safe que um operador/agente frio (sem o contexto do incidente) possa seguir. A única dica exibida no caso rc=1 é a genérica do rclone (`Run with --force if desired`), que é **perigosa**: propaga as deleções e causa perda de dados se o conteúdo não estiver salvo em outro lugar. A recuperação correta depende hoje de re-derivar mecânicas não-óbvias (marker-deletion para reconstruir o baseline; `--resync` é uma operação de UNIÃO que pode ressuscitar conteúdo; a ordem purge-cloud-antes-do-resync quando a intenção é dropar do cloud), conhecimento que vive só na cabeça de quem já resolveu um incidente.

## Jobs in scope
- J1: Dar a um operador/agente frio um procedimento único, organizado por sintoma/trigger e por intenção, para recuperar de um safety-abort recuperável do bisync (rc=7 stale-listings e rc=1 too-many-deletes) de forma **correta e sem perda de dados**, cobrindo a árvore de decisão de intenção no caso rc=1 (conteúdo movido vs. deletado de fato vs. re-homed em repo git).

## Non-goals (explicitly out of scope)
- N1: Impedir o alarme/abort em si — o safety brake NÃO pode inferir a intenção do operador; abortar em >50% de deleções é o comportamento correto e desejado. Bare scope-boundary (não é deferral de capability).
- N2: Auto-recuperação de rc=1 (classificador + integrity precheck → auto-resync gated), análoga ao caminho de auto-resync de rc=7. Guard-rail mais delicado (divergência real; a direção do resync importa) → procedimento manual documentado primeiro. — `Trigger-source:` GitHub drive-sync issue #47 (família auto-resync) — fires when a família de auto-resync de rc=7 for implementada e a extensão a rc=1 for reavaliada.
- N3: Enriquecer o log/notify no momento do rc=1 (tag própria + ponteiro para o playbook, substituindo a dica `--force` por advice safe) — é código em `sync_engine`, superfície distinta de um doc. — `Trigger-source:` a ser filado como issue de código separada (sibling do #47) — fires when o playbook (J1) existir e puder ser referenciado pela mensagem enriquecida.

## Constraints
- C1: **Data-safety by default.** Todo passo destrutivo (purge/force) só é prescrito depois de backup confirmado em outro lugar; o doc deve tornar impossível seguir a dica `--force` cega sem essa checagem.
- C2: **Preservar o invariante `bisync errors do NOT auto-recover`** (ADR-003 / CLAUDE.md §Operational Invariants): a recuperação é manual e documentada; nenhum passo introduz auto-cura no daemon.
- C3: **House style + esforço proporcional.** Artefato em `docs/operations/`, procedural (bash), com cross-ref a ADRs, no espírito de `playbook-flip-git-handling.md`. Peso proporcional a um doc.

## Success criteria
- S1: Um operador/agente frio que enfrenta um safety-abort recuperável consegue, seguindo só o doc, chegar a uma recuperação correta e sem perda de dados — verifiable: o doc contém (a) um branch de triage por trigger (rc=7 vs rc=1) que roteia para o caminho certo; (b) para rc=1, a árvore de intenção moved/deleted/re-homed; (c) a advertência explícita de que `--resync` é união e pode ressuscitar conteúdo; (d) a ordem data-safe backup → [purge-cloud se intenção=dropar] → marker-delete → resync; (e) a explicação de por que deletar-só-no-cloud sem clearar o marker não resolve (o safety check compara scan-de-Path1 vs baseline stale).

## Key facts and provenance
- F1: rc=1 too-many-deletes é disparado quando o scan atual de Path1 tem >50% menos itens que o baseline `.lst`; a mensagem do rclone sugere `--force`, que propaga as deleções. — basis: retrieved (incidente drive-sync `dev-projects`, 2026-08-26; rclone v1.74.3), verificado ao vivo.
- F2: `rclone bisync --resync` produz um SUPERSET (união) — arquivos presentes só no Path2 são copiados de volta para o Path1; portanto clearar o marker e resyncar sem antes purgar as cópias stale do cloud RESSUSCITA no local o conteúdo movido. — basis: retrieved (mesmo incidente, 2026-08-26), verificado ao vivo (o resync pós-purge NÃO ressuscitou).
- F3: O marker `~/.cache/rclone/bisync/drive-sync.<sha1(local|remote)[:16]>.initialized` (`sync_engine.py:_state_marker_for` / `:277`) controla o first-run; deletá-lo faz o daemon disparar `--resync` no próximo ciclo com os flags/excludes exatos e AO VIVO (git_handling-aware), evitando transcrição manual de flags. — basis: retrieved (código + incidente), verificado.
- F4: Deletar só as cópias stale no cloud NÃO limpa o abort: o safety check compara o scan de Path1 contra o baseline `.lst` (que ainda lembra os N itens), então o abort persiste até o baseline ser reconstruído via resync. — basis: retrieved (raciocínio validado no incidente 2026-08-26).
- F5: Prior art relacionado (mesma família de mecânica marker-delete/resync, trigger diferente): drive-sync issue #36 (docs: refinar invariante rc=7 com marker-deletion — undecided entre nota-no-invariante vs passo-no-playbook), #47 (auto-resync gated para rc=7 stale-listings — código), #50 (este; guarda-chuva rc=1). `playbook-flip-git-handling.md` já documenta a ordem purge+marker para o cenário de flip. — basis: retrieved (GitHub drive-sync issues + docs, 2026-08-26).
- F6: A subject deste Brief é o daemon drive-sync (bidirectional sync com Proton Drive via `rclone bisync`). — basis: model-prior.

## Deliberate exclusions (from the dossier)
- Candidato C do dossier (refinar só o invariante/CLAUDE.md, sem playbook novo) — rejeitado: subdimensiona; a árvore de decisão rc=1 não cabe num invariante. O invariante ganha no máximo um ponteiro para o playbook (parte do escopo de J1, não um artefato à parte).
- Candidato A do dossier (playbook estreito só-rc=1) — não adotado em favor do unificado por família (decisão delegada pelo operador, 2026-08-26): rc=7 e rc=1 compartilham a mecânica marker-delete/resync e o operador vive o problema por sintoma, não por rc-code. Não é redução de escopo de J1 — é a forma do home. Consolida a intenção-doc do #36.

## Solution-space status (NOT a solution)
- Vehicle: committed (um doc/playbook em `docs/operations/` — o vehicle é processo/documentação, não código novo no daemon; N2/N3 são os braços de código, deferidos).
- Settled when: já settled para este ciclo — a rota re-firada no hand-off aponta para build leve do doc agora (sem Spec governando `docs/operations/`).

## Open questions carried forward
- Ao fechar: reavaliar se #36 é fechado (satisfeito pelo playbook + ponteiro no invariante) ou re-escopado; decisão do operador no merge. Back-refs em #36/#47 PROPOSTOS, não postados (Brief é draft).
