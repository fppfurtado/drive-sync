# ADR-016: Classe `proton_infra` (flakiness transitória) + auto-resume gated

**Data:** 2026-08-27
**Status:** Aceito

## Origem

- **Incidentes-instância (2×):** 2026-06-23 e 2026-08-26/27 — em ambos, uma **tempestade de erros
  5xx da Proona** (outage / falha de datacenter) produziu um `Code=8002` de auth que o classificador
  `_AUTH_CODES` ([ADR-003](ADR-003-type-notify-sinalizacao-degraded.md)) leu como
  `invalid_credentials` (falha genuína de credencial), disparando pausa global + orientação de
  recovery **perigosa** (reauth durante o outage aprofunda o gate e gera alertas de segurança). O
  `8002` era **colateral** do storm, não uma credencial quebrada.
- **Frame:** `briefs/infra-flakiness-vs-real-failure.md` (v1, frozen 2026-08-27). Spec
  `specs/infra-flakiness-vs-real-failure.md` (v1). Tracker **#35** (âncora) + **#46** (2ª instância:
  block-upload `502/504`).
- **Decisão base emendada:** ADR-003 — este ADR **estende** o classificador e **abre uma exceção
  restrita** ao seu invariante "sem auto-resume em nenhum caso".

## Contexto

O classificador de ADR-003 é **stateless**: mapeia um par `(code,status)` → `kind` sem consciência do
contexto temporal. Um `8002` isolado (credencial realmente trocada) e um `8002` colateral de um storm
de 5xx são indistinguíveis para ele — e a receita de recovery de `invalid_credentials`
(`rclone config update proton 2fa` + restart) é ativamente nociva no segundo caso.

## Decisão

