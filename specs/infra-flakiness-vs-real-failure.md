<!--
Spec — drive-sync #35 (+ #46). Consome o Problem Brief FROZEN briefs/infra-flakiness-vs-real-failure.md.
PRD pulado (anti-ceremony): os S1-S4 do Brief já são os requisitos verificáveis; SP* citam J*/S* do Brief.
Draft-never-freeze aplica.
-->

# Spec: classificador context-aware de flakiness-transitória-vs-falha-real do provedor

- Frozen at: 2026-08-27 (frozen after operator approval — "congelar" instruction, 2026-08-27)
- Spec version: v1
- Source: Problem Brief `briefs/infra-flakiness-vs-real-failure.md` v1 (PRD pulado — ver cabeçalho)
- Status: FROZEN
- Amendments: none

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

### Cross-cutting
- **Observabilidade:** log tagueado `[PROTON_INFRA]` (uniforme com `[AUTH_DEGRADED]`/`[FOLDER_DEGRADED]`, ADR-012); STATUS `degraded: proton_infra — aguardando recuperação do provedor (sem reauth)`.
- **Error handling / falso-positivo:** uma falha de credencial GENUÍNA coincidindo com 5xx incidental seria mascarada como `proton_infra`. Mitigação: (1) exige um STORM real (`>= threshold` numa janela), não um 5xx isolado; (2) **escalada** — se o probe de auto-resume falhar `M` vezes com o provedor já saudável (janela de 5xx esvaziada mas auth ainda falhando), reclassifica para credencial-genuína e re-sinaliza (SP-T5).
- **Auth genuína preservada:** kinds `invalid_credentials`/`captcha_required`/`refresh_token_invalid`/`rate_limited` mantêm comportamento atual (pausa + sem auto-resume) quando SEM storm.

## Task plan
- SP-T1 (spike): resolver Q-A — o backend protondrive exige o campo `2fa` do rclone.conf em TODO cold reauth? — resolves: risco do braço J4 — done when: finding registrado (experimento/leitura do rclone) determinando se limpar o campo é seguro. depends on: —
- SP-T2: janela de 5xx em `RcloneEngine` (`_infra_window` + `_record_infra_signals`, detecção endpoint-agnóstica auth+block). — implements `infra-flakiness-vs-real-failure:J1` — acceptance (EARS): "WHEN um `_run` retorna rc≠0 com N ocorrências `Status=5xx` no stderr, the system SHALL registrar N timestamps na janela." — depends on: —
- SP-T3: classificação context-aware → `proton_infra` quando par auth co-ocorre com storm ≥ threshold; par isolado mantém kind de credencial. — implements `:J1` (S1) — acceptance (EARS): "IF um par auth casa E `count(janela) >= threshold`, THEN the system SHALL classificar `kind=proton_infra`; IF a janela está abaixo do threshold, THEN SHALL manter o kind de credencial." — depends on: SP-T2
- SP-T4: roteamento/mensagem de recuperação por kind — `proton_infra` sinaliza degraded SEM instruir reauth; kinds genuínos inalterados. — implements `:J2` (S2) — acceptance (EARS): "WHEN o daemon entra em degraded com `kind=proton_infra`, the system SHALL emitir STATUS/notify que NÃO instrui refazer auth/2FA." — depends on: SP-T3
- SP-T5: auto-resume gated de `proton_infra` via `_auth_probe_loop` (probe OK → resume; kinds genuínos seguem pausados; escalada para genuíno após M falhas com provedor saudável). — implements `:J2`/`:J3` (S3) — acceptance (EARS): "WHILE degraded com `kind=proton_infra`, WHEN um auth-probe leve tem sucesso, the system SHALL retomar os workers sem restart manual; WHILE degraded com kind genuíno, the system SHALL permanecer pausado." — depends on: SP-T4
- SP-T6: ADR emendando ADR-003 (classe `proton_infra` + auto-resume gated + precedência de STATUS). — implements decisão arquitetural de SP-T5 — acceptance (EARS): "WHEN o ADR é escrito, the system SHALL registrar a exceção ao invariante 'sem auto-resume' restrita a `proton_infra` e sua justificativa." — depends on: SP-T5
- SP-T7 (gated em SP-T1): fix do landmine — limpar/zerar o campo `2fa` estático pós-auth bem-sucedida, SE SP-T1 confirmar seguro. — implements `:J4` (S4) — acceptance (EARS): "IF SP-T1 confirmou que o backend NÃO exige `2fa` em cold reauth, THEN WHEN uma auth tem sucesso, the system SHALL zerar o campo `2fa` do rclone.conf." — depends on: SP-T1

## Coverage check (every in-scope J*/S* -> ≥1 task)
- J1 (S1) -> SP-T2, SP-T3
- J2 (S2) -> SP-T4, SP-T5
- J3 (S3) -> SP-T5
- J4 (S4) -> SP-T7 (gated em SP-T1)
- (#46 unificação) -> SP-T2 (detecção endpoint-agnóstica)

## Deliberate exclusions (from the Brief)
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
