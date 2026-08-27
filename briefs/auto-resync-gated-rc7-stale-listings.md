# Problem Brief: auto-recuperação data-safe de rc=7 stale-listings no daemon

- Frozen at: 2026-08-27 (frozen after operator decision on the central risk fork — pre-check posture = dry-run gated, 2026-08-27)
- Mode: verifiable
- Distilled from dossier: auto-resync-gated-rc7-stale-listings (`.throughline/dossiers/`)
- Brief version: v1
- Status: FROZEN
- Parent frame: `briefs/recovery-safety-abort-bisync.md` (v1) — este Brief é o braço de CÓDIGO (daemon) da mesma família de recovery; o pai cobre o playbook MANUAL (J1) e defere a auto-recuperação de rc=1 (seu N2) com `Trigger-source: #47`. Este Brief é esse #47.
- Amendments: none

## Problem statement
Uma queda de rede no meio de um `rclone bisync` longo destrói o estado de listagem (`.lst`) do bisync —
os dados locais e remotos ficam intactos, só o estado de baseline morre — e o daemon passa a abortar
aquela pasta a cada ciclo com um erro stale-listings ("cannot find prior Path1 or Path2 listings / Must
run --resync to recover"), permanecendo degradado **indefinidamente até uma recuperação manual**. O modo
de falha é estrutural em pastas grandes (quanto maior a árvore, mais longa a janela de listagem em que uma
queda de rede corrompe o estado), e o caso dominante é **benigno** (estado perdido, árvores ainda
coincidem) — auto-recuperável sem violar o espírito de "nunca auto-recuperar divergência genuína", desde
que a segurança do dado seja provada antes de agir.

## Jobs in scope
- J1: Quando o daemon classificar um abort do tipo stale-listings **E** provar, por um pre-check
  data-safe, que a recuperação não altera dado, auto-curar a pasta (reconstruir o baseline de listagem via
  o mecanismo de recuperação live-flags do próprio daemon) em vez de permanecer degradado — reduzindo o
  tempo-até-recuperação do caso benigno de horas/dias para ~1 ciclo.

## Non-goals (explicitly out of scope)
- N1: Auto-recuperação do abort too-many-deletes (remoção legítima em massa >50%) — bare scope-boundary
  aqui, mas é um deferral value-affirmed no Brief-pai (seu N2), cujo guard-rail é mais delicado (divergência
  real; a direção da recuperação importa). Este Brief é o gatilho que o pai nomeia. — `Trigger-source:` a
  reavaliação do N2 do Brief-pai — fires when o mecanismo deste Brief (J1) estiver implementado e a extensão
  ao too-many-deletes puder herdar o padrão do pre-check.
- N2: Discriminar o abort stale-listings de outras causas do mesmo exit-code (ex.: colisões
  case-insensitive) — parser de stderr é infra compartilhada, mas a discriminação é eixo próprio. Bare
  scope-boundary: este Brief assume que o classificador reconhece a assinatura stale-listings específica.
- N3: O procedimento de recuperação MANUAL para operador/agente frio — já entregue pelo Brief-pai. Bare
  scope-boundary (superfície distinta: doc vs código).
- N4: Refinar o texto do invariante na doutrina do repo com o caminho de reconstrução-de-baseline — doc,
  sibling. Bare scope-boundary.

## Constraints
- C1: **Preservar o ESPÍRITO do invariante "erros de bisync não auto-recuperam".** A exceção é RESTRITA e
  fail-safe: auto-recupera SÓ quando o pre-check prova data-safety; em qualquer outro caso (pre-check não
  passa, ou a recuperação falha) permanece degradado e sinaliza. Existe precedente no repo: uma exceção
  igualmente restrita, gated-por-probe, já foi aberta ao mesmo invariante para flakiness transitória de
  provedor — este Brief segue a mesma forma.
- C2: **Data-safety informada pela semântica de UNIÃO da recuperação (F2).** A operação de recuperação
  (`--resync`) é uma união: itens presentes só no lado remoto são copiados de volta ao local. Uma
  recuperação indevida portanto **RESSUSCITA conteúdo deletado** — não é neutra. O pre-check tem de provar
  que a união é um no-op (0 transferências, 0 deleções) ANTES de agir; um proxy raso que não veja
  divergência aninhada é insuficiente.
- C3: **Custo proporcional; roda sob o lock serializado de rclone.** O pre-check ocorre no mesmo caminho
  serializado das chamadas rclone (uma corre por vez). Aceita-se o custo de uma passada de listagem (a
  operação de recuperação faria essa passada de qualquer modo); recusa-se custo que exceda isso
  materialmente (ex.: comparação de hash item-a-item da árvore inteira).

## Success criteria
- S1: O tempo-até-recuperação do caso stale-listings benigno cai de horas/dias para ~1 ciclo. — verifiable:
  dado um estado `.lst` destruído com as árvores local/remoto coincidentes, o daemon retoma a pasta com
  sucesso dentro de 1 ciclo, sem intervenção manual.
- S2: ZERO auto-recuperação em qualquer caso genuinamente divergente. — verifiable: dado um estado onde a
  união NÃO é no-op (o lado remoto tem itens ausentes no local, ou vice-versa), o pre-check reprova e a
  pasta permanece degradada + sinalizada; nenhuma transferência/deleção é disparada pela auto-recuperação.
- S3: Observabilidade house-style. — verifiable: o dispatch da auto-recuperação (tentada / bem-sucedida /
  reprovada-no-pre-check) é greppável no journal com tag dedicada, no padrão das tags de erro existentes;
  sem double-signal indevido com o watchdog externo.

## Key facts and provenance
- F1: A subject deste Brief é o daemon drive-sync (sync bidirecional com Proton Drive via `rclone bisync`).
  — basis: model-prior.
- F2: A operação de recuperação `rclone bisync --resync` produz um SUPERSET (união) — arquivos presentes só
  no Path2 (remoto) são copiados de volta para o Path1 (local). Logo, disparar recuperação sem provar
  no-op RESSUSCITA no local conteúdo movido/deletado. — basis: retrieved (Brief-pai
  `recovery-safety-abort-bisync.md` F2; incidente drive-sync 2026-08-26, verificado ao vivo).
- F3: Deletar o marker de first-run do bisync (`~/.cache/rclone/bisync/drive-sync.<hash>.initialized`,
  `sync_engine.py:_state_marker_for`) faz o daemon disparar `--resync` no próximo ciclo com os flags/excludes
  EXATOS e ao vivo (git_handling-aware), evitando transcrição manual — este é o mecanismo de auto-cura
  disponível. — basis: retrieved (código + Brief-pai F3, 2026-08-26).
- F4: O safety-check do rclone compara o scan atual de Path1 contra o baseline `.lst`; enquanto o baseline
  não for reconstruído (via resync), o abort persiste — deletar só cópias stale de um lado não resolve. —
  basis: retrieved (Brief-pai F4, 2026-08-26).
- F5: Hoje o caminho de erro do bisync (`sync_engine.py:~438-448`) retorna falha em qualquer rc≠0 e
  **deliberadamente NÃO** invoca `--resync` automático — o comentário no código documenta o invariante
  ("não invocamos automaticamente para não causar perda de dados"). A exceção deste Brief é aberta
  exatamente aqui. — basis: retrieved (código, 2026-08-27).
- F6: Incidente-instância: folder grande (~105k arquivos), queda de rede mid-bisync, travado ~38h até
  `--resync` manual; a recuperação manual transferiu 0 B e fez 0 deleções (o caso benigno que este Brief
  auto-cura). — basis: retrieved (incidente drive-sync 2026-08-22/24).
- F7: Existe no repo o precedente de uma exceção-restrita gated-por-probe ao mesmo invariante de
  auto-recover (classe de flakiness transitória de provedor, com auto-resume só após um probe de sucesso
  real) — a forma "exceção restrita + gate + fail-safe para degradado" já está estabelecida. — basis:
  retrieved (ADR-016, 2026-08-27).

## Deliberate exclusions (from the dossier)
- Candidato de pre-check "comparação top-level de árvores" (a direção original do item de tracker) —
  carried-narrowed → NÃO adotado como o gate: é um PROXY raso que deixa passar divergência aninhada,
  violando C2 (S2). Sobrevive apenas como otimização possível (early-reject barato antes do pre-check
  data-safe), não como a prova. — decisão de risco do operador, 2026-08-27.
- Candidato de pre-check "comparação de hash item-a-item da árvore inteira" — rejeitado: seguro mas excede
  o custo aceitável (C3) sem ganho sobre provar o no-op da própria união.
- A postura "ficar manual / não auto-mutar" (fechar o item como não-vale-resolver) — considerada e NÃO
  adotada: o operador escolheu a auto-recuperação com o gate data-safe mais forte. Registrado como fork
  decidido, não como escopo reduzido.

## Solution-space status (NOT a solution)
- Vehicle: committed (código no daemon — a família de recovery já tem o braço-doc entregue; este é o braço
  de código). A postura de RISCO do pre-check está DECIDIDA pelo operador (2026-08-27): o gate é a prova
  data-safe direta (a recuperação em modo dry-run tem de reportar união no-op — 0 transferências, 0
  deleções — antes de agir), não um proxy estrutural raso.
- Settled when: a fase de solução (design/Spec → build) resolve as mecânicas AINDA abertas — onde no
  `sync_engine`/`daemon` a exceção é aberta; semântica de retry (mesmo ciclo vs próximo); limite de 1
  tentativa (auto-recuperação que falha → degradado, sem loop); reuso da infra de sinalização per-folder
  existente; a tag de journal. Nenhuma reabre a postura de risco (settled).

## Open questions carried forward
- Design/Spec: o dry-run de recuperação deve rodar como um passo dedicado do pre-check, ou o daemon
  interpreta a saída da própria tentativa de recuperação? (afeta reentrância e custo — 1 vs 2 passadas de
  listagem.)
- Design/Spec: retry imediato no mesmo ciclo pós-prova, ou marca-e-deixa o próximo ciclo executar?
- Ao fechar o build: reavaliar se o item de doc do invariante (sibling N4) é satisfeito por ponteiro, e
  re-firar o `Trigger-source` do N2 do Brief-pai (rc=1) agora que J1 existe.