**(1) Nova classe `proton_infra` (classificação context-aware).** `sync_engine` mantém uma janela
deslizante de timestamps monotônicos de erros `Status=5xx` (endpoint-agnóstica — unifica auth
`/auth/v4` e block `/storage/blocks`, i.e. #35 + #46). Quando um par auth casa **E** há um storm de
5xx ativo na janela (`>= infra_storm_threshold` em `infra_window_seconds`), o `kind` é reclassificado
de credencial-genuína para `proton_infra` (recovery = **aguardar**, sem reauth). Par isolado (sem
storm) mantém o kind de credencial — comportamento de ADR-003 preservado.

**(2) Exceção RESTRITA ao invariante "sem auto-resume" de ADR-003.** ADR-003 diz *"sem auto-resume em
nenhum caso — flakiness lateral pode mascarar problema residual"*. Este ADR abre **uma** exceção, e só
uma: um degraded de kind `proton_infra` é **auto-resumível de forma gated**. O `_auth_probe_loop`, que
antes apenas pulava enquanto degraded, agora — e **somente** para `proton_infra` — faz um probe leve;
um **sucesso real** (`rc==0`; um 5xx não conta como sucesso) retoma os workers sem restart manual.
**Todos os kinds de credencial genuína continuam sob o invariante original** (permanecem pausados, sem
probe, até reauth/restart manual).

**(3) Guard anti-falso-positivo (escalada).** Se um `proton_infra` degraded sofrer
`_INFRA_ESCALATE_AFTER` probes falhados **com o provedor já saudável** (janela de 5xx vazia), ele
**escala** para `auth_uncertain` (provável credencial genuína) e **permanece pausado** — cobrindo o
caso em que uma credencial genuinamente quebrada coincidiu com um storm e foi mascarada como transitória.

## Justificativa (por que gated, não cego)

O medo legítimo do ADR-003 (auto-resume cego mascarar problema residual) é endereçado pelo **gate**: o
resume exige um probe que teve sucesso REAL, e a persistência-de-falha-com-provedor-saudável escala de
volta para pausa. O resume não é por tempo nem incondicional — é evidência-baseado. A exceção fica
confinada à classe `proton_infra`, que por construção só existe quando há storm — o caso em que
"aguardar" é a recuperação correta e o restart manual é puro toil.

## Consequências

- Config novo em `RcloneConfig`: `infra_storm_threshold` (default 5), `infra_window_seconds` (default
  600) — calibração inicial derivada dos 2 incidentes; tunável.
- STATUS/notify de `proton_infra` NÃO instrui reauth (mensagem própria: "aguardar recuperação, NÃO
  refazer auth").
- Estado in-memory (janela module-level em `sync_engine`; `_degraded_kind`/`_infra_probe_failures` no
  daemon) — sem persistência cross-restart, consistente com o resto do daemon.
- **Fora de escopo (deferido):** o landmine do campo `2fa` estático (J4 do Brief) — o spike SP-T1
  concluiu que limpá-lo é seguro mas introduz corrida de escrita no `rclone.conf`; deferido a
  mini-ciclo próprio (ver `docs/spikes/SP-T1-2fa-cold-reauth.md`). A família rc=7/rc=1 (estado bisync)
  segue coberta por `briefs/recovery-safety-abort-bisync.md`, mecanismo distinto.

## Emenda 2026-09-02 (#79) — gatilho signature-gated + precedência de divergência-genuína

**Status:** Aceito. **Origem:** #79 (Brief v2 J5/S5/S6/F7/F8, Spec v2 SP-T8..SP-T11). Instâncias vivas:
2026-09-01 e 2026-09-02/#80 — storms 5xx do provedor que **não** casavam `_AUTH_CODES` (500 cru em
`/auth/v4/info`, `502 /storage/blocks`, `400 Code=2003 "Upload file empty"`) faziam o rclone re-logar a
cada chamada → flood de "new login" + rate-limit aprofundado.

**O que muda:**

1. **Gatilho ampliado de par-`_AUTH_CODES` → conjunto de assinaturas de storm (fecha F7/#46).** A
   decisão original disparava `proton_infra` só quando um **par `_AUTH_CODES`** co-ocorria com um storm
   (`_classify_rclone_stderr` era **auth-endpoint-gated** por `_AUTH_ENDPOINT_RE`). A janela de storm já
   era endpoint-agnóstica, mas o **gatilho não** — então `502 /storage/blocks` (#46) e `500` cru de auth
   eram registrados na janela e nunca agiam. Agora, sob storm ativo, um erro que carrega uma **assinatura
   de storm-colateral** (`_has_storm_signature`: qualquer `Status=5xx` endpoint-agnóstico OU
   `400 Code=2003`) vira `proton_infra` **mesmo sem par `_AUTH_CODES`**. Reusa integralmente a máquina de
   pausa + auto-resume gated por probe + escalada (nada novo no caminho de recuperação — C2).

2. **`400 Code=2003` entra no conjunto de evidência de storm** gravado por `_record_infra_signals`
   (SP-T8/F8) — bloco de upload cortado mid-stream por queda de conexão, um sintoma de storm apesar de
   ser 400.

3. **Precedência de divergência-genuína (S6, trava de segurança).** Uma assinatura de **divergência real**
   de bisync coincidente com um storm **NÃO** é mascarada como transitória: `_classify_rclone_stderr`
   **defere** (retorna `None`) quando o stderr casa `_is_too_many_deletes` (rc=1, #52), deixando o abort
   alcançar o handler `[BISYNC_SAFETY_ABORT]` do `bisync_folder`. Co-ocorrência de 5xx ≠ causação; mascarar
   suprimiria o advice de perda-de-dados. A deferência vive no classificador por necessidade (`_run`
   levanta a exceção internamente — retornar `proton_infra` pularia o branch rc≠0). **Limite:** rc=7
   case-duplicates (ADR-011) não tem discriminador runtime (é config-time; o classificador rc=7 runtime é o
   **#38** aberto) — o braço case-dup do S6 fica deferido ao #38; residual (case-dup + 5xx coincidente sob
   storm → mascaramento temporário, reaparece pós-storm, sem perda de dado) aceito.

**Preservado:** a mesma assinatura **sem** storm armado não pausa (S3); par `_AUTH_CODES` isolado-sem-storm
mantém o kind de credencial-genuína; o probe/escalada do resume (auth-shaped) é inalterado — a exclusão de
divergência-genuína é **por construção** (o classificador defere), não depende do probe para desmascarar.
