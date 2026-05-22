# Plano — Cooldown por folder

## Contexto

Hoje o daemon enfileira uma sync para qualquer folder assim que o `debounce_seconds` (default 5s) vence — não há rate-limit acima dessa rajada. A safety-net global `periodic_full_sync_seconds` (default 1800s) também enfileira todas as pastas independentemente.

Para folders com custo de sync alto, isso é proibitivo. Caso real (incidente 2026-05-13/14, `tjpa/pje-2.1`): repo sob `git_mode: bundle` com `.git/objects/pack/` de GBs gera bundle multi-GB a cada job; rclone não faz delta upload de blob no backend protondrive, então cada job re-uploadar o bundle inteiro. Editor salvando arquivos durante codificação dispara um sync a cada ~debounce; safety-net periódica adiciona outro upload por hora. Custo recorrente impede uso real de bundle mode em repos grandes.

Sob [ADR-001](../decisions/ADR-001-serializar-chamadas-rclone.md), todo trabalho rclone é serial (`_rclone_lock` no sync_engine). Um job de 4h da `tjpa/pje-2.1` não só re-uploadar o bundle — também *bloqueia todas as outras pastas* na fila pelo mesmo período. Cooldown desacopla folders caros da fila serial do rclone; o ganho é duplo: economiza upload **e** libera a fila para folders normais.

Este plano introduz `cooldown_seconds` opcional por folder. Quando configurado, gate-keia **todos** os enfileiramentos da pasta (eventos do watcher + ciclos do `periodic_full_sync`), colapsando atividade numa janela longa.

**ADRs candidatos:** ADR-004 (cooldown gate-keia também o periodic_full_sync — decisão estrutural delegada via `/new-adr`).

**Linha do backlog:** watcher/daemon: `min_interval_between_syncs` (ou `cooldown_seconds`) por folder — rate-limit por pasta independente do `debounce_seconds` (que só agrupa eventos da mesma rajada) e do `periodic_full_sync_seconds` (que é safety-net global). Hoje, qualquer toque em qualquer arquivo da pasta dispara job assim que o debounce vence. Para folders com custo de sync alto (caso típico: `git_mode: bundle` em repo com `.git/objects/pack/` de GBs — cada job re-uploadar o bundle inteiro porque rclone não faz delta upload de blob), o ideal é colapsar atividade num intervalo longo parametrizável (ex.: 4h). Semântica: ao receber evento na pasta, se `now - last_sync_at < cooldown`, agendar para `last_sync_at + cooldown` em vez de disparar; eventos subsequentes dentro da janela são absorvidos. Implementação provável: estado in-memory no daemon (`_last_sync_at: dict[str, float]`), comparado antes de enfileirar. Sem persistência cross-restart inicialmente (cooldown reseta no restart — ok, gera no máximo um upload extra). Direção a validar antes de plano. Bloqueador de fato para o caso real do tjpa/pje-2.1 sob bundle mode.

## Resumo da mudança

Novo campo opcional `cooldown_seconds: int = 0` em `FolderConfig`. Default `0` = desligado (preserva semântica atual para todos os folders existentes).

**Decisões-chave** (validadas em `/triage`):

- **Gate cobre todos os enfileiramentos**, incluindo `periodic_full_sync`. Razão: sem isso, o caso `tjpa/pje-2.1` continua re-uploadando bundle a cada ciclo do periodic (default 1800s) e a feature vira meio-fix. Safety-net enfraquecida é trade-off explícito do operador ao escolher `cooldown_seconds: 14400` para a pasta.
- **Gate no consumer (daemon)**, não no producer (watcher). Estado `_last_sync_at` vive no daemon junto da fila e do `_inflight` set; periodic e watcher passam pelo mesmo caminho sem duplicar lógica em dois layers.
- **Nome do campo:** `cooldown_seconds` (não `min_interval_between_syncs`) — mais curto, idiomático com `debounce_seconds`, `periodic_full_sync_seconds`, `startup_delay_seconds` na mesma seção.
- **Sem persistência cross-restart.** `_last_sync_at` reseta no restart do daemon; pior caso é um upload extra na inicialização.

**Semântica precisa:**

