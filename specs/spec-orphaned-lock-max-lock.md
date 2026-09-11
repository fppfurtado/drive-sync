# Spec: auto-expiração de lock órfão de bisync via `--max-lock` nativo

- Frozen at: 2026-09-10 (frozen after operator instruction "congele" on the rendered draft, 2026-09-10)
- Spec version: v2
- Source: Problem Brief `briefs/orphaned-bisync-lock-recovery.md` v2 (FROZEN) — tracker #88
- Status: FROZEN
- Relation: **Extends (not amends)** `specs/spec-autoresync-rc7-stale-listings.md` (v2, FROZEN) — comportamento NOVO e distinto (expiração preventiva de lock via flag nativa) sobre a MESMA superfície de montagem de cmd de `bisync_folder`. A LÓGICA do branch `rc != 0` de recuperação daquele Spec fica byte-intacta; a flag `--max-lock` é herdada pelas invocações de `--resync`/dry-run daquele branch apenas por serem montadas pelo builder base comum (D4) — o rc7 Spec NÃO é re-congelado nem re-versionado (nenhuma decisão dele muda). Ambos são braços-código da família de recovery (Brief-pai `briefs/recovery-safety-abort-bisync.md`). **A relação com o rc7 é agora também de COMPOSIÇÃO em runtime** (D6): expirar o lock desemboca num rc=7 que aquele Spec recupera.
- Amendments:
  - **v1→v2 (2026-09-10, tracker #88 — diagnóstico `debug`):** o **quê** — corrige o modelo de mecanismo (Approach, D1, D5) e adiciona **D6** (composição-com-rc7 + dependência de `auto_resync_stale_listings` + migração one-time); reescreve S1, a acceptance de SP-T3 e o coverage; adiciona **SP-T5** (migração). O **porquê** — o teste comportamental de SP-T3 refutou a v1: `--max-lock` é **prevenção write-time** (a expiração é governada pelo `TimeExpires` GRAVADO no lock, não pelo flag do leitor), e ultrapassar um lock expirado **deleta as listings → rc=7 stale-listings**, não um proceed limpo. Recuperação real = compor com ADR-019 (brief:F9). Absorve o Brief v2 (F9/F10). **Aprovação:** instrução do operador sobre o rascunho renderizado (2026-09-10).

> Sem PRD (rota Brief→Spec, precedente da família: feature de 1 job num daemon existente; o "what"
> vive no Brief). Traceabilidade cita o Brief por ID (`brief:J1`, `brief:S1`…) no lugar de `PR*`.

## Design

Não é product-scale — sem componente novo, data store, ou arranjo de módulos. É a adição de UMA
flag nativa do rclone (`--max-lock`) às invocações de `bisync` de um par + um knob de config +
testes. SEM subseções de Architecture/Data-model/Interfaces. Só as decisões que os open questions
do Brief exigem + o cross-cutting de doc-sync.

**Approach:** o abort `rc=1 prior lock file found` (brief:F5) surge de um `.lck` cujo processo dono
morreu sem liberá-lo; sem `--max-lock`, o rclone grava expiração efetivamente infinita (never-expire,
brief:F3), então o folder trava indefinidamente. A correção é `--max-lock <dur>` em toda invocação de
`bisync` do par, funcionando como **PREVENÇÃO write-time** (não como recuperador direto — brief:F9): o
rclone grava `TimeExpires = now + <dur>` e RENOVA a cada `<dur>`/2 enquanto o run vive (brief:F3,
observado ao vivo — logo um run legítimo longo segue protegido) e, quando o dono morre, o lock deixa de
renovar e **expira `<dur>` após a última renovação**. A recuperação do estado resultante NÃO é um proceed
limpo: ao ultrapassar um lock expirado, o rclone **remove o lock E deleta as listings**, abortando `rc=7
stale-listings` (brief:F9) — que o braço rc=7 (ADR-019, Spec estendido) então recupera de forma data-safe
(dry-run prova união no-op → `--resync` real; divergência → degradado). Assim a feature **COMPÕE** com o
rc7 (D6): `--max-lock` converte o wedge permanente rc=1 num rc=7 auto-recuperável no caso benigno. Nenhum
código de detecção de liveness custom. O daemon fica quase intocado (só a flag no cmd); a sinalização
degraded (ADR-005/watchdog) permanece como backstop para a janela `<dur>` até a expiração, para o caso
divergente, e para o caso (raro) de um bisync concorrente genuíno legítimo.

### Design decisions (resolvem os open questions do Brief)

- **D1 — Mecanismo = flag nativa `--max-lock`, não recuperação reativa custom** (resolve o crivo do
  Brief). O rclone provê expiração via `TimeExpires` gravado + renovação-enquanto-vivo (brief:F3/F9).
  Candidatos hand-rolled (limpeza no shutdown handler; PID-probe) são rejeitados no Brief (§Deliberate
  exclusions): o shutdown handler cobre só morte graciosa; o PID-probe reinventa a liveness que o backend
  já faz. Preserva C2 (não reinventar padrão nativo).

- **D2 — Knob `rclone.max_lock_seconds: int` (default `3600`), espelhando `max_job_runtime_seconds`.**
  `0` = desligado = comportamento de hoje (nenhum `--max-lock` no cmd → lock never-expire); `> 0` passa
  `--max-lock <N>s`. Serve de kill-switch (padrão da família — toda recovery ganha knob) E de tuning da
  janela. Default `3600` (1h): auto-cura muito antes dos 12h de staleness da ADR-005 (o operador em geral
  nem chega a ver o `[FOLDER_DEGRADED]`), com renovação a cada 30min protegendo run legítimo longo com
  folga larga. Validado em `load_config` como os irmãos.

- **D3 — Validação: `max_lock_seconds` ∈ {0} ∪ [120, ∞).** O rclone exige `--max-lock` ≥ 2m (120s);
  um valor em `1..119` é rejeitado em `load_config` (falha-fast, house style) com mensagem apontando o
  mínimo, em vez de deixar o rclone abortar em runtime com erro opaco. `0` é o opt-out explícito.

- **D4 — A flag vai em TODA invocação de `bisync` do par, não só no cmd principal.** `bisync_folder`
  monta o cmd principal (F6 do Brief); a recuperação rc=7 (Spec estendido) monta `--resync --dry-run`
  e `--resync` reais sobre o mesmo par/`.lck`. Todas recebem `--max-lock` (via o builder base comum),
  para semântica de lock uniforme — senão uma recuperação rc7 escreveria um lock never-expire e
  reabriria a janela que este Spec fecha. Ponto de inserção: onde o `_base_cmd()`/builder do bisync
  monta os flags globais do par (não em `mkdir`/`copyto`-de-bundle — `--max-lock` é conceito de bisync).

- **D5 — Sem código de auto-cura NOVO no daemon; a expiração é do backend e a recuperação é do braço
  rc7 existente.** Não há branch novo de recuperação neste Spec: a flag só muda o write-time do lock; o
  fluxo normal de retry (watcher + periodic full-sync) re-tenta o par a cada ciclo, e a primeira tentativa
  após a expiração cai em `rc=7 stale-listings` (brief:F9), tratado pelo branch `rc != 0` já existente
  (ADR-019). Menor superfície de código; nenhuma lógica de lock custom.

- **D6 — A recuperação COMPÕE com o rc7; dependência dura de `auto_resync_stale_listings`; migração
  one-time (absorve brief:F9/F10).** Consequências do modelo corrigido:
  - **Composição:** expirar o lock não recupera sozinho — desemboca num rc=7 (o rclone deleta as
    listings ao ultrapassar o lock expirado). O sucesso vem do braço ADR-019 (dry-run no-op → `--resync`).
    Logo esta feature tem **dependência dura de `rclone.auto_resync_stale_listings=true`**: com ele
    `false`, `--max-lock` converte o wedge rc=1 num wedge rc=7 (ainda degradado) — melhora parcial
    (o rc=7 é diagnosticável/recuperável pelo playbook), mas não auto-cura. Registrado como constraint
    de runtime; sinalizado no ADR-022 (SP-T4).
  - **Migração one-time (SP-T5):** locks never-expire PRÉ-EXISTENTES (`TimeExpires` ~infinito, escritos
    antes desta feature) NUNCA são expirados por `--max-lock` (o leitor honra o `TimeExpires` gravado —
    brief:F10); a feature só previne os FUTUROS. Os pré-existentes exigem `rm` manual one-time
    (documentado no ADR-022 + playbook). NÃO se adiciona código de limpeza de lock never-expire no
    startup: uma limpeza SEGURA exigiria prova de dono-morto (C1), e para essa classe o sinal de
    idade/renovação é inútil (um never-expire nunca renova, viva ou morta a origem) → precisaria justo
    do PID-probe que D1 rejeita (motivo INTRÍNSECO). A classe é FINITA (só os locks órfãos já existentes
    no deploy) e é drenada pela migração one-time SP-T5 — NÃO pela feature: um folder com lock
    never-expire fica travado até o `rm` manual (sem auto-dreno; F10). Custo aceito = uma migração por host.

### Cross-cutting

- **Observabilidade.** Sem tag nova dedicada: o caso hoje já loga `[BISYNC_FAIL] rc=1 ... prior lock
  file found` (ADR-012) durante a janela `<dur>` até expirar, e depois um `bisync concluído com
  sucesso` normal. A recuperação é silenciosa (sem `notify-send` novo) — o backstop de sinalização
  (ADR-005/watchdog) só dispara se a janela exceder os thresholds, o que com default 1h << 12h não
  ocorre no caso comum. O ADR (SP-T4) documenta grep de investigação existente.
- **Interação com ADR-018 (max-runtime kill).** Ortogonais: `max_job_runtime_seconds` mata um job vivo
  longo demais (e — nota do Brief F5 — esse kill é ele próprio uma FONTE de lock órfão, agora coberta por
  `--max-lock`); `max_lock_seconds` expira um lock cujo dono já morreu. Um job vivo continua renovando o
  lock até o kill de ADR-018; após o SIGKILL, o lock deixa de renovar e expira em `<dur>`.
- **Doc-sync.** A feature muda o comportamento do invariante documentado `bisync errors do NOT
  auto-recover` (o lock órfão deixa de exigir intervenção manual) → exige ADR + nota no invariante +
  playbook (SP-T4).

## Task plan

Ordenado por dependência. Sem PRD → cada task cita o Brief (`brief:*`). Acceptance = teste pytest
persistido (regressão), no padrão de `tests/`. O risco do mecanismo (o lock renova durante um run
vivo?) foi retirado no frame por teste ao vivo (F3), não re-spikado aqui.

- **SP-T1**: knob `rclone.max_lock_seconds: int = 3600` (D2/D3) — campo em `RcloneConfig`,
  parse+default+validação em `load_config`, doc em `config/config.yaml.example`. — serves `brief:C1`,
  `brief:C3` — acceptance (EARS):
  - "the SISTEMA SHALL default `max_lock_seconds` a `3600` quando ausente."
  - "WHERE `max_lock_seconds` está em `1..119`, o SISTEMA SHALL rejeitar a config em `load_config` com
    erro nomeando o mínimo de 120s (2m)."
  - "WHERE `max_lock_seconds: 0`, o SISTEMA SHALL NOT passar `--max-lock` a nenhuma invocação de bisync
    (comportamento never-expire de hoje)."
  - depends on: —

- **SP-T2**: passar `--max-lock <N>s` em toda invocação de `bisync` do par quando `max_lock_seconds > 0`
  (D1/D4) — ponto único no builder base do cmd de bisync, de modo que o cmd principal E os `--resync`/
  dry-run da recuperação rc7 herdem a flag. — implements `brief:J1`; serves `brief:S1`, `brief:N1`
  (bisync concorrente genuíno segue protegido pelo lock renovado) — acceptance (EARS):
  - "WHEN `max_lock_seconds > 0`, o SISTEMA SHALL incluir `--max-lock <max_lock_seconds>s` no cmd de
    `bisync` (principal e recuperação-rc7) daquele par." (brief:J1 · S1)
  - "WHILE um bisync do par está em execução, o lock permanece renovado (o SISTEMA delega a renovação ao
    rclone), de modo que uma segunda invocação concorrente sobre o mesmo par continua bloqueada." (brief:N1 · S2)
  - depends on: SP-T1

