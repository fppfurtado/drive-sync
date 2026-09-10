# Spec: auto-expiração de lock órfão de bisync via `--max-lock` nativo

- Frozen at: 2026-09-10 (frozen after operator instruction "congele" on the rendered draft, 2026-09-10)
- Spec version: v1
- Source: Problem Brief `briefs/orphaned-bisync-lock-recovery.md` v1 (FROZEN) — tracker #88
- Status: FROZEN
- Relation: **Extends (not amends)** `specs/spec-autoresync-rc7-stale-listings.md` (v2, FROZEN) — comportamento NOVO e distinto (expiração preventiva de lock via flag nativa) sobre a MESMA superfície de montagem de cmd de `bisync_folder`. A LÓGICA do branch `rc != 0` de recuperação daquele Spec fica byte-intacta; a flag `--max-lock` é herdada pelas invocações de `--resync`/dry-run daquele branch apenas por serem montadas pelo builder base comum (D4) — o rc7 Spec NÃO é re-congelado nem re-versionado (nenhuma decisão dele muda). Ambos são braços-código da família de recovery (Brief-pai `briefs/recovery-safety-abort-bisync.md`).
- Amendments: none

> Sem PRD (rota Brief→Spec, precedente da família: feature de 1 job num daemon existente; o "what"
> vive no Brief). Traceabilidade cita o Brief por ID (`brief:J1`, `brief:S1`…) no lugar de `PR*`.

## Design

Não é product-scale — sem componente novo, data store, ou arranjo de módulos. É a adição de UMA
flag nativa do rclone (`--max-lock`) às invocações de `bisync` de um par + um knob de config +
testes. SEM subseções de Architecture/Data-model/Interfaces. Só as decisões que os open questions
do Brief exigem + o cross-cutting de doc-sync.

**Approach:** o abort `rc=1 prior lock file found` (F5 do Brief) surge de um `.lck` cujo processo
dono morreu sem liberá-lo; sem `--max-lock`, o rclone grava expiração efetivamente infinita (F3), então
o folder trava indefinidamente. A correção é passar `--max-lock <dur>` em toda invocação de `bisync`
do par: o rclone RENOVA o lock a cada `<dur>`/2 enquanto o run vive (F3, observado ao vivo) — logo um
run legítimo longo segue protegido — e um lock cujo dono morreu deixa de ser renovado e expira `<dur>`
após a última renovação, permitindo que o próximo ciclo prossiga com o `.lst` intacto, SEM `--resync`.
Nenhum código de detecção de liveness custom; a auto-cura é do próprio backend. O daemon fica
intocado; a sinalização degraded (ADR-005/watchdog ADR-014) permanece como backstop para a janela
`<dur>` até a expiração e para o caso (raro) de um bisync concorrente genuíno legítimo.

### Design decisions (resolvem os open questions do Brief)

- **D1 — Mecanismo = flag nativa `--max-lock`, não recuperação reativa custom** (resolve o crivo do
  Brief). O rclone provê expiração de lock por idade + renovação-enquanto-vivo (F3). Candidatos
  hand-rolled (limpeza no shutdown handler; PID-probe) são rejeitados no Brief (§Deliberate exclusions):
  o shutdown handler cobre só morte graciosa; o PID-probe reinventa a liveness que o backend já faz e
  domina só se `--max-lock` falhasse o teste de F3 — que passou. Preserva C2 (não reinventar padrão nativo).

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

- **D5 — Sem código de auto-cura no daemon; a expiração é do backend.** Diferente do rc7 (que precisa
  de lógica gated em `bisync_folder`), aqui não há branch novo de recuperação: a flag muda o
  comportamento do lock e o fluxo normal de retry (watcher + periodic full-sync) já re-tenta o par a
  cada ciclo. A primeira tentativa após a expiração simplesmente sucede. Menor superfície de código.

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