1. Worker tira `folder_name` da queue.
2. Se `folder.cooldown_seconds > 0` e `now - _last_sync_at[folder_name] < cooldown_seconds`:
   - Se `folder_name not in _cooldown_scheduled`: marca como agendado, cria task asyncio que dorme `(_last_sync_at[folder_name] + cooldown_seconds) - now` e re-enfileira (a task remove do set ao re-enfileirar).
   - Se já está agendado: descarta evento silente.
   - `queue.task_done()` e segue.
3. Caso contrário: **atualiza `_last_sync_at[folder_name] = now` antes de processar** e segue para o processamento.

**Janela conta do início (from-start), não do fim.** "Cooldown 4h" significa "no máximo um sync iniciado a cada 4h". Alternativa from-finish (atualizar no `finally`) foi descartada: bundle de 4h + cooldown 4h produziria janela efetiva de 8h, surpresa para o operador. From-start também resolve o caso falha-cedo (`AuthDegradedError` em 10s não queima janela diferente do sucesso) — alinhado com `debounce_seconds` e `periodic_full_sync_seconds`, que também contam do início.

Fora de escopo:

- Persistência cross-restart de `_last_sync_at`. Adicionar se virar problema na prática (custo: 1 upload extra/restart por folder, marginal).
- Cooldown como default global (em `WatcherConfig`). Adicionar quando mais de um folder real precisar — YAGNI por enquanto, opt-in por folder cobre o caso motivador.
- Exposição via `drive-sync --status` (mostrar tempo restante de cooldown por folder). Follow-up natural se `--status` ganhar mais campos.

## Arquivos a alterar

### Bloco 1 — schema de config {reviewer: code}

- `drive_sync/config.py`:
  - Adicionar `cooldown_seconds: int = 0` em `FolderConfig` (após `debounce_seconds`, mesma família semântica).
  - Em `load_config`, parsear `int(entry.get("cooldown_seconds", 0))` e validar `>= 0` (similar ao tratamento de `debounce_seconds`).
- `config/config.yaml.example`:
  - Documentar o campo na seção `folders`, com nota sobre o trade-off com `periodic_full_sync_seconds` e o caso típico (bundle mode em repo grande). Comentário em inglês, alinhado ao tom dos demais campos no arquivo.

### Bloco 2 — gate no daemon {reviewer: code}

- `drive_sync/daemon.py`:
  - `__init__`: adicionar `self._last_sync_at: dict[str, float] = {}` e `self._cooldown_scheduled: set[str] = set()`.
  - `_worker`: após o trecho que resolve `folder` e antes do `_inflight_lock` (linhas ~163-174), inserir checagem de cooldown. Em hit, criar task via `asyncio.create_task(self._schedule_deferred_enqueue(folder.name, delay))`, `queue.task_done()` e `continue`.
  - Em miss (segue para processar), **atualizar `self._last_sync_at[folder.name] = loop.time()` antes do `_inflight_lock`** — janela conta do início. Não tocar `_last_sync_at` no `finally` (resolveria a janela em from-finish; ver `## Resumo`).
  - Novo método `_schedule_deferred_enqueue(self, folder_name: str, delay: float)`:
    1. Adicionar `folder_name` a `_cooldown_scheduled`.
    2. `await asyncio.sleep(delay)`.
    3. Remover do set e `await self.queue.put(folder_name)`.
    4. Tratamento de cancellation: se task for cancelada no shutdown, descartar silente (não re-enfileirar para queue parada).
  - Logging: `log.info("[%s] Cooldown ativo — sync diferida em %.0fs.", folder.name, delay)` no hit; `log.debug` para "já agendada" para não poluir.

### Bloco 3 — testes {reviewer: qa}

- `tests/test_daemon.py`: novo grupo `# cooldown_seconds`. Cobertura mínima:
  - **Evento dentro da janela é diferido:** com `cooldown_seconds=60` e `_last_sync_at` setado a t=now, enfileirar evento → worker não chama `_process_folder`; task diferida criada; `folder_name in _cooldown_scheduled`.
  - **Eventos subsequentes na janela são absorvidos:** segundo evento na mesma janela com folder já em `_cooldown_scheduled` → silent drop; só uma task diferida existe.
  - **Após a janela, processa normalmente:** com `_last_sync_at` antigo (now - cooldown - 1), enfileirar → `_process_folder` chamado, `_last_sync_at` atualizado.
  - **`cooldown_seconds=0` preserva comportamento atual:** evento sempre processa; `_last_sync_at` pode ou não ser populado (tolerar ambos, mas garantir que o gate não dispara).
  - **Periodic_full_sync respeita cooldown:** validar que enfileiramentos vindos do `_periodic_full_sync` passam pelo mesmo gate (efetivo via worker — não precisa de mock direto do periodic; basta verificar que dois `queue.put` consecutivos na janela produzem só um job).
  - Mock de clock: usar `asyncio.get_event_loop().time` interceptado via monkeypatch, ou injetar `_now()` como método sobreescrevível no daemon (preferir o segundo se o monkeypatch ficar feio).