- **SP-T3**: teste de regressão. Cobre o MECANISMO corrigido (brief:F9) de forma rápida e determinística
  (sem esperar 2min de rclone vivo), via `rclone bisync` local-local direto num `--workdir` isolado
  (`bisync_folder` exigiria um remote nomeado — cap abaixo). Planta um `.lck` sobre um par com baseline
  limpo e observa o comportamento por `TimeExpires`. Mais os testes de superfície (nosso código): montagem
  do cmd e validação de config. — serves `brief:S1`, `brief:S2`, `brief:N1` — acceptance: pytest cobre
  (a) [mecanismo, brief:F9] um `.lck` com `TimeExpires` no PASSADO (feature-written expirado) NÃO produz
  abort `rc=1 prior lock file found` (o lock é ultrapassado/removido); o run aborta `rc=7 stale-listings`
  (as listings são purgadas) — provando que a expiração destrava o rc=1 e desemboca no caminho rc=7;
  (b) [contraste, brief:F9/F10 + S2/N1] um `.lck` com `TimeExpires` no FUTURO (never-expire, pré-feature)
  aborta `rc=1 prior lock file found` E o flag `--max-lock` do leitor NÃO muda isso (expiração é pelo
  `TimeExpires` gravado, não pelo flag) — pinando por que never-expire pré-existentes exigem migração (F10)
  e por que um lock vivo/renovado segue bloqueando concorrência (S2/N1);
  (c) [nosso código] cmd inclui `--max-lock 3600s` no default e omite com knob=0; `load_config` rejeita
  `1..119` e aceita `0`/`>=120`.
  Testes de integração (a)/(b) `skipif` sem rclone no PATH.
  **Cap de teste (LOG explícito no ADR — SP-T4):** (i) a COMPOSIÇÃO fim-a-fim (rc=7 → ADR-019 dry-run
  no-op → `--resync` → sucesso) roda em `bisync_folder` contra um remote real → NÃO automatizada aqui; o
  gate `_dryrun_resync_is_noop` já tem cobertura própria nos testes do ADR-019, e o no-op do caso benigno
  foi observado ao vivo no `debug` (2026-09-10); (ii) a RENOVAÇÃO-enquanto-vivo foi validada ao vivo no
  frame (brief:F3, ~100s) e não é re-testada (exigiria rclone vivo >2min). Sinal de regressão para ambos:
  reaparecimento de `prior lock file found` com bisync vivo em produção (invalidator). — depends on:
  SP-T1, SP-T2

