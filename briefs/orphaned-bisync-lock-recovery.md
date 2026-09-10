<!--
Problem Brief — família de recovery do bisync (irmão de auto-resync-gated-rc7 e recovery-safety-abort).
Draft (draft-never-freeze): freeze é edição separada, só após registro de aprovação do operador.
-->

# Problem Brief: auto-recuperação data-safe de lock órfão de bisync no daemon

- Frozen at: 2026-09-10 (frozen after operator instruction "congele" on the rendered draft, 2026-09-10)
- Mode: verifiable
- Distilled from dossier: orphaned-bisync-lock-recovery (`.throughline/dossiers/`)
- Brief version: v1
- Status: FROZEN
- Amendments: none
- Parent frame: `briefs/auto-resync-gated-rc7-stale-listings.md` (v1) — adjacent. PEER na mesma família de recovery do daemon (braço de CÓDIGO que auto-cura um folder travado, gated-por-prova, preservando "nunca auto-recuperar divergência genuína"). Causa e prova de segurança DISTINTAS: lá o estado `.lst` morre e a prova é "a união `--resync` é no-op"; aqui o guard de concorrência `.lck` fica órfão e a prova é "o dono do lock está morto" (estado `.lst` intacto, sem `--resync`). Ambos descendem do pai manual `briefs/recovery-safety-abort-bisync.md`, que não cobre esta terceira causa.
- persona-input: n/a (sem superfície user-facing; comportamento interno de daemon)

## Problem statement
Um `.lck` de bisync cujo processo dono morreu sem liberá-lo trava aquele folder **indefinidamente**: o daemon aborta `rc=1 prior lock file found` a cada ciclo e permanece degradado até uma remoção manual do lock. A causa estrutural é que o rclone, sem `--max-lock`, grava o lock com expiração efetivamente infinita (never-expire) e nada no daemon o recicla — então qualquer morte não-graciosa do processo que segura o lock (restart/stop do daemon mid-bisync, kill por max-runtime de ADR-018, reboot, power-loss, OOM, kill manual) converte um estado transitório e seguro-de-limpar num travamento permanente e silencioso. O modo de falha é benigno no caso dominante (dono morto ⇒ nenhum bisync concorrente de fato ⇒ o `.lst` intacto permite bisync normal) e portanto auto-recuperável sem violar o espírito de "nunca auto-recuperar divergência genuína", desde que a morte do dono seja provada antes de agir.

## Jobs in scope
- J1: Quando um folder abortar por lock de bisync órfão **E** for provado que o dono do lock está morto (sem run concorrente genuíno), auto-curar o folder — reciclar o lock e retomar o bisync normal (baseline `.lst` intacto, SEM `--resync`) — em vez de permanecer degradado indefinidamente, reduzindo o tempo-até-recuperação de horas/manual para uma janela limitada e automática.