- **SP-T3**: teste de regressão. Cobre S1 no seu núcleo COMPORTAMENTAL de forma rápida e determinística
  (sem esperar 2min de rclone vivo): plantar um `.lck` para o par com `TimeRenewed`/mtime mais VELHO que
  `max_lock_seconds`, rodar `bisync_folder` com o knob>0, e assertar que o rclone trata o lock como
  expirado e o bisync PROSSEGUE (sem abort `rc=1 prior lock file found`) — este é o caminho de auto-cura
  de J1/S1. Mais os testes de superfície: montagem do cmd e validação de config. — serves `brief:S1`,
  `brief:S2` — acceptance: pytest cobre
  (a) com um `.lck` de idade > `max_lock_seconds` plantado, `bisync_folder` (knob>0) prossegue sem
  `rc=1 prior lock file found` [núcleo comportamental de S1];
  (b) cmd inclui `--max-lock 3600s` no default e omite `--max-lock` com knob=0;
  (c) `load_config` rejeita `1..119` e aceita `0`/`>=120`.
  **Cap de teste (LOG explícito, no ADR — SP-T4):** (i) a RENOVAÇÃO-enquanto-vivo (o pilar de que run
  legítimo longo não se auto-expira) foi validada ao vivo no frame (F3, bisync local-local ~100s) e NÃO
  é re-testada em pytest (exigiria um rclone vivo >2min, lento/flaky); (ii) o bloqueio de um SEGUNDO
  processo concorrente genuíno (S2) NÃO é coberto por teste automatizado (exigiria dois rclone vivos
  colidindo) — repousa no comportamento nativo do rclone + F3; o sinal de regressão para ambos é o
  reaparecimento de `[BISYNC_FAIL] prior lock file found` com um bisync vivo em produção (invalidator).
  — depends on: SP-T1, SP-T2

- **SP-T4**: doc-sync + ADR (obrigatório: muda o comportamento do invariante documentado). — (a) novo
  `docs/decisions/ADR-022-orphaned-lock-auto-expira-via-max-lock.md` (decisão + a evidência viva de F3 +
  o cap do teste de SP-T3 + que o kill de ADR-018 é fonte de lock órfão agora coberta); (b) nota mínima
  no invariante `bisync errors do NOT auto-recover` em `CLAUDE.md` apontando o ADR-022 (o lock órfão
  auto-expira; distinto do auto-resync rc=7); (c) atualizar `docs/operations/playbook-bisync-recovery.md`
  notando que o `rm` manual do `.lck` órfão agora é fallback (o caso comum auto-expira em `<dur>`). —
  serves doc-sync — acceptance: os 3 docs referenciam a behavior; `prior lock file found` aparece no
  playbook com o ponteiro para `max_lock_seconds`. — depends on: SP-T2

## Coverage check (cada item in-scope → ≥1 task)

- `brief:J1` (auto-curar lock órfão sob prova de dono-morto) → SP-T2 (a expiração-por-não-renovação É a prova) + SP-T3(a) (regressão comportamental: lock velho → prossegue)
- `brief:S1` (MTTR: folder retoma sozinho dentro da janela) → SP-T2 (acc 1) + **SP-T3(a) cobre o núcleo comportamental** (lock expirado → bisync prossegue); a parte "renovação-enquanto-vivo não auto-expira run legítimo" fica no cap de teste, coberta por F3 (frame, ao vivo). NARROWING consciente: o critério verificável de S1 é coberto por teste rápido comportamental (SP-T3a) + evidência viva do frame (F3), não por um teste de expiração em tempo-real — ver §Deliberate exclusions.
- `brief:S2` (bisync concorrente genuíno segue bloqueado) → SP-T2 (acc 2) via comportamento nativo do rclone (lock renovado) + F3; **NÃO coberto por teste automatizado** (exigiria dois rclone vivos colidindo) — cap LOG-ado em SP-T3(ii) + ADR. Sinal de regressão: reaparecimento de `prior lock file found` em produção.
- `brief:C1` (exceção restrita/fail-safe via kill-switch) → SP-T1 (knob, `0`=off)
- `brief:C2` (não reinventar padrão nativo) → SP-T2 (D1: flag nativa, sem liveness custom)
- `brief:C3` (custo proporcional) → SP-T1 + SP-T2 (uma flag, sem branch novo)
- `brief:N1` (não quebrar concorrência genuína) → SP-T2 (acceptance 2)
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
  se F3 tivesse falhado — não aplicável (F3 verde).
- **Cobertura de teste em tempo-real de S1/S2 — carried-narrowed (decisão consciente).** O critério
  verificável de S1 é coberto por um teste comportamental RÁPIDO (SP-T3a: lock de idade>janela →
  bisync prossegue) + a evidência viva do frame (F3), NÃO por um teste de expiração/renovação em
  tempo-real (>2min de rclone vivo, lento/flaky em CI). S2 (dois processos concorrentes) não é
  automatizável barato e repousa no comportamento nativo + F3, com cap LOG-ado (SP-T3) e sinal de
  regressão em produção (invalidator). Por quê: esforço proporcional (C3) + precedente da família (o
  rc7 Spec validou seu mecanismo por spike, não por teste de tempo-real). Surfaçado pela leitura cega
  independente (blind-critic J1/J3) e disposto ANTES do freeze.

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