- **SP-T4**: doc-sync + ADR (obrigatório: muda o comportamento do invariante documentado). — (a) novo
  `docs/decisions/ADR-022-orphaned-lock-max-lock.md` (decisão + o MODELO corrigido: `--max-lock` é
  prevenção write-time que COMPÕE com o rc7/ADR-019 — brief:F9; a dependência dura de
  `auto_resync_stale_listings=true` — D6; que o kill de ADR-018 é fonte de lock órfão agora coberta; a
  evidência viva do frame/`debug`; os caps de teste de SP-T3); (b) nota mínima no invariante `bisync
  errors do NOT auto-recover` em `CLAUDE.md` apontando o ADR-022 (lock órfão feature-written auto-expira
  e é recuperado pelo caminho rc=7; distinto e COMPOSTO com o auto-resync rc=7); (c) atualizar
  `docs/operations/playbook-bisync-recovery.md`: o `rm` manual do `.lck` órfão vira fallback para o caso
  comum feature-written (auto-expira em `<dur>`) E o passo obrigatório da MIGRAÇÃO one-time (SP-T5). —
  serves doc-sync — acceptance: os 3 docs referenciam a behavior composta + a dependência de
  `auto_resync_stale_listings`; `prior lock file found` aparece no playbook com o ponteiro para
  `max_lock_seconds` + a nota de migração. — depends on: SP-T2, SP-T5