## Non-goals (explicitly out of scope)
- N1: Bloquear/quebrar um bisync GENUINAMENTE concorrente (segundo daemon no mesmo par, ou operador rodando `rclone bisync` manual no mesmo par enquanto o daemon roda) — o lock DEVE continuar protegendo esse caso; a auto-cura só age sob prova de dono-morto. Bare scope-boundary (é o invariante que J1 preserva, não um deferral).
- N2: Auto-recuperação de rc=7 stale-listings — já entregue (`auto-resync-gated-rc7-stale-listings`); causa e prova distintas. Bare scope-boundary.
- N3: Auto-recuperação de rc=1 too-many-deletes — outra causa do mesmo exit-code, guard-rail de divergência real mais delicado; segue o caminho da sua própria família (#70/#50). Bare scope-boundary.
- N4: Sinalização degraded (ADR-005 / watchdog ADR-014) e o playbook manual de recovery — funcionam e permanecem como backstop; nenhuma mudança de escopo aqui. Bare scope-boundary.
- N5: Eliminar a GERAÇÃO de locks órfãos na origem (ex.: fazer o shutdown handler / o kill de ADR-018 liberar o lock antes de matar o rclone) — mitigação preventiva parcial, ortogonal à auto-cura de J1 que cobre TODAS as fontes de morte de uma vez. — `Trigger-source:` este Brief (§Deliberate exclusions, candidato-2) — fires when a auto-cura de J1 estiver entregue e medir-se que a janela de exposição pré-recuperação ainda incomoda o suficiente para justificar reduzir a taxa de geração.

## Constraints
- C1: **Preservar o ESPÍRITO do invariante "bisync errors do NOT auto-recover".** A exceção é RESTRITA e fail-safe: auto-cura SÓ quando a morte do dono é provada; em qualquer ambiguidade (dono possivelmente vivo, prova inconclusiva) permanece degradado e sinaliza. Segue o precedente já aberto ao mesmo invariante por ADR-019 (rc=7 gated-por-dry-run) e ADR-016 (proton_infra gated-por-probe).
- C2: **Não reinventar mecanismo nativo.** O rclone já provê expiração de lock por idade e renova o lock enquanto o run está vivo; a solução não deve hand-rollar detecção de liveness que duplique (pior) o que o backend já faz (posture: don't reinvent established standards).
- C3: **House style + split do repo.** A mecânica (cmd/flags/lock) vive em `sync_engine`; o daemon apenas sinaliza. Esforço proporcional (o incumbente da família é ~1 flag + knob de config + testes, sem pipeline pesado). Toda auto-cura vem com kill-switch de config (padrão da família: `auto_resync_stale_listings`).

## Success criteria
- S1: Dado um `.lck` órfão (processo dono morto) com o `.lst` intacto, o daemon retoma o folder com sucesso, sem intervenção manual, dentro de uma janela limitada e conhecida. — verifiable: partindo de um lock cujo dono não existe mais, o folder volta a `bisync concluído com sucesso` automaticamente dentro da janela, e o `[FOLDER_DEGRADED]` reseta.
- S2: Um bisync genuinamente concorrente permanece bloqueado. — verifiable: com um segundo processo rclone efetivamente ativo (renovando o lock) sobre o mesmo par, a auto-cura NÃO recicla o lock e não inicia um bisync colidente.

## Key facts and provenance
- F1: A subject deste Brief é o daemon drive-sync (sync bidirecional com Proton Drive via `rclone bisync`). — basis: model-prior.
- F2: `rclone --max-lock Duration` "consider lock files older than this to be expired" — default `0` = never expire, mínimo `2m`. Disponível na versão instalada. — basis: retrieved (`rclone help flags`, rclone v1.74.3, 2026-09-10), verificado ao vivo.
- F3: Com `--max-lock <dur>`, o rclone RENOVA o lock a cada `<dur>`/2 enquanto o run está vivo, empurrando `TimeExpires` adiante; sem `--max-lock` grava `TimeExpires` efetivamente infinito (never-expire). Consequência: com `--max-lock`, um run legítimo longo permanece protegido (renova em ciclo), enquanto um lock cujo dono morreu deixa de ser renovado e expira `<dur>` após a última renovação. — basis: retrieved + **observado ao vivo** (teste local-local `rclone bisync --max-lock 2m` de ~100s, 2026-09-10: `TimeRenewed` avançou de `05:28:56`→`05:29:56` e `TimeExpires` de `05:30:56`→`05:31:56` aos 60s, exatamente `max-lock`/2; o never-expire foi observado no `.lck` do incidente 2026-09-09, `TimeExpires=2226-07-23`). Este era o teste disconfirmante nomeado da direção — rodado e VERDE.
- F4: `_run` roda sob o lock serializado `_rclone_lock` (ADR-001) — dentro de UM daemon nunca há dois bisync concorrentes. A proteção cross-invocation do `.lck` só é relevante contra um SEGUNDO processo rclone (segundo daemon, ou operador manual). — basis: retrieved (`sync_engine.py:398-470`, 2026-09-10).
- F5: `_kill_stuck_proc` (ADR-018) mata o rclone filho por SIGTERM→SIGKILL ao estourar `max_job_runtime` SEM remover o `.lck`; o restart/stop do daemon (SIGTERM do systemd ao cgroup) tampouco faz o rclone liberar o lock. Logo a geração de locks órfãos tem MÚLTIPLAS fontes internas + externas, não só o restart. — basis: retrieved (`sync_engine.py:445-462` + incidente 2026-09-09, 2026-09-10).
- F6: O bisync cmd é montado em `bisync_folder` (`sync_engine.py:594`) como `_base_cmd() + ["bisync", <local>, <remote>]` (+ excludes, + `--resync` no first-run) — o ponto onde uma flag de lock entraria. — basis: retrieved (código, 2026-09-10).
- F7: Duas Specs frozen tocam `sync_engine.py` (`spec-autoresync-rc7-stale-listings.md`, `infra-flakiness-vs-real-failure.md`); o mecanismo de lock é adjacente ao branch `rc!=0` de recovery da primeira, não dentro dele — a relação Spec (amend vs sibling) é decisão da fase de solução. — basis: retrieved (`spec_surface match`, 2026-09-10).
- F8: Prior art da família: `briefs/recovery-safety-abort-bisync.md` (pai, playbook manual rc=7+rc=1), `briefs/auto-resync-gated-rc7-stale-listings.md` (braço código rc=7), issue #70 (auto-recovery rc=1 too-many-deletes). — basis: retrieved (briefs + GitHub issues, 2026-09-10).

## Deliberate exclusions (from the dossier)
- Candidato-2 do dossier (limpeza do lock no shutdown handler / no kill de ADR-018) — não adotado como direção primária: cobre só a morte GRACIOSA via SIGTERM do daemon, míope a SIGKILL/reboot/power-loss/OOM. Sobrevive como mitigação preventiva ORTOGONAL — armado em N5.
- Candidato-3 do dossier (probe de liveness custom + retry gated) — não adotado. Motivo INTRÍNSECO e load-bearing: com F3 agora VERDE, `--max-lock` (F2/F3) entrega a mesma cobertura reativa NATIVAMENTE — zero código custom, dominando por C2. (Nota de honestidade do crivo, blind-critic J6: a "fragilidade a PID-reuse" citada antes NÃO é o motivo decisivo — é uma propriedade do CARRIER trocável: trocar o sinal de liveness de PID para a idade do próprio `TimeRenewed` do `.lck` dissolveria a fragilidade sem mudar a forma reativa. Logo o candidato-3 só cairia de fato se F3 tivesse FALHADO — nesse contra-factual o runner-up seria o candidato-3 na forma TimeRenewed-carrier, NÃO o candidato-2. Como F3 passou, a poda vale pelo dominado-pelo-nativo.)
- Candidato-4 do dossier (não fazer nada / manter só recovery manual) — Value-rejected: o incidente 2026-09-09 provou travamento silencioso de ~14h+; as fontes de lock órfão (restart, kill ADR-018, suspend, `update.sh`) são rotina, não borda rara.

## Solution-space status (NOT a solution)
- Vehicle: committed (mudança de código no daemon existente; nenhuma outra via serviria os jobs).
- Settled: o teste disconfirmante de F3 (o rclone renova o `.lck` durante um bisync longo?) foi RODADO e passou (2026-09-10, F3) — a direção nativa (`--max-lock`) está sustentada sobre as reinvenções. Nada mais bloqueia o problem-space.

## Open questions carried forward
- Valor default de `<dur>` para a expiração/janela (candidato inicial 30m–1h, folgado sob os 12h de staleness da ADR-005) — a fixar na fase de solução.
- A relação com a Spec frozen do rc7 (amend da Spec existente vs Spec-irmã nova) — a classificar no software-track por scope-membership.
