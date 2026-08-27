# Spec: auto-recuperação data-safe de rc=7 stale-listings no daemon

- Frozen at: 2026-08-27 (frozen after operator approval on 2026-08-27)
- Spec version: v1
- Source: Problem Brief `briefs/auto-resync-gated-rc7-stale-listings.md` v1 (FROZEN) — tracker #47
- Status: FROZEN
- Amendments: none

> Sem PRD (rota Brief→Spec decidida com o operador 2026-08-27: feature de 1 job num
> daemon existente; o "what" já vive no Brief). A traceabilidade cita o Brief por ID
> (`brief:J1`, `brief:S1`…) no lugar de `PR*`.

## Design

Não é product-scale — não há componente novo, data store, nem escolha de arranjo de
módulos. É uma exceção restrita adicionada a UMA função existente
(`sync_engine.bisync_folder`) + um knob de config + testes. Portanto SEM subseções de
Architecture/Data-model/Interfaces/Tech-choices. Só as decisões de design que os open
questions do Brief genuinamente exigem, mais o cross-cutting de observabilidade.

**Approach:** dentro do branch `rc != 0` de `bisync_folder` (F5), quando o stderr casa a
assinatura stale-listings, o engine tenta uma auto-recuperação **gated por dry-run**:
prova que `--resync` é união no-op (0 transfers / 0 deletes) e só então reconstrói o
baseline com o `--resync` real; senão permanece degradado (caminho de hoje). Guard de
1-tentativa-por-episódio em memória. Tudo em `sync_engine` (onde vivem o cmd/flags/marker
exatos); o daemon fica quase intocado — a recuperação é transparente (recupera→`True`;
falha→`False`, e o staleness ADR-005 sinaliza como hoje).

### Design decisions (resolvem os open questions do Brief)

- **D1 — Onde a exceção abre: em `sync_engine.bisync_folder`, no branch `rc != 0`, gated por
  assinatura stale-listings.** Só `bisync_folder` tem o cmd + flags + excludes + marker
  live e git_handling-aware (F3/F5); reconstruir o baseline exige re-invocar EXATAMENTE esse
  cmd. Segue o split do repo "sync_engine faz a mecânica rclone, daemon sinaliza". **NÃO**
  levanta erro tipado ao daemon (diferente de `StuckJobError`/`AuthDegradedError`): a
  recuperação re-invoca o cmd local, então mora onde o cmd é montado — não no daemon.

- **D2 — Gate = dry-run dedicado (2 passadas), não interpretar a própria tentativa** (resolve
  open-Q1). O `--resync` real é união e MUTA (F2: ressuscita conteúdo deletado). Logo não dá
  para "interpretar a saída da própria recuperação" — ela já agiu. A prova tem de ser um
  passo `--resync --dry-run` ANTES: se reportar união no-op (0 transfers, 0 deletes) → roda o
  `--resync` real; senão → não age. Custo: a passada extra de listagem do dry-run (C3 aceita
  "a passada que a recuperação faria de qualquer modo"; o dry-run é a prova adicional,
  bounded, e só ocorre num folder já preso). O candidato "interpretar a própria tentativa" é
  rejeitado por inseguro.

- **D3 — `--resync-mode path1` NÃO é usado no caminho auto.** A recuperação manual do
  Brief-pai usou `--resync-mode path1` (F6), mas ele só importa quando há divergência a
  resolver. Aqui o `--resync` real só dispara APÓS o dry-run provar união no-op — não há nada
  a copiar/deletar em direção alguma, então `--resync-mode` é inócuo. Omiti-lo mantém o cmd
  mais simples sem perder segurança (a segurança vem do gate dry-run, não do mode).