- **SP-T5** (migração, brief:F10/D6): documentar a migração one-time — locks never-expire pré-existentes
  não são cobertos por `--max-lock` e exigem `rm` manual uma vez por host. NÃO é código (D6 rejeita
  limpeza automática no startup). Entrega: uma seção "Migração one-time" no ADR-022 e/ou no playbook, com
  o comando de diagnóstico (`grep` do `TimeExpires` distante nos `.lck` de `~/.cache/rclone/bisync/`) e o
  `rm` seguro (gated por dono-morto, como o recovery do incidente 2026-09-09). — serves `brief:F10` (o
  fato que a torna necessária) — acceptance: o doc lista (i) como identificar um `.lck` never-expire
  órfão, (ii) o `rm` seguro, (iii) que após a migração todo lock futuro é finite-expiry. — depends on: —

## Coverage check (cada item in-scope → ≥1 task)

- `brief:J1` (auto-curar lock órfão sob prova de dono-morto) → SP-T2 (a flag limita a vida do lock = a "prova de dono-morto" operacional) + SP-T3(a) (mecanismo: expirado → destrava rc=1 → rc=7) + composição com ADR-019 (D6, cap fim-a-fim LOG-ado)
- `brief:S1` (MTTR: folder retoma sozinho no caso benigno) → SP-T2 + SP-T3(a) cobre o MECANISMO (expiração destrava o rc=1 e desemboca no rc=7); a recuperação fim-a-fim (rc=7→ADR-019→sucesso) é COMPOSTA e coberta pelos testes próprios do ADR-019 + observação viva do `debug`. NARROWING consciente: o critério verificável de S1 é coberto por (teste rápido do mecanismo SP-T3a) + (cobertura pré-existente do gate ADR-019) + (evidência viva), não por um e2e em tempo-real através de `bisync_folder`+remote — ver §Deliberate exclusions.
- `brief:S2` (bisync concorrente genuíno segue bloqueado) → SP-T2 (acc 2) + SP-T3(b) (contraste: lock não-expirado bloqueia rc=1) via comportamento nativo (lock renovado); o caso de DOIS processos vivos colidindo não é automatizado (cap LOG-ado). Sinal de regressão: reaparecimento de `prior lock file found` com bisync vivo em produção.
- `brief:C1` (exceção restrita/fail-safe via kill-switch) → SP-T1 (knob, `0`=off) + D6 (dependência de `auto_resync_stale_listings`, fail-safe herdado do ADR-019)
- `brief:C2` (não reinventar padrão nativo) → SP-T2 (D1: flag nativa, sem liveness custom)
- `brief:C3` (custo proporcional) → SP-T1 + SP-T2 (uma flag, sem branch novo)
- `brief:N1` (não quebrar concorrência genuína) → SP-T2 (acc 2) + SP-T3(b)
- `brief:F10` (locks pré-existentes não cobertos → migração) → SP-T5
- doc-sync (invariante muda) → SP-T4
- Sem task órfã.

