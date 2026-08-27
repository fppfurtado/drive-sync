<!--
Problem Brief — drive-sync #35 (+ #46). Distilado do dossiê infra-flakiness-vs-real-failure.md.
Contrato problema→solução; garbage de trabalho (candidatos rejeitados, scratchpad, log) excluído.
-->

# Problem Brief: distinguir flakiness transitória do provedor de falha genuína (classificador de erros `(code,status)`)

- Frozen at: 2026-08-27 (frozen after operator approval — "congelar" instruction, 2026-08-27)
- Mode: verifiable
- Distilled from dossier: `.throughline/dossiers/infra-flakiness-vs-real-failure.md`
- Brief version: v1
- Status: FROZEN
- Amendments: none
- Parent frame: `briefs/recovery-safety-abort-bisync.md` — IRMÃO distinto. Ambos distinguem "falha transitória da Proton" de falha real; aquele cobre invalidação de **estado bisync** (rc=7 stale-listings / rc=1 too-many-deletes), ESTE cobre classificação de **erros `(code,status)`** (auth/block-upload). Mecanismos distintos, frames deliberadamente separados (decisão de escopo do operador, 2026-08-27).

## Problem statement
O classificador de erros do daemon é **stateless**: mapeia um único par `(code,status)` do stderr do rclone para um `kind` de falha sem consciência dos eventos que o cercam. Por isso, um erro de auth que é **colateral de uma falha transitória de infraestrutura do provedor** (outage / tempestade de 5xx) é lido como **falha genuína de credencial** — e essa má-classificação produz dois danos: (1) aponta o operador para uma ação de recuperação *perigosa* (refazer auth / martelar o provedor durante o outage, o que aprofunda o gate e gera alertas de segurança), e (2) dispara pausa global + alarme que o operador **não pode acionar** (a falha é do provedor) e que não se auto-cura quando o provedor volta.

## Jobs in scope
- J1: Distinguir, no momento da classificação, falha-transitória-de-infra-do-provedor de falha genuína de credencial/auth — como duas classes nomeadas distintas.
- J2: Rotear cada classe para a recuperação correta — transitória → aguardar (opcionalmente auto-probe gated); genuína → reauth com credencial fresca — inclusive na MENSAGEM/orientação emitida ao operador.
- J3: Absorver storms transitórios de 5xx antes de degradar — um período de flakiness do provedor não deve pausar o daemon na primeira ocorrência.
- J4: Eliminar o landmine do campo de segundo-fator estático — a re-submissão de um TOTP já expirado (guardado em config) que transforma uma morte-de-token transitória num erro de credencial espúrio na raiz.