- **D4 — Guard de 1-tentativa-por-episódio, in-memory** (resolve open-Q2: retry + "1
  tentativa"). `RcloneEngine` mantém `set[Path]` de markers com auto-recuperação já tentada
  neste episódio. Ao detectar stale-listings: marker no set → **pula** a auto-recuperação
  (loga skip, retorna `False`, segue degradado via ADR-005 como hoje); senão → registra o
  marker e tenta. Limpo em QUALQUER sucesso de bisync (o `marker.touch()` existente). Reseta
  no restart (novo processo → set vazio → 1 tentativa fresca; aceito, simétrico ao
  re-avaliar-janela pós-restart de ADR-007). **Retry = mesmo-ciclo** (dry-run + resync rodam
  inline na única chamada de `bisync_folder`), não próximo-ciclo — mais simples e
  self-contained. Sem loop: o caso divergente é tentado no máximo 1×/episódio, evitando o
  custo do dry-run a cada ciclo num folder divergente preso.

- **D5 — Kill-switch de config `rclone.auto_resync_stale_listings: bool` (default `true`).**
  C1 exige exceção restrita e fail-safe; precedente do repo é que toda feature de recovery
  ganha knob (`infra_storm_threshold`, `max_job_runtime_seconds`). Default ON (o ponto é
  reduzir MTTR); opt-out para o operador paranoico ou se a parse do dry-run algum dia se
  provar não-confiável. Validado em `load_config` como os irmãos. Com `false`, o comportamento
  é exatamente o de hoje (loga `BISYNC_FAIL`, segue degradado).

### Cross-cutting

- **Observabilidade (brief:S3).** Tag dedicada `[BISYNC_AUTORESYNC]`, greppável, no padrão
  das tags de ADR-012, com os desfechos distintos:
  `attempted` · `recovered` · `skipped (divergent: pre-check reprovou — Nt/Nd)` ·
  `skipped (already attempted this episode)` · `skipped (disabled)`.
  `journalctl --user -u drive-sync --grep "BISYNC_AUTORESYNC"` dá o dispatch.
- **Sem double-signal com o watchdog (brief:S3).** Auto-recuperação bem-sucedida é
  **silenciosa no notify-send** (só journal), como o reset de degraded de ADR-005 — não
  dispara alerta. O caso divergente/falho permanece degradado e sinaliza pelo caminho
  existente (staleness ADR-005 → watchdog ADR-014). Nenhum `Notifier` novo.
- **Error handling.** Se o `--resync` real (pós-prova) falhar (ex.: 5xx no meio), retorna
  `False` sem re-tentar (guard já marcou tentado) → degradado. Um `AuthDegradedError` ou
  `StuckJobError` levantado durante o dry-run/resync propaga normalmente (o daemon já os
  trata). O TOCTOU dry-run→resync sob o lock serializado (ADR-001) só admite mudança de FS
  local benigna (upload de arquivo novo é desejado, nunca ressurreição); o remote só muda via
  este daemon, que segura o lock — risco benigno, registrado como invalidator.

## Task plan

Ordenado por dependência. Sem PRD → cada task cita o Brief (`brief:*`). Acceptance = teste
pytest persistido (regressão), no padrão de `tests/`.

- **SP-T1** (spike): fixar o formato de saída de `rclone bisync --resync --dry-run` (v1.74.3)
  e a estratégia de parse robusta de "união no-op (0 transfers, 0 deletes)". — resolves: o
  risco R1 (a prova de segurança inteira repousa nessa parse). — done when: finding registrado
  em `docs/spikes/SP-T1-autoresync-dryrun-parse.md` com amostras reais de saída (caso benigno
  no-op E caso divergente), o sinal exato a parsear, e a decisão de parse (linha-summary vs
  contagem de operações). — depends on: —

- **SP-T2**: classificador da assinatura stale-listings em `sync_engine` (regex + helper
  `_is_stale_listings(stderr) -> bool`), irmão de `_classify_rclone_stderr`. — serves
  `brief:N2` (assume a assinatura reconhecida) — acceptance (EARS): "WHEN o stderr de um rc=7
  contém `cannot find prior Path1 or Path2 listings` E/OU `Must run --resync to recover`, o
  SISTEMA SHALL classificá-lo como stale-listings; IF o rc=7 vem de outra causa (ex.:
  case-duplicates `they exist?`) THEN o SISTEMA SHALL NOT classificá-lo como stale-listings."
  — depends on: —

- **SP-T3**: knob `rclone.auto_resync_stale_listings: bool = true` (D5) — campo em
  `RcloneConfig`, parse+default em `load_config`, doc em `config/config.yaml.example`. —
  serves `brief:C1` — acceptance (EARS): "the SISTEMA SHALL default `auto_resync_stale_listings`
  a `true` quando ausente; WHERE `auto_resync_stale_listings: false`, o SISTEMA SHALL manter o
  comportamento atual (loga `BISYNC_FAIL`, sem tentar auto-recuperação)." — depends on: —

- **SP-T4**: mecanismo gated de auto-recuperação em `bisync_folder` (D1-D4) — no branch
  `rc != 0`, se stale-listings (SP-T2) E habilitado (SP-T3) E guard permite: rodar
  `--resync --dry-run` (parse de SP-T1); provado no-op → rodar `--resync` real e retornar seu
  resultado; senão → `False`. Guard `set[Path]` em `RcloneEngine`, limpo no sucesso. Tag
  `[BISYNC_AUTORESYNC]` (D5). — implements `brief:J1`; serves `brief:S1`, `brief:S2`,
  `brief:C2`, `brief:C3` — acceptance (EARS):
  - "WHEN um folder aborta com rc=7 stale-listings E o dry-run de `--resync` prova união no-op,
    o SISTEMA SHALL rodar o `--resync` real e retomar o folder com sucesso na MESMA chamada,
    sem intervenção manual." (brief:S1)
  - "IF o dry-run de `--resync` reporta QUALQUER transfer ou delete (divergência real), THEN o
    SISTEMA SHALL NOT disparar `--resync` e SHALL manter o folder degradado + sinalizado; zero
    transfer/delete é disparado pela auto-recuperação." (brief:S2 · C2)
  - "WHILE um folder já teve auto-recuperação tentada no episódio corrente (sem sucesso de
    bisync intermediário), o SISTEMA SHALL NOT re-tentar a auto-recuperação naquele folder
    (sem loop de dry-run)." (D4)
  - depends on: SP-T1, SP-T2, SP-T3