## Deliberate exclusions (do Brief)

- **`brief:N5`** (eliminar a GERAÇÃO de locks órfãos na origem — liberar o lock no shutdown handler /
  no kill de ADR-018) — fora de escopo, **deferral value-affirmed** armado no Brief. `--max-lock`
  cobre TODAS as fontes de morte de uma vez, tornando a mitigação preventiva não-urgente. — mantém o
  arme do Brief: `Trigger-source:` §N5 do Brief — fires when a auto-expiração (J1) estiver entregue e a
  janela `<dur>` de exposição pré-expiração ainda incomodar o suficiente.
- **`brief:N2`/`N3`/`N4`** (rc=7, rc=1 too-many-deletes, sinalização) — bare scope-boundaries de outras
  causas/famílias, já cobertas por seus próprios Specs/issues (rc7 Spec; #70; ADR-005/014). Não armados.
- Candidato "limpeza no shutdown handler" e "PID-probe custom" — rejeitados no Brief §Deliberate
  exclusions; não re-litigados aqui (D1 os herda). O PID-probe permanece o fallback contra-factual SÓ
  se o mecanismo nativo falhasse — não aplicável.
- Candidato "limpeza automática de lock never-expire pré-existente no startup" — NÃO adotado (D6):
  uma limpeza segura exigiria o PID-probe de dono-morto que D1 rejeita (o sinal de idade é inútil para
  never-expire — motivo intrínseco). `Value-rejected:` escopo desproporcional; a classe é finita
  (pré-existentes no deploy) e drenada pela migração one-time SP-T5 (NÃO pela feature — o folder travado
  não re-sincroniza até o `rm`). (Se algum dia locks never-expire pré-feature se provarem numerosos/
  persistentes, reavaliar — mas sem valor afirmado isolado hoje.)
- **Cobertura de teste fim-a-fim de S1/S2 — carried-narrowed (decisão consciente).** O critério
  verificável de S1 é coberto pela CADEIA: (i) SP-T3(a) prova o MECANISMO rápido/determinístico (lock
  expirado destrava rc=1 → rc=7); (ii) o gate ADR-019 (`_dryrun_resync_is_noop`) que recupera o rc=7 já
  tem cobertura própria nos testes do rc7 Spec; (iii) o no-op do caso benigno foi observado ao vivo no
  `debug`. NÃO se automatiza o e2e através de `bisync_folder`+remote real (exigiria um Proton remote;
  lento/frágil). S2 (dois processos vivos colidindo) não é automatizável barato — cap LOG-ado (SP-T3) +
  sinal de regressão em produção (invalidator). Por quê: esforço proporcional (C3) + precedente da
  família (o rc7 Spec validou seu mecanismo por spike/evidência viva, não por e2e). Surfaçado pela
  leitura cega independente (blind-critic J1/J3) e pelo diagnóstico `debug`; disposto ANTES do freeze.

## Risks / unknowns

- **R1 — um run legítimo com renovação ESTOLADA >`<dur>` (ex.: processo SIGSTOP-ado, ou I/O travado
  sem morrer) teria o próprio lock expirado, abrindo janela de corrida.** — mitigação: (a) ADR-001
  serializa bisync intra-daemon → uma segunda invocação do MESMO daemon nunca corre em paralelo,
  independentemente do lock; a corrida só existiria contra um SEGUNDO processo rclone (segundo daemon /
  operador manual), raro; (b) default 1h dá renovação a cada 30min — margem larga contra estol
  transitório; (c) ADR-018 mata job vivo >2h de qualquer forma. Risco residual benigno sob a
  arquitetura atual; registrado como invalidator.
- **R2 — rclone muda a semântica de `--max-lock` ou o intervalo de renovação num upgrade.** —
  mitigação: knob permite `0` (desligar → comportamento de hoje); ADR-022 registra a versão testada
  (v1.74.3) e a evidência viva de F3. Gatilho de revisão: bump major/minor de rclone.

## Assumptions & invalidators

- **O rclone renova o `.lck` a cada `max_lock`/2 enquanto o run vive (F3)** — VALIDADO ao vivo no frame
  (bisync local-local ~100s, `--max-lock 2m`, renovação observada aos 60s). Invalidado se um upgrade de
  rclone alterar isso — sinal: `[BISYNC_FAIL] prior lock file found` reaparecendo com o daemon rodando
  um bisync longo e vivo do mesmo par.
- **A proteção cross-invocation do `.lck` é largamente redundante com ADR-001 intra-daemon (brief:F4)** —
  invalidado se surgir um caso de uso multi-daemon ou de operador rodando rclone manual concorrente como
  rotina (então o default de `max_lock_seconds` mereceria subir para reduzir a janela de expiry).

## Open questions

- **open-Q1** (valor default de `<dur>`) — RESOLVIDO em D2 (`3600`s, com rationale de folga vs staleness
  ADR-005 e vs estol de renovação).
- **open-Q2** (amend do rc7 Spec vs Spec-irmã) — RESOLVIDO: Spec-irmã via `Extends (not amends)` (o
  comportamento é novo e distinto do branch de recuperação rc7; o rc7 Spec não muda).