## Non-goals (explicitly out of scope)
- N1: Substituir o rclone ou o backend do provedor. — Scope-boundary: é o substrato do projeto (o provedor não tem cliente Linux nativo); nenhum job aqui está barrado de mudá-lo, mas é um problema ordens-de-magnitude maior, sem gatilho.
- N2: Detectar o outage consultando a status page do provedor. — Value-rejected: acoplaria o tool à infra de status de UM provedor, ferindo soberania/portabilidade e o princípio "sinal nunca vira silêncio" (decidido em #59).
- N3: A família de invalidação de **estado bisync** (rc=7 stale-listings / rc=1 too-many-deletes). — Scope-boundary: mecanismo distinto (estado de listagem, não erro `(code,status)`), coberto pelo Brief irmão `recovery-safety-abort-bisync.md`.

## Constraints
- C1: As chamadas rclone são serializadas por um lock global (o backend sofre uma race de `client_uid` sob storm de 5xx — o mecanismo que mata os tokens em cache). Qualquer auto-probe/retry introduzido compete pelo mesmo lock e não pode reintroduzir concorrência.
- C2: Existe já uma máquina de classificação-e-degradação (mapa de pares `(code,status)`→`kind`, pausa global, sinalização degraded tripla via status/notify/log). A solução **estende** essa máquina — não a substitui nem duplica.
- C3: Sem acoplamento a provedor (ver N2) e sem auto-resume cego: flakiness lateral do provedor pode mascarar um problema residual, então qualquer auto-recuperação é *gated* por uma verificação, nunca automática por tempo.

## Success criteria
- S1 (J1): Dado um erro de auth cujo par `(code,status)` hoje classifica como credencial-genuína, MAS **precedido de uma janela de 5xx do provedor acima de um threshold**, o classificador emite a classe transitória (`proton_infra` ou equivalente); o MESMO par **isolado** (sem storm precedente) continua classificado como credencial-genuína. — verifiable: asserção sobre a classe retornada a partir de sequências de stderr fixtures (com e sem storm).
- S2 (J2): A classe transitória carrega recovery=aguardar e sua sinalização/mensagem ao operador **não instrui refazer auth/2FA**; a classe genuína mantém a orientação de reauth. — verifiable: asserção sobre o texto/`kind` sinalizado por classe.
- S3 (J3): Uma sequência de N erros 5xx transitórios seguida de sucesso **não** dispara a pausa global de degradação. — verifiable: asserção sobre estado do daemon após a sequência.
- S4 (J4): Após uma auth bem-sucedida, uma morte-de-token transitória subsequente **não** produz um erro de credencial espúrio por re-submissão de segundo-fator expirado. — verifiable: asserção sobre o caminho de re-init; **CONDICIONAL** à resolução da questão aberta Q-A (ver Open questions) — se o backend exigir o campo em todo cold reauth, S4 é reescrito ou J4 sai de escopo.

## Key facts and provenance
- F1: O classificador atual (`sync_engine.py`, mapa `_AUTH_CODES` de 4 pares → kind; regex endpoint-gated a `/auth/v4`) é stateless e context-free — nenhum estado de janela, nenhuma co-ocorrência com 5xx. — basis: retrieved (leitura do código, 2026-08-27).
- F2: Incidente 2026-08-26/27 (2ª reprodução): outage global do provedor (falha de datacenter) → storm de 5xx → `8002/422` em `/auth/v4` (etapa SRP de senha) classificado como `invalid_credentials` → pausa global + emails de "novo login". Recovery correto = aguardar + restart simples (tokens em cache sobreviveram; SEM reauth). — basis: retrieved (incidente tratado nesta sessão: journalctl + status page, 2026-08-27).
- F3: Incidente 2026-06-23 (1ª reprodução): ~16× `500/500` em `/auth/v4/info` mataram o `client_uid` em cache → rclone caiu no fallback e re-submeteu o campo de segundo-fator estático (TOTP expirado ~40min) → `8002/422` em `/auth/v4/2fa`. — basis: retrieved (#35 body, 2026-08-24).
- F4: Discriminante entre as direções candidatas: os dois `8002` observados vieram em endpoints diferentes (`/auth/v4` senha vs `/auth/v4/2fa` segundo-fator). Logo uma solução baseada em janela-de-storm + co-ocorrência generaliza para ambos; uma solução que só limpa o campo de segundo-fator cobre apenas o sub-caso de 2026-06-23. — basis: retrieved (comentário no #35, 2026-08-27).
- F5: Instância-irmã do MESMO problema em outro endpoint: erros `502/504` em `/storage/blocks` (upload de bloco) durante flakiness do provedor, hoje sem classe própria (candidato `_INFRA_CODES {(0,502),(0,504)}`). O frame unificado-limitado trata auth-`8002` e block-`502/504` como duas instâncias de UM classificador. — basis: retrieved (#46, incidente 2026-05-31).
- F6: O segundo-fator fresco do operador não é obtível programaticamente pelo daemon — a rota genuína (J2) sempre termina em ação manual do operador; a solução melhora o roteamento/mensagem, não automatiza o reauth genuíno. — basis: model-prior (assumido; confirmar se contestado).

## Deliberate exclusions (from the dossier)
- Direção (a) "limpar campo de segundo-fator estático" como candidato PRIMÁRIO — carried-narrowed: rebaixada a braço secundário (só cobre J4/sub-caso TOTP), gated na questão Q-A; núcleo passou a ser a co-ocorrência-por-janela (cobre J1 nos dois incidentes). Registrado por F4.
- Working garbage (candidatos rejeitados, open-questions scratchpad, log turn-a-turn) — excluído por classe (declarado pelo template).

## Solution-space status (NOT a solution)
- Vehicle: committed — é mudança de código num daemon existente (brownfield, `sync_engine.py`); o veículo-software já está dado.
- Settled when: n/a (veículo não está em aberto).

## Open questions carried forward
- Q-A: O backend do provedor exige o campo de segundo-fator em TODO cold reauth? Se sim, limpar/zerar o campo (base de J4/S4) quebra o reauth automático — J4 seria reescrito ou saído de escopo. Resolver por experimento/leitura do rclone antes de comprometer o braço J4.
- Q-B: Calibração do threshold da janela de 5xx (quantos erros em qual intervalo) — herda a mesma pergunta aberta do candidato irmão #46; resolver no design com dados dos incidentes.
