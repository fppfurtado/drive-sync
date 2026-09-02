<!--
Spec — drive-sync #35 (+ #46). Consome o Problem Brief FROZEN briefs/infra-flakiness-vs-real-failure.md.
PRD pulado (anti-ceremony): os S1-S4 do Brief já são os requisitos verificáveis; SP* citam J*/S* do Brief.
Draft-never-freeze aplica.
-->

# Spec: classificador context-aware de flakiness-transitória-vs-falha-real do provedor

- Frozen at: 2026-08-27 (v1); v2 frozen 2026-09-02 (frozen after operator approval — "Freeze v2 + proceed to Spec/build", 2026-09-02)
- Spec version: v2
- Source: Problem Brief `briefs/infra-flakiness-vs-real-failure.md` **v2** (PRD pulado — ver cabeçalho)
- Status: FROZEN
- Amendments:
  - 2026-09-02 (#79) — **gatilho signature-gated + precedência de divergência-genuína.** Consome Brief v2 (J5/S5/S6/F7/F8). O gatilho da classe transitória (`proton_infra`) deixa de exigir um par `_AUTH_CODES` e passa a disparar sob storm ativo para o **conjunto de assinaturas de infra-do-provedor** (5xx endpoint-agnóstico — remove o gate `_AUTH_ENDPOINT_RE` no caminho de storm; + `400 Code=2003`), fechando a meia-entrega do F5 (F7). **Precedência de segurança (S6):** o classificador **defere** (retorna None) quando o stderr também casa uma assinatura de **divergência-genuína** (`_is_too_many_deletes` / case-duplicates), para que um 5xx coincidente NÃO mascare um rc=1/rc=7-case-dup — este cai no handler próprio (#52/ADR-011/ADR-019). Adiciona SP-T8..SP-T11. Direção endurecida por um blind-critic (rejeitou o gatilho amplo "qualquer rc≠0 sob storm"). Aprovado pelo operador.
  - 2026-08-27 (#61) — SP-T7/J4 des-deferido e **mecanismo revisto**. O SP-T1 (spike) surfaçou
    que "zerar o campo no rclone.conf" corre com o rclone reescrevendo tokens no mesmo arquivo
    (o defer). Mecanismo trocado para **forçar `--protondrive-2fa ""` (explícito-vazio) em toda
    invocação rclone via `_base_cmd()`** — sobrescreve o campo estático SEM jamais escrever o
    rclone.conf (zero corrida). EARS de SP-T7 reescrito de acordo. Recovery de 2FA genuíno passa a
    reauth interativo (ADR-017, emenda o playbook do ADR-003). Aprovado pelo operador (opção B).

## Design

### Architecture
Hoje `_classify_rclone_stderr(stderr)` é uma **função pura por-chamada** (`sync_engine.py`), invocada
em `_run` sob o lock serializado (ADR-001). Ela mapeia um par `(code,status)` → `kind` sem memória.
A mudança introduz **memória de curta-duração** para detectar co-ocorrência com storm de 5xx.

- **Estado de janela na instância `RcloneEngine`** (um `deque[float]` de timestamps monotônicos de
  ocorrências 5xx recentes) — because o `RcloneEngine` já é o dono natural do ciclo de vida das chamadas
  rclone e `_run` é serializado (append/leitura sem lock extra) — **over** estado module-level (pior
  testabilidade) e **over** persistência em disco (a janela é efêmera por design; C3 — sem auto-resume
  cego que persista entre restarts). A classificação vira **método** de `RcloneEngine` (lê `self._infra_window`).
- **Detecção de 5xx endpoint-agnóstica** unifica #35 (auth `/auth/v4`) e #46 (block `/storage/blocks`):
  todo `_run` que retorna rc≠0 escaneia o stderr por TODAS as ocorrências `Status=5\d\d` (não só a
  primeira — rclone acumula retries internos) e registra na janela — **over** dois matchers paralelos
  por-endpoint (a duplicata que o escopo unificado-limitado existe para evitar).
- **Nova classe `proton_infra`** no eixo de `kind`, distinta das credenciais-genuínas. Semântica de
  recuperação divergente: sinaliza degraded SEM instruir reauth, e é **auto-resumível de forma gated**
  (via o `_auth_probe_loop` existente, hoje inerte enquanto degraded) — **over** manter o "sem
  auto-resume em nenhum caso" do ADR-003 para esta classe (a mudança que exige emenda de ADR — SP-T6).

### Interfaces / contracts
- `RcloneEngine._infra_window: deque[float]` — timestamps monotônicos; janela = últimos `window_seconds`.
- `RcloneEngine._record_infra_signals(stderr: str) -> None` — chamado em `_run` no caminho rc≠0; append de cada 5xx observado.
- `RcloneEngine._classify_rclone_stderr(self, stderr: str) -> AuthDegradedError | None` — vira método; em match de par auth, consulta `self._infra_window`: se `count(window) >= storm_threshold` → `kind="proton_infra"`; senão o kind de credencial atual.
- `AuthDegradedError.kind` ganha o valor `"proton_infra"`; `.recovery` (novo, opcional) ou o daemon deriva a mensagem por kind.
- Config (novo, opt-out com defaults): `rclone.infra_storm_threshold` (default a calibrar — Q-B), `rclone.infra_window_seconds` (default a calibrar).

### Tech choices
- `deque(maxlen=...)` + `time.monotonic()` — because alinhado com o dual-clock de ADR-004/007 (monotonic imune a ajuste de wall-clock/suspend) e zero-dependência — **over** lista + poda manual (mais código) — (model-prior).

### Architecture — v2 addendum (#79): storm-collateral signature set + genuine-divergence precedence
Sem nova arquitetura — **estende** o classificador existente (C2). Duas mudanças no caminho de classificação de `_run`:
- **Conjunto de assinaturas de storm-colateral** (o que faz `proton_infra` disparar sob storm ativo, além do par `_AUTH_CODES`): (a) **qualquer `Status=5xx`** — remove-se o gate `_AUTH_ENDPOINT_RE` NO CAMINHO DE STORM, então 5xx de `/storage/blocks` (#46/F5) e 5xx cru de `/auth/v4` sem par `_AUTH_CODES` passam a disparar; (b) **`400 Code=2003`** "Upload file empty" (F8). O par `_AUTH_CODES` isolado-sem-storm mantém o comportamento v1 (credencial-genuína).
- **Precedência de divergência-genuína (S6 — a trava do blind-critic).** O gatilho de storm-colateral é **subordinado** aos discriminadores de divergência-genuína já existentes: se o stderr casa `_is_too_many_deletes` (rc=1) ou a assinatura de case-duplicates (rc=7-case-dup), o classificador **retorna None** (não reclassifica para `proton_infra`) MESMO com storm ativo — deixando o erro alcançar seu handler próprio em `bisync_folder` (#52/ADR-011) ou o auto-resync gated (ADR-019, rc=7 stale-listings segue caminho próprio). Racional: co-ocorrência de 5xx ≠ causação; um 5xx mid-stream pode coincidir com uma divergência real, e mascará-la como transitória (pausa+auto-resume) esconderia perda-de-dado/malformação. O probe do SP-T5 é auth-shaped e NÃO desmascararia uma divergência bisync — por isso a exclusão é **por construção** (o classificador defere), não por guarda pós-fato.

### Cross-cutting
- **Observabilidade:** log tagueado `[PROTON_INFRA]` (uniforme com `[AUTH_DEGRADED]`/`[FOLDER_DEGRADED]`, ADR-012); STATUS `degraded: proton_infra — aguardando recuperação do provedor (sem reauth)`. **v2:** a mensagem/tag não muda com o conjunto ampliado de assinaturas — o operador vê o mesmo `proton_infra` (a causa-raiz é a mesma: provedor doente).
- **Error handling / falso-positivo:** uma falha de credencial GENUÍNA coincidindo com 5xx incidental seria mascarada como `proton_infra`. Mitigação: (1) exige um STORM real (`>= threshold` numa janela), não um 5xx isolado; (2) **escalada** — se o probe de auto-resume falhar `M` vezes com o provedor já saudável (janela de 5xx esvaziada mas auth ainda falhando), reclassifica para credencial-genuína e re-sinaliza (SP-T5).
- **Auth genuína preservada:** kinds `invalid_credentials`/`captcha_required`/`refresh_token_invalid`/`rate_limited` mantêm comportamento atual (pausa + sem auto-resume) quando SEM storm.

## Task plan
- SP-T1 (spike): resolver Q-A — o backend protondrive exige o campo `2fa` do rclone.conf em TODO cold reauth? — resolves: risco do braço J4 — done when: finding registrado (experimento/leitura do rclone) determinando se limpar o campo é seguro. depends on: —
- SP-T2: janela de 5xx em `RcloneEngine` (`_infra_window` + `_record_infra_signals`, detecção endpoint-agnóstica auth+block). — implements `infra-flakiness-vs-real-failure:J1` — acceptance (EARS): "WHEN um `_run` retorna rc≠0 com N ocorrências `Status=5xx` no stderr, the system SHALL registrar N timestamps na janela." — depends on: —
- SP-T3: classificação context-aware → `proton_infra` quando par auth co-ocorre com storm ≥ threshold; par isolado mantém kind de credencial. — implements `:J1` (S1) — acceptance (EARS): "IF um par auth casa E `count(janela) >= threshold`, THEN the system SHALL classificar `kind=proton_infra`; IF a janela está abaixo do threshold, THEN SHALL manter o kind de credencial." — depends on: SP-T2
- SP-T4: roteamento/mensagem de recuperação por kind — `proton_infra` sinaliza degraded SEM instruir reauth; kinds genuínos inalterados. — implements `:J2` (S2) — acceptance (EARS): "WHEN o daemon entra em degraded com `kind=proton_infra`, the system SHALL emitir STATUS/notify que NÃO instrui refazer auth/2FA." — depends on: SP-T3
- SP-T5: auto-resume gated de `proton_infra` via `_auth_probe_loop` (probe OK → resume; kinds genuínos seguem pausados; escalada para genuíno após M falhas com provedor saudável). — implements `:J2`/`:J3` (S3) — acceptance (EARS): "WHILE degraded com `kind=proton_infra`, WHEN um auth-probe leve tem sucesso, the system SHALL retomar os workers sem restart manual; WHILE degraded com kind genuíno, the system SHALL permanecer pausado." — depends on: SP-T4
- SP-T6: ADR emendando ADR-003 (classe `proton_infra` + auto-resume gated + precedência de STATUS). — implements decisão arquitetural de SP-T5 — acceptance (EARS): "WHEN o ADR é escrito, the system SHALL registrar a exceção ao invariante 'sem auto-resume' restrita a `proton_infra` e sua justificativa." — depends on: SP-T5
- SP-T7 (gated em SP-T1; **emendado #61**): fix do landmine — forçar `--protondrive-2fa ""` em toda invocação rclone, tornando qualquer `2fa` estático persistido inerte SEM escrever o rclone.conf. — implements `:J4` (S4) — acceptance (EARS): "WHEN o drive-sync invoca o rclone (qualquer subcomando que toca o backend protondrive), the system SHALL passar `--protondrive-2fa` com valor de string vazia, E o drive-sync SHALL NÃO escrever o `rclone.conf` para manipular o campo `2fa`." — depends on: SP-T1
  - Nota do finding SP-T1 (corrige a premissa do EARS original): o backend **exige** `2fa` no cold reauth com 2FA-enabled, MAS um valor **estático** nunca satisfaz esse requisito na prática (TOTP ~30s expira antes do cold reauth). Logo forçá-lo vazio não perde função real e converte o `8002` enganoso num erro claro "2FA required".

### v2 (#79) — signature-gated trigger + genuine-divergence precedence
- SP-T8: estender o conjunto de sinais de storm gravados por `_record_infra_signals` para incluir `400 Code=2003` "Upload file empty" (além de `Status=5xx`). — implements `:J5` (S5)/F8 — acceptance (EARS): "WHEN um `_run` retorna rc≠0 cujo stderr contém `Code=2003` com `Status=400` numa mensagem de upload de bloco vazio, the system SHALL registrar um timestamp na janela de storm (como faz para `Status=5xx`)." — depends on: SP-T2 (estende o mesmo recorder).
- SP-T9: gatilho signature-gated — `_classify_rclone_stderr` classifica `proton_infra` quando `_infra_storm_active()` E o stderr casa uma **assinatura de storm-colateral** (`Status=5xx` de QUALQUER endpoint — sem o gate `_AUTH_ENDPOINT_RE` neste caminho — OU `400 Code=2003`), MESMO sem par `_AUTH_CODES`; um par `_AUTH_CODES` isolado-sem-storm mantém o kind de credencial. — implements `:J5` (S5) — acceptance (EARS): "IF `_infra_storm_active()` E o stderr casa uma assinatura de storm-colateral (5xx qualquer-endpoint OU `2003`) E NÃO casa uma assinatura de divergência-genuína, THEN the system SHALL classificar `kind=proton_infra`; IF a mesma assinatura ocorre SEM storm ativo, THEN SHALL NÃO classificar `proton_infra` (comportamento v1)." — depends on: SP-T3, SP-T8.
- SP-T10: precedência de divergência-genuína (trava de segurança S6) — `_classify_rclone_stderr` **retorna None** (defere) quando o stderr casa `_is_too_many_deletes` (rc=1), mesmo com storm ativo e 5xx presente, deixando o erro alcançar o handler de `bisync_folder` (o advice `[BISYNC_SAFETY_ABORT]` da linha ~558). **Racional de localização:** `_run` levanta a `AuthDegradedError` INTERNAMENTE (linha ~352) antes de retornar a `bisync_folder`; logo a deferência PRECISA viver dentro do classificador — se ele retornasse proton_infra, o raise pularia o branch rc≠0 do `bisync_folder` e suprimiria o advice too-many-deletes. — implements `:S6` — acceptance (EARS): "IF o stderr casa `_is_too_many_deletes` E `_infra_storm_active()` é verdadeiro E há `Status=5xx` no mesmo stderr, THEN the system SHALL retornar None (NÃO `proton_infra`)." — depends on: SP-T9.
- SP-T11: emenda do ADR-016 (registra o gatilho ampliado de par-`_AUTH_CODES` para conjunto-de-assinaturas + a precedência de divergência-genuína S6). — implements a decisão arquitetural de SP-T9/SP-T10 — acceptance (EARS): "WHEN o ADR-016 é emendado, the system SHALL registrar que o gatilho de `proton_infra` passou a disparar por assinatura-de-storm (não só par auth) sob storm, E que assinaturas de divergência-genuína têm precedência (deferem a classificação)." — depends on: SP-T10.

## Coverage check (every in-scope J*/S* -> ≥1 task)
- J1 (S1) -> SP-T2, SP-T3
- J2 (S2) -> SP-T4, SP-T5
- J3 (S3) -> SP-T5
- J4 (S4) -> SP-T7 (gated em SP-T1)
- J5 (S5) -> SP-T8, SP-T9  *(v2, #79)*
- S6 (segurança) -> SP-T10  *(v2, #79)*
- F8 (assinatura 2003) -> SP-T8  *(v2, #79)*
- (#46 unificação) -> SP-T2 (janela endpoint-agnóstica) + **SP-T9** (v2: o GATILHO, não só a janela, passa a ser endpoint-agnóstico — fecha o F7)

## Deliberate exclusions (from the Brief)
- **S6 braço case-duplicates (rc=7) — carried-narrowed (v2, #79).** O Brief S6 nomeia rc=1 too-many-deletes E rc=7 case-duplicates como divergências-genuínas a não-mascarar. Apenas o braço rc=1 é implementável aqui: `_is_too_many_deletes` existe; **NÃO há discriminador runtime de case-duplicates** (ADR-011 detecta case-dup em config-time; o classificador rc=7 runtime é o issue **aberto #38**, não-construído — inline seria scope-creep nele). Residual aceito: um rc=7 case-dup COM 5xx coincidente durante storm pode ser temporariamente classificado `proton_infra` (mascaramento benigno-temporário — reaparece pós-storm, sem perda de dado; proton_infra não roda `--force`/`--resync`). `Trigger-source: #38 (classificador rc=7 case-duplicates runtime) — fires when #38 for construído; então o braço case-dup do S6 vira uma cláusula de deferência análoga ao SP-T10.`
- Família rc=7/rc=1 (N3 do Brief) — Value-rejected aqui / Scope-boundary: mecanismo distinto (estado bisync), coberto por `recovery-safety-abort-bisync.md`.
- Detecção via status page (N2) — Value-rejected: soberania/portabilidade (#59).
- Substituir rclone/backend (N1) — Scope-boundary: substrato.

## Risks / unknowns
- Q-A (backend exige `2fa` em cold reauth?) — mitigation: SP-T1 (spike) ANTES de comprometer SP-T7; J4/S4 é o único braço bloqueado, o núcleo (J1-J3) não depende.
- Q-B (calibração de threshold/janela) — mitigation: default conservador derivado dos 2 incidentes (2026-06-23: ~16× 500 numa janela; 2026-08-26: storm ~1.5h) + tunável por config; refinar com dados.
- Falso-positivo credencial-genuína-sob-5xx — mitigation: exige storm real + escalada por M falhas de probe (SP-T5 cross-cutting).

## Assumptions & invalidators
- Aposta: os dois `8002` observados foram pós-storm; um `8002` de credencial genuína raramente vem precedido de storm de 5xx. — invalidated if: aparecer um incidente de credencial genuína (senha/2FA realmente trocada) COM storm 5xx concorrente → a janela mascararia; sinal barato: o probe de auto-resume falhar persistentemente com o provedor saudável (a escalada de SP-T5 pega isso).

## Open questions
- Q-A e Q-B acima — Q-A resolvida por SP-T1 (spike) no build; Q-B calibrada no build com default seguro. Nenhuma bloqueia o freeze do Spec (o núcleo J1-J3 é totalmente especificado; J4 está isolado atrás do gate SP-T1).
