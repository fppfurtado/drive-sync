# Plano — Serializar chamadas rclone para evitar race no token refresh

## Contexto

Causa-raiz isolada por `/debug` em 07/mai 2026: race condition no refresh do token cached do backend protondrive quando ≥2 instâncias rclone fazem init do backend simultaneamente. Disparada pelo `periodic_full_sync` (a cada 3.600s, enfileira 12 pastas; até 3 rodam em paralelo via `Semaphore(max_concurrent_jobs)`). A cada 4-6 dias zera `client_uid` e `client_salted_key_pass` no `rclone.conf`, exigindo reauth manual via TOTP. Concordância qualitativa de [rclone#7381](https://github.com/rclone/rclone/issues/7381) (mantenedor henrybear327): "if you ran more than 1 rclone instances at the same time, and the token refresh happens, the race condition will cause the refresh to fail." Outro relator do mesmo issue confirmou que serializar resolveu (sessão durou >1 semana após).

**Linha do backlog:** sync_engine: serializar chamadas rclone (asyncio.Lock module-level envolvendo `_run`) para corrigir race condition no token refresh do backend protondrive — disparada quando ≥2 instâncias rclone fazem init do backend simultaneamente (cenário recorrente na nossa config: `periodic_full_sync` enfileira 12 pastas/h e `max_concurrent_jobs=3`). A cada ~5 dias zera `client_uid` e `client_salted_key_pass` no `rclone.conf`, exigindo reauth manual via TOTP. Causa-raiz isolada por /debug em 07/mai 2026 com concordância qualitativa de [rclone#7381](https://github.com/rclone/rclone/issues/7381) (mantenedor henrybear327). Fix é ~5 linhas + teste verificando que chamadas concorrentes serializam. Trade-off aceito: paralelismo dos workers vira ilusão (todo trabalho útil é rclone), mas o lock fica na fronteira correta — subprocess rclone, não a lógica de fila/dedup do daemon.

## Resumo da mudança

Adicionar `asyncio.Lock` no escopo do módulo em `drive_sync/sync_engine.py` envolvendo a chamada a `asyncio.create_subprocess_exec` em `_run`. Toda invocação rclone passa pelo lock — serial em escala de processo. API pública (`bisync_folder`, `_run`) inalterada; nenhuma config nova exigida do operador.

Trade-off aceito: workers do daemon continuam tendo a fila/dedup/debounce úteis (uma pasta grande não bloqueia o `_inflight` set para outras pastas), mas o trabalho rclone propriamente dito vira serial. Não é regressão — é mover o lock para a fronteira correta (o backend protondrive não suporta concorrência segura, a fila do daemon nunca soube disso).

## Arquivos a alterar

### Bloco 1 — serialização do _run {reviewer: code}

- `drive_sync/sync_engine.py`: declarar `_rclone_lock = asyncio.Lock()` no escopo do módulo (logo após o logger). Envolver o corpo de `_run` (do `asyncio.create_subprocess_exec` até o `return`) com `async with _rclone_lock:`. Adicionar comentário de uma linha explicando o porquê (race no token refresh — referência a rclone#7381) — caso de comentário justificado pela convenção do CLAUDE.md (WHY não-óbvio).

### Bloco 2 — teste de serialização {reviewer: qa}

- `tests/test_sync_engine.py`: adicionar `test_run_serializes_concurrent_calls`. Dispara duas chamadas concorrentes a `_run` via `asyncio.gather`. Mock interno (substitui `asyncio.create_subprocess_exec` ou injeta `subprocess` fake) registra `start_time`/`end_time` e dorme ~50ms. Assert: `call_2.start >= call_1.end` (sem sobreposição temporal). Importa `_run` direto do módulo para testar a primitiva serializada, não passar pelo `RcloneEngine`.

## Verificação end-to-end

- `python -m pytest tests/ -v` → 71 testes passando (70 atuais + 1 novo).
- `grep -n "_rclone_lock" drive_sync/sync_engine.py` → mostra a declaração module-level e o `async with` em `_run` (≥2 ocorrências).
- `python -m drive_sync --check` → config valida sem nova chave.

## Verificação manual

A race só manifesta em janela de refresh de token (~24h) com chamadas concorrentes — não há reprodução determinística em segundos. Validação é por observação prolongada:

1. **D0 — Renovar credenciais Proton e reativar**:
   ```bash
   rclone config update proton 2fa "<TOTP fresco>" client_uid "" client_access_token "" client_refresh_token "" client_salted_key_pass ""
   rclone lsd proton:                                    # confirma auth
   rclone config show proton | grep -E "client_(uid|salted_key_pass)"   # ambos populados
   systemctl --user enable --now drive-sync
   ```
2. **D0+1h — primeira janela de paralelismo eliminada**: `journalctl --user -u drive-sync --since "1h ago" | grep -c Code=8002` → `0`. Pelo menos um `periodic_full_sync` deve ter rodado nesse intervalo.
3. **D7 — sessão se manteve**: `rclone config show proton | grep -E "client_(uid|salted_key_pass)"` ambos ainda populados; `journalctl --user -u drive-sync --since "7 days ago" | grep -c Code=8002` → `0`.
4. **D14+ — confirmação**: se a sessão se manteve >2 semanas, race está mitigada (referência: relator de rclone#7381 reportou >1 semana após serializar).

Se em qualquer ponto ressurgir `Code=8002`, abrir item separado no backlog: a serialização não foi suficiente, há outro modo de falha do backend.

## Notas operacionais

- Bloco 1 antes do Bloco 2 — código antes do teste evita teste vermelho em commits intermediários.
- Teste de concorrência é frágil sob CI lento: usar margem confortável de tempo (sleeps de 50-100ms) e tolerância de ≥10ms na asserção de não-sobreposição. Não inflar — teste irritante é teste que será desabilitado.
- `_run` hoje é privado (`_` prefix) e usado só por `RcloneEngine`. Importá-lo direto no teste é aceito (já há precedente em `test_sync_engine.py` importando `_state_marker_for`).

## Pendências de validação

A invariante (sessão Proton se mantém após serialização) só pode ser confirmada por observação prolongada. D0 já validado pelo operador (auth voltou após reativar). Falta confirmar:

- **D0+1h** — `journalctl --user -u drive-sync --since "1h ago" | grep -c Code=8002` → `0` após pelo menos um `periodic_full_sync` ter rodado.
- **D7** — `rclone config show proton | grep -E "client_(uid|salted_key_pass)"` ambos ainda populados; `journalctl --user -u drive-sync --since "7 days ago" | grep -c Code=8002` → `0`.
- **D14+** — confirmação final (referência: relator de rclone#7381 reportou >1 semana após serializar).
