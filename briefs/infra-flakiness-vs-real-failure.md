<!--
Problem Brief — drive-sync #35 (+ #46). Distilado do dossiê infra-flakiness-vs-real-failure.md.
Contrato problema→solução; garbage de trabalho (candidatos rejeitados, scratchpad, log) excluído.
-->

# Problem Brief: distinguir flakiness transitória do provedor de falha genuína (classificador de erros `(code,status)`)

- Frozen at: 2026-08-27 (v1); v2 frozen 2026-09-02 (frozen after operator approval — "Freeze v2 + proceed" instruction, 2026-09-02)
- Mode: verifiable
- Distilled from dossier: `.throughline/dossiers/infra-flakiness-vs-real-failure.md` (v1); `.throughline/dossiers/79-storm-triggered-backoff.md` (v2)
- Brief version: v2
- Status: FROZEN
- Amendments:
  - 2026-09-02 (#79) — **storm-triggered back-off on the full provider-infra signature set.** The v1 solution (ADR-016) fires the transient-infra class only when an `_AUTH_CODES` pair co-occurs with a storm; a storm surfacing as raw non-code auth-5xx, block/storage-5xx (F5's #46 case), or the new `400 Code=2003` records into the window but never triggers back-off → the daemon keeps issuing calls, each forcing a full re-sign-in (login flood + deepened rate-limit; live instances 2026-09-01 and 2026-09-02/#80). Adds J5, N4, S5, S6, F7, F8. Approved by operator (option: amend, not sibling).
- Parent frame: `briefs/recovery-safety-abort-bisync.md` — IRMÃO distinto. Ambos distinguem "falha transitória da Proton" de falha real; aquele cobre invalidação de **estado bisync** (rc=7 stale-listings / rc=1 too-many-deletes), ESTE cobre classificação de **erros `(code,status)`** (auth/block-upload). Mecanismos distintos, frames deliberadamente separados (decisão de escopo do operador, 2026-08-27).

## Problem statement
O classificador de erros do daemon é **stateless**: mapeia um único par `(code,status)` do stderr do rclone para um `kind` de falha sem consciência dos eventos que o cercam. Por isso, um erro de auth que é **colateral de uma falha transitória de infraestrutura do provedor** (outage / tempestade de 5xx) é lido como **falha genuína de credencial** — e essa má-classificação produz dois danos: (1) aponta o operador para uma ação de recuperação *perigosa* (refazer auth / martelar o provedor durante o outage, o que aprofunda o gate e gera alertas de segurança), e (2) dispara pausa global + alarme que o operador **não pode acionar** (a falha é do provedor) e que não se auto-cura quando o provedor volta.

## Jobs in scope
- J1: Distinguir, no momento da classificação, falha-transitória-de-infra-do-provedor de falha genuína de credencial/auth — como duas classes nomeadas distintas.
- J2: Rotear cada classe para a recuperação correta — transitória → aguardar (opcionalmente auto-probe gated); genuína → reauth com credencial fresca — inclusive na MENSAGEM/orientação emitida ao operador.
- J3: Absorver storms transitórios de 5xx antes de degradar — um período de flakiness do provedor não deve pausar o daemon na primeira ocorrência.
- J4: Eliminar o landmine do campo de segundo-fator estático — a re-submissão de um TOTP já expirado (guardado em config) que transforma uma morte-de-token transitória num erro de credencial espúrio na raiz.
- J5 (v2, #79): Durante um storm ativo do provedor, um erro que carrega uma **assinatura de infra-do-provedor que NÃO é um par `_AUTH_CODES`** (5xx cru em endpoint de auth, 5xx de block/storage, ou o `400 Code=2003` de upload-vazio) DEVE fazer back-off (entrar na classe transitória — pausa + resume gated), para o daemon parar de emitir chamadas que cada uma força um re-sign-in completo. Complemento de storm-response do J3 e completude da cobertura de classificação do J1 (a classe transitória hoje só dispara por par auth; esta a estende para o conjunto pleno de assinaturas de storm).

## Non-goals (explicitly out of scope)
- N1: Substituir o rclone ou o backend do provedor. — Scope-boundary: é o substrato do projeto (o provedor não tem cliente Linux nativo); nenhum job aqui está barrado de mudá-lo, mas é um problema ordens-de-magnitude maior, sem gatilho.
- N2: Detectar o outage consultando a status page do provedor. — Value-rejected: acoplaria o tool à infra de status de UM provedor, ferindo soberania/portabilidade e o princípio "sinal nunca vira silêncio" (decidido em #59).
- N3: A família de invalidação de **estado bisync** (rc=7 stale-listings / rc=1 too-many-deletes). — Scope-boundary: mecanismo distinto (estado de listagem, não erro `(code,status)`), coberto pelo Brief irmão `recovery-safety-abort-bisync.md`. **Nota v2:** o J5/S6 não traz a família rc=7/rc=1 para escopo — ao contrário, S6 garante que uma assinatura de divergência-genuína (rc=1 too-many-deletes / rc=7 case-duplicates) coincidente com storm **não** seja mascarada como transitória; ela segue roteada ao handler próprio (#52 / ADR-011 / ADR-019).
- N4 (v2, #79): "Re-sign-in anormalmente frequente" como sinal interno de 1ª classe. — Deferido: o J5 estanca o flood sem isso, e um re-sign-in bem-sucedido não gera erro (mecanismo mais pesado, menor confiança). `Trigger-source: journalctl AUTH/flood de logins recorre APESAR do back-off do J5, OU surge necessidade de visibilidade cross-device de re-sign-in — fires when qualquer um observado.`

## Constraints
- C1: As chamadas rclone são serializadas por um lock global (o backend sofre uma race de `client_uid` sob storm de 5xx — o mecanismo que mata os tokens em cache). Qualquer auto-probe/retry introduzido compete pelo mesmo lock e não pode reintroduzir concorrência.
- C2: Existe já uma máquina de classificação-e-degradação (mapa de pares `(code,status)`→`kind`, pausa global, sinalização degraded tripla via status/notify/log). A solução **estende** essa máquina — não a substitui nem duplica.
- C3: Sem acoplamento a provedor (ver N2) e sem auto-resume cego: flakiness lateral do provedor pode mascarar um problema residual, então qualquer auto-recuperação é *gated* por uma verificação, nunca automática por tempo.

## Success criteria
- S1 (J1): Dado um erro de auth cujo par `(code,status)` hoje classifica como credencial-genuína, MAS **precedido de uma janela de 5xx do provedor acima de um threshold**, o classificador emite a classe transitória (`proton_infra` ou equivalente); o MESMO par **isolado** (sem storm precedente) continua classificado como credencial-genuína. — verifiable: asserção sobre a classe retornada a partir de sequências de stderr fixtures (com e sem storm).
- S2 (J2): A classe transitória carrega recovery=aguardar e sua sinalização/mensagem ao operador **não instrui refazer auth/2FA**; a classe genuína mantém a orientação de reauth. — verifiable: asserção sobre o texto/`kind` sinalizado por classe.
- S3 (J3): Uma sequência de N erros 5xx transitórios seguida de sucesso **não** dispara a pausa global de degradação. — verifiable: asserção sobre estado do daemon após a sequência.
- S4 (J4): Após uma auth bem-sucedida, uma morte-de-token transitória subsequente **não** produz um erro de credencial espúrio por re-submissão de segundo-fator expirado. — verifiable: asserção sobre o caminho de re-init; **CONDICIONAL** à resolução da questão aberta Q-A (ver Open questions) — se o backend exigir o campo em todo cold reauth, S4 é reescrito ou J4 sai de escopo.
- S5 (J5, v2): DADA a janela de storm armada (`>= threshold` na janela), QUANDO ocorre um erro com assinatura de infra-do-provedor que **não** casa `_AUTH_CODES` (5xx cru de auth-endpoint, 5xx de block, ou `2003`), o sistema DEVE entrar na classe transitória degradada; a MESMA assinatura **sem** storm ativo mantém o comportamento atual (não pausa). — verifiable: asserção sobre a classe/estado retornado a partir de sequências de stderr fixtures (com e sem storm).
- S6 (segurança, v2): Uma assinatura de **divergência-genuína** (`rc=1` too-many-deletes, `rc=7` case-duplicates) ocorrendo **durante** um storm ativo **NÃO** é reclassificada para transitória — alcança seu handler existente (#52 / ADR-011 / ADR-019) sem máscara. — verifiable: asserção fixture (stderr de divergência-genuína + janela armada → NÃO `proton_infra`).

## Key facts and provenance
- F1: O classificador atual (`sync_engine.py`, mapa `_AUTH_CODES` de 4 pares → kind; regex endpoint-gated a `/auth/v4`) é stateless e context-free — nenhum estado de janela, nenhuma co-ocorrência com 5xx. — basis: retrieved (leitura do código, 2026-08-27).
- F2: Incidente 2026-08-26/27 (2ª reprodução): outage global do provedor (falha de datacenter) → storm de 5xx → `8002/422` em `/auth/v4` (etapa SRP de senha) classificado como `invalid_credentials` → pausa global + emails de "novo login". Recovery correto = aguardar + restart simples (tokens em cache sobreviveram; SEM reauth). — basis: retrieved (incidente tratado nesta sessão: journalctl + status page, 2026-08-27).
- F3: Incidente 2026-06-23 (1ª reprodução): ~16× `500/500` em `/auth/v4/info` mataram o `client_uid` em cache → rclone caiu no fallback e re-submeteu o campo de segundo-fator estático (TOTP expirado ~40min) → `8002/422` em `/auth/v4/2fa`. — basis: retrieved (#35 body, 2026-08-24).
- F4: Discriminante entre as direções candidatas: os dois `8002` observados vieram em endpoints diferentes (`/auth/v4` senha vs `/auth/v4/2fa` segundo-fator). Logo uma solução baseada em janela-de-storm + co-ocorrência generaliza para ambos; uma solução que só limpa o campo de segundo-fator cobre apenas o sub-caso de 2026-06-23. — basis: retrieved (comentário no #35, 2026-08-27).
- F5: Instância-irmã do MESMO problema em outro endpoint: erros `502/504` em `/storage/blocks` (upload de bloco) durante flakiness do provedor, hoje sem classe própria (candidato `_INFRA_CODES {(0,502),(0,504)}`). O frame unificado-limitado trata auth-`8002` e block-`502/504` como duas instâncias de UM classificador. — basis: retrieved (#46, incidente 2026-05-31).
- F6: O segundo-fator fresco do operador não é obtível programaticamente pelo daemon — a rota genuína (J2) sempre termina em ação manual do operador; a solução melhora o roteamento/mensagem, não automatiza o reauth genuíno. — basis: model-prior (assumido; confirmar se contestado).
- F7 (v2): O gatilho do classificador SHIPPED é **auth-endpoint-gated** (`_AUTH_ENDPOINT_RE` casa `/auth/v4` antes de qualquer lookup), então 5xx de block (`/storage/blocks`, o caso #46/F5) e 5xx cru de auth sem par `_AUTH_CODES` são **registrados na janela mas nunca disparam** a classe transitória — a intenção "unifica auth+block" do F5 está só **meio-entregue** (janela endpoint-agnóstica; gatilho não). O J5 fecha isso. — basis: retrieved (leitura de `sync_engine.py` `_classify_rclone_stderr`/`_AUTH_ENDPOINT_RE`, 2026-09-02).
- F8 (v2): Nova assinatura de storm `400 Code=2003` "Upload file empty" = upload de block cortado no meio por queda de conexão durante flakiness do provedor — distinta dos 5xx já catalogados. — basis: retrieved (comentário do #79; instâncias vivas 2026-09-01 e 2026-09-02/#80).

## Deliberate exclusions (from the dossier)
- Direção (a) "limpar campo de segundo-fator estático" como candidato PRIMÁRIO — carried-narrowed: rebaixada a braço secundário (só cobre J4/sub-caso TOTP), gated na questão Q-A; núcleo passou a ser a co-ocorrência-por-janela (cobre J1 nos dois incidentes). Registrado por F4.
- Working garbage (candidatos rejeitados, open-questions scratchpad, log turn-a-turn) — excluído por classe (declarado pelo template).

## Solution-space status (NOT a solution)
- Vehicle: committed — é mudança de código num daemon existente (brownfield, `sync_engine.py`); o veículo-software já está dado.
- Settled when: n/a (veículo não está em aberto).

## Open questions carried forward
- Q-A: O backend do provedor exige o campo de segundo-fator em TODO cold reauth? Se sim, limpar/zerar o campo (base de J4/S4) quebra o reauth automático — J4 seria reescrito ou saído de escopo. Resolver por experimento/leitura do rclone antes de comprometer o braço J4.
- Q-B: Calibração do threshold da janela de 5xx (quantos erros em qual intervalo) — herda a mesma pergunta aberta do candidato irmão #46; resolver no design com dados dos incidentes.