- **SP-T5**: doc-sync + ADR (obrigatório: a feature muda o comportamento do invariante
  documentado). — (a) novo `docs/decisions/ADR-019-auto-resync-gated-rc7-stale-listings.md`
  registrando a decisão (promoção do design-decision, regra de decision-record); (b) nota
  mínima da **exceção-restrita** na linha do invariante `bisync errors do NOT auto-recover` em
  `CLAUDE.md` (superfície user-facing) apontando para o ADR-019; (c) atualizar o playbook
  `docs/operations/playbook-bisync-recovery.md` (branch rc=7) notando que o caso benigno agora
  auto-recupera (o passo manual vira fallback do caso disabled/divergente). — serves doc-sync +
  `brief:S3` — acceptance: os 3 docs referenciam a nova behavior; `--grep BISYNC_AUTORESYNC`
  aparece no playbook como sinal de investigação. — depends on: SP-T4

## Coverage check (cada item in-scope → ≥1 task)

- `brief:J1` (auto-curar gated) → SP-T4
- `brief:S1` (MTTR benigno → ~1 ciclo) → SP-T4 (acceptance 1)
- `brief:S2` (ZERO auto-recuperação em divergência) → SP-T4 (acceptance 2)
- `brief:S3` (observabilidade + sem double-signal) → SP-T4 (tag) + SP-T5 (playbook)
- `brief:C1` (exceção restrita/fail-safe) → SP-T3 (kill-switch) + SP-T4 (fail-safe: falha→degradado)
- `brief:C2` (data-safety pela união) → SP-T1 (parse) + SP-T4 (acceptance 2)
- `brief:C3` (custo proporcional) → SP-T4 (D2: dry-run + resync, sem hash item-a-item)
- `brief:N2` (assume assinatura reconhecida) → SP-T2
- Sem task órfã (nenhuma task sem item de Brief).

## Deliberate exclusions (do Brief)

- **`brief:N1`** (auto-recuperação de too-many-deletes rc=1) — fora de escopo, **deferral
  value-affirmed** já armado no Brief-pai. Este Spec É o gatilho: `Trigger-source:` a
  reavaliação do N2 do Brief-pai `briefs/recovery-safety-abort-bisync.md` — fires when SP-T4
  (J1) mergear e a extensão a rc=1 puder herdar o padrão do pre-check. (Também é o open-Q3 do
  Brief; ver Open questions.)