### Bloco 4 — invariante operacional na documentação {reviewer: doc}

- `CLAUDE.md`, seção "Operational Invariants":
  - Adicionar bullet sobre `cooldown_seconds`: opt-in por folder; quando setado, gate-keia tanto eventos do watcher quanto ciclos do `periodic_full_sync` para aquela pasta; sem persistência cross-restart (cooldown reseta, pior caso 1 upload extra); motivação principal é `git_mode: bundle` em repo com `.git/` grande (rclone não faz delta upload de blob).

## Verificação end-to-end

- `python -m pytest tests/ -v` passa, incluindo os novos testes de cooldown.
- `python -m drive_sync --check` aceita `cooldown_seconds: 14400` em uma entry de folder do `config.yaml` real sem erro de validação; aceita ausência (default 0).
- `grep -n cooldown_seconds drive_sync/config.py drive_sync/daemon.py config/config.yaml.example CLAUDE.md` retorna ocorrências consistentes em todos os arquivos esperados.

## Verificação manual

Cenário motivador (bundle mode em repo grande): operador configura `cooldown_seconds: 14400` (4h) em folder com `git_mode: bundle` e `.git/` grande. Editar arquivos no repo em rajada de ~10 minutos, depois observar:

1. `journalctl --user -u drive-sync --since "10 min ago" | grep -E "(Cooldown ativo|Iniciando job)"` — exatamente um `Iniciando job` para a pasta na primeira janela, seguido de `Cooldown ativo — sync diferida em ~14400s` para eventos subsequentes.
2. `journalctl --user -u drive-sync --since "1h ago" | grep "Sincronização periódica"` confirma que o periodic disparou pelo menos um ciclo; mesmo assim, o `grep -c "Iniciando job.*<folder>"` na mesma janela permanece em 1 (ciclo absorvido pelo cooldown).
3. Após o fim da janela (4h), o próximo evento (ou o próximo `periodic`) dispara `Iniciando job` normalmente; `_last_sync_at` é re-armado.

Cenário de regressão (folder sem cooldown configurado): editar arquivos numa folder existente sem `cooldown_seconds` no config → comportamento idêntico ao anterior (sync a cada debounce + cada periodic). Nenhum log de "Cooldown ativo".

## Notas operacionais

- Ordem dos blocos: 1 → 2 → 3 → 4. Bloco 1 é pré-requisito de 2 (campo precisa existir antes do gate ler); bloco 3 valida 1+2 antes de doc; bloco 4 é cosmético depois dos demais.
- Atenção do reviewer (bloco 2): `_last_sync_at` é atualizado **antes** do `_inflight_lock` (from-start), não no `finally`. Ambos os usos do clock (update e cálculo do `delay` no `_schedule_deferred_enqueue`) devem usar `loop.time()`; mistura com `time.monotonic()` quebra a aritmética da janela.
- Atenção do reviewer (bloco 3): mockar clock no asyncio é frágil — preferir injeção via método `_now()` no daemon (override em subclasse de teste ou `monkeypatch.setattr`) a tentar interceptar `loop.time`.
- Composição com `_degraded` (ADR-003): tasks diferidas **não são canceladas** em entry-degraded. Quando acordam, `await self.queue.put(folder_name)` deposita na queue e o worker degraded-gate (`daemon.py:159-161`) descarta — sem retrabalho. Custo: 1 sleep + 1 put + 1 descarte por folder com cooldown ativo durante o degraded; desprezível dado o N pequeno de folders. Mantém composabilidade sem código extra.
- Follow-ups previstos (não nesse plano):
  - Persistência cross-restart de `_last_sync_at` se uploads extras pós-restart virarem incômodo real.
  - Default global em `WatcherConfig` se mais de um folder precisar.
  - Exibir tempo restante de cooldown em `drive-sync --status`.
  - Cleanup de `_last_sync_at`/`_cooldown_scheduled` em hot-reload de config (não existe hoje; só relevante quando reload for implementado).
