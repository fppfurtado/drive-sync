# ADR-001: Serializar chamadas rclone

**Data:** 2026-05-07
**Status:** Aceito

## Origem

- **Investigação:** diagnóstico de race condition no token refresh do backend protondrive isolado por `/debug` em 07/mai 2026; concordância upstream com [rclone#7381](https://github.com/rclone/rclone/issues/7381).
- **Plano de execução:** [docs/plans/serialize-rclone-calls.md](../plans/serialize-rclone-calls.md) (commit `69e54ec`).

## Contexto

Race condition no token refresh do backend protondrive do rclone quando ≥2 instâncias init-ializam o backend simultaneamente. A cada 4-6 dias zera `client_uid` e `client_salted_key_pass` no `rclone.conf`, exigindo reauth manual via TOTP. Mantenedor do backend (henrybear327, rclone#7381) confirmou o modo de falha; outro relator confirmou que serializar resolveu (sessão durou >1 semana após).

A configuração atual do projeto materializa o cenário vulnerável: cada job em [`drive_sync/sync_engine.py`](../../drive_sync/sync_engine.py) spawna `asyncio.create_subprocess_exec(rclone, ...)` independentemente, e o `Semaphore(max_concurrent_jobs=3)` em [`drive_sync/daemon.py:33`](../../drive_sync/daemon.py) permite até 3 instâncias rclone em paralelo. O `periodic_full_sync` (a cada 3.600s, enfileira 12 pastas) garante janelas recorrentes em que isso acontece.

A premissa original do daemon — "sync assíncrona, arquivo grande não bloqueia outros" ([CLAUDE.md](../../CLAUDE.md)) — colide com restrição externa do upstream: o backend protondrive não suporta concorrência segura.

## Decisão

Todas as invocações rclone (subprocess) feitas por [`drive_sync/sync_engine.py`](../../drive_sync/sync_engine.py) serão serializadas via `asyncio.Lock` no escopo do módulo (`_rclone_lock`), envolvendo a execução de `_run`.

Razões:
- Restrição externa (rclone protondrive não tolera concorrência) sobrescreve a premissa interna de paralelismo.
- O lock fica na fronteira correta — o subprocess rclone, não a lógica de fila/dedup do daemon.
- Workers do daemon mantêm valor para a lógica de negócio (`_inflight` set, watcher debounce); só o trabalho rclone propriamente dito vira serial.
- Mudança mínima (~5 linhas + teste); preserva API pública.

## Consequências

### Trade-offs

- Paralelismo dos workers torna-se efetivamente serial para chamadas rclone. Como todo trabalho útil do daemon é rclone, a fila de jobs vira fila de execução em vez de pool paralelo.
- Pasta grande passa a bloquear pasta pequena no front rclone — inversão da premissa documentada em CLAUDE.md. CLAUDE.md deve ser atualizado refletindo a restrição (follow-up de doc após o `/run-plan`).

### Benefícios

- Elimina a janela de race no token refresh — sessão do backend protondrive deve persistir por dias/semanas em vez de quebrar a cada 4-6 dias.
- Reauth manual via TOTP deixa de ser tarefa recorrente do operador.

### Limitações

- Não cobre rclones invocados fora do daemon (ex.: operador rodando `rclone lsd proton:` em terminal enquanto o serviço está ativo). Esse cenário continua vulnerável à mesma race; mitigação prática é não fazer isso.

## Alternativas consideradas

### `max_concurrent_jobs: 1` (config-only)

Quick-fix sem deploy de código. **Recusada** por mover o concern para o lugar errado: a fila/dedup do daemon (`_inflight`, debounce do watcher) deixaria de ter qualquer valor — todo o conceito de workers vira ilusão. O problema é restrição do rclone, não do daemon; a solução pertence à fronteira certa (subprocess rclone).

### `flock` externo no comando rclone

Cobre rclones invocados fora do daemon (operador no terminal). **Recusada** como complexidade não justificada — o daemon é o único cliente rclone do projeto na prática, e a coexistência com invocações manuais já era frágil antes desta decisão.

## Gatilhos de revisão

- **rclone#7381 resolvido upstream**: mantenedor cita migração do backend protondrive para `lib/oauthutil` como solução estrutural. Quando feito, esta ADR pode ser **Substituída**: `_rclone_lock` removido e o paralelismo restaurado.
- **Outro modo de falha de auth observado mesmo com a serialização aplicada**: indica que a race não era a causa única; reabrir investigação e elevar a prioridade do segundo item do [BACKLOG.md](../../BACKLOG.md) (health-check + pause-on-failure no daemon).

## Referências

- Issue upstream: https://github.com/rclone/rclone/issues/7381
- Plano de execução: [docs/plans/serialize-rclone-calls.md](../plans/serialize-rclone-calls.md)
- Linha do backlog: primeiro item de `## Próximos` em [BACKLOG.md](../../BACKLOG.md)