- **`brief:N3`** (procedimento manual para operador frio) — já entregue pelo Brief-pai
  (playbook). Bare scope-boundary. Não armado (não é deferral de capacidade deste esforço).
- **`brief:N4`** (refinar o TEXTO do invariante na doutrina — sibling doc) —
  **carried-narrowed**: a regra doc-sync do software-track proíbe enviar comportamento que
  contradiz o invariante documentado, então SP-T5 puxa a **nota mínima** da exceção-restrita +
  o ponteiro para ADR-019 para dentro deste Spec. Um refinamento de doutrina mais profundo que
  a nota (se merecer) permanece o sibling N4. Registrado como narrowing consciente, não
  compressão silenciosa. — Surfaçado ao operador (2026-08-27): decisão de puxar a nota mínima.
- Candidato de pre-check "comparação top-level de árvores" — NÃO adotado como o gate (é proxy
  raso que deixa passar divergência aninhada, fere C2/S2); herdado do Brief §Deliberate
  exclusions. Sobrevive só como possível otimização de early-reject barato ANTES do dry-run,
  não como a prova — não implementado neste Spec (`Value-rejected:` como gate; como otimização,
  deferido sem armar — grow-by-need, sem valor afirmado isolado).
- Candidato "hash item-a-item da árvore inteira" — rejeitado (excede C3). `Value-rejected:`
  custo desproporcional sem ganho sobre provar o no-op da própria união.

## Risks / unknowns

- **R1 — parse da saída de `rclone bisync --resync --dry-run` é frágil / version-específica.**
  Toda a garantia de data-safety (S2) repousa em detectar corretamente "união no-op". Uma parse
  que falhe para o lado errado (achar no-op quando há divergência) causaria a ressurreição que
  a feature existe para evitar. — mitigação: **SP-T1 (spike dedicado)** fixa o formato em
  v1.74.3 com amostras reais dos dois casos ANTES de SP-T4; fail-safe por construção (parse
  ambígua/erro → trata como NÃO-no-op → não age, segue degradado).
- **R2 — rclone atualiza e muda o formato de saída do dry-run.** — mitigação: o kill-switch
  (D5) permite desligar; SP-T1 registra a versão testada; parse conservadora (fail para
  não-agir). Gatilho de revisão: bump de rclone major/minor.

## Assumptions & invalidators

- **A parse de SP-T1 detecta no-op de forma confiável em v1.74.3** — invalidado se, no build,
  a saída do dry-run não expuser um sinal robusto de "0 operações" (então: escalar para um gate
  alternativo — ex.: `rclone check` dos dois lados — ou manter a feature desligada por default).
- **O `--resync` real dispara apenas após prova no-op, tornando `--resync-mode` inócuo (D3)** —
  invalidado se um caso real mostrar transfer/delete no `--resync` real apesar do dry-run
  no-op (TOCTOU não-benigno) — sinal: um `[BISYNC_AUTORESYNC] recovered` seguido de bytes
  transferidos no log do resync. Improvável sob o lock serializado (ADR-001).
- **Guard in-memory + reset-no-restart é suficiente** — invalidado se um crash-loop de restart
  frequente fizer a auto-recuperação divergente re-disparar o dry-run com custo material; sinal:
  `[BISYNC_AUTORESYNC] attempted` repetido a cada restart num folder divergente.

## Open questions

- **open-Q1** (dry-run dedicado vs interpretar a própria tentativa) — **RESOLVIDO** em D2
  (dedicado; interpretar a própria é inseguro).
- **open-Q2** (retry mesmo-ciclo vs próximo; limite 1 tentativa) — **RESOLVIDO** em D4
  (mesmo-ciclo, 1 tentativa/episódio via guard in-memory).
- **open-Q3** (ao fechar o build: satisfazer o doc do invariante N4 por ponteiro + re-firar o
  `Trigger-source` do N2/rc=1 do Brief-pai) — **DEFERIDO ao close do build** (SP-T5 faz o
  ponteiro N4; a re-fira do gatilho rc=1 é ação de cycle-close, registrada no Deliberate
  exclusions acima como `Trigger-source` armado).
