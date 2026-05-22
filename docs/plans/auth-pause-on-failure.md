# Plano — Pausar daemon em falha de auth (detecção reativa + probe proativo)

## Contexto

Hoje (2026-05-11) o daemon acumulou **292 ocorrências de `Code=8002`** no endpoint `POST /api/auth/v4/2fa` ao longo de ~10h sem qualquer sinalização ao operador. O volume de retries chegou a cascatear para `Code=9001` (CAPTCHA gate da Proton) entre 16:07–16:13, derrubando temporariamente **todos os 12 jobs** — inclusive os que estavam usando credenciais até então válidas. A intervenção manual (renovação via `rclone config update proton 2fa <code>` + restart) só aconteceu porque o operador foi olhar o status por outra razão.

[ADR-001](../decisions/ADR-001-serializar-chamadas-rclone.md) já registrava como gatilho de revisão: *"outro modo de falha de auth observado mesmo com a serialização aplicada → reabrir investigação e elevar a prioridade do segundo item do BACKLOG (health-check + pause-on-failure no daemon)"*. Este plano materializa esse follow-up — não substitui a ADR-001; é defesa em profundidade complementar.

Esta é defesa em profundidade: a serialização aplicada via ADR-001 reduz a frequência da expiração induzida por race no token refresh, mas não elimina expirações naturais nem mudanças unilaterais do upstream (vide CAPTCHA novo da Proton). O pause-on-failure transforma "horas/dias até intervenção" em "segundos até sinalização".

**Linha do backlog:** daemon: health-check de auth + pause-on-failure — detectar `Code=8002`/422 em qualquer job, marcar daemon como degraded (`sd_notify` ou similar), pausar workers e disparar `notify-send`. Hoje, falha de auth produz 26 erros/10min antes de qualquer sinalização (descobri por acaso 5 dias depois). Defesa em profundidade que vale mesmo com a serialização aplicada — qualquer erro de auth futuro (regressão upstream, mudança da Proton, etc.) fica visível em segundos em vez de horas/dias. Tradeoff: `notify-send` exige sessão gráfica ativa; em headless precisaria canal alternativo (registrar como follow-up se virar caso real).

## Resumo da mudança

**Entra:**
- Classificação de erros do rclone por substring no stderr (`Code=8002`, `Code=9001`, endpoints `/api/auth/v4`) com exception sentinela `AuthDegradedError` propagada para o daemon.
- Estado `degraded` no `SyncDaemon` (`asyncio.Event`) — quando setado, workers param de processar jobs (drenam a fila marcando `task_done` sem executar), pausa **global**.
- Probe proativo de baixa frequência (default 1h, configurável) executando uma chamada rclone leve (`rclone about <remote>:`) para detectar queda de auth antes que um job real falhe.
- Sinalização tripla quando entra em degraded: `sd_notify STATUS=` (visível em `systemctl status`), `notify-send` (desktop, best-effort se houver `DISPLAY`), e log estruturado tagueado `[AUTH_DEGRADED]` em nível CRITICAL.
- Recuperação **manual**: degraded é volátil (in-memory); operador renova o token e faz `systemctl --user restart drive-sync`. Sem auto-resume (evita reativação prematura durante flakiness da Proton, vide cascateamento de CAPTCHA observado hoje).

**Fica de fora:**
- Canal alternativo para sessão headless (e-mail/webhook). Item de backlog menciona como follow-up condicional; só registrar nova linha se virar caso real.
- Estado degraded persistente em disco. Restart limpa, e é exatamente o comando de recuperação prescrito — sobreposição de design.
- Auto-resume via probe. Foi explicitamente recusado; complexidade adicional (loop de probe + backoff + condição de saída) sem ganho proporcional para um restart de ~3 segundos.

## Arquivos a alterar

### Bloco 1 — classificador de erro e exception sentinela {reviewer: code}

- `drive_sync/sync_engine.py`:
  - Novo módulo-level: classe `AuthDegradedError(RuntimeError)` carregando `kind` (`"invalid_credentials"` | `"captcha_required"`), `code` (`int`), `stderr_tail` (str).
  - Nova função pura `_classify_rclone_stderr(stderr: str) -> AuthDegradedError | None` — detecta `Code=8002` ou `Code=9001` (com `Status=422`) e o endpoint `/api/auth/v4`. Não casa em substrings parciais; usa âncora `(Code=<n>,` + `Status=422)`.
  - `_run` (já existente): após capturar `stderr_b`, se `rc != 0`, chama o classificador e levanta a exception em vez de retornar — propaga para o caller.
  - `_ensure_remote_dir`, `bisync_folder`, `upload_bundle`, `download_bundle_if_newer`: deixar `AuthDegradedError` propagar (não capturar); demais erros continuam logando e retornando `False` como hoje.

### Bloco 2 — estado degraded no daemon e pausa global de workers {reviewer: code}

- `drive_sync/daemon.py`:
  - `SyncDaemon.__init__`: `self._degraded: asyncio.Event = asyncio.Event()`, `self._degraded_reason: str | None = None`.
  - Novo método `_enter_degraded(reason: str)`: idempotente — se já degraded, no-op; senão seta `_degraded`, guarda razão, chama `Notifier.degraded(reason)` (bloco 4), loga CRITICAL com tag `[AUTH_DEGRADED]`. **Formato da `reason`**: priorizar campos parseados (`kind`, `code`) sobre `stderr_tail`. Exemplo: `"invalid_credentials (Code=8002) — tail: ..."`. Evita que o tail de 500 chars (invariante do CLAUDE.md) corte fora a informação crítica que já foi extraída pelo classificador.
  - `_worker`: antes de chamar `_process_folder`, checar `if self._degraded.is_set(): self.queue.task_done(); continue` — drena fila sem rodar jobs.
  - `_worker` envolve `_process_folder` em `try/except AuthDegradedError as exc: await self._enter_degraded(f"{exc.kind}: {exc.stderr_tail}")` antes do `except Exception` genérico.
  - `_periodic_full_sync`: também checar degraded antes de enfileirar — evita inflar a fila à toa.

### Bloco 3 — probe proativo de auth {reviewer: code}

- `drive_sync/sync_engine.py`:
  - Novo método `RcloneEngine.auth_probe() -> None` — executa `rclone about <remote>:` (chamada leve, retorna metadados da conta); reusa `_run`, então o classificador já cobre. Levanta `AuthDegradedError` em falha de auth; outros erros retornam silenciosamente (probe não deve degradar por erro de rede genérico).
- `drive_sync/daemon.py`:
  - Nova coroutine `_auth_probe_loop()`: `await asyncio.wait_for(self._stop_event.wait(), timeout=interval)` no padrão de `_periodic_full_sync`. Em cada tick, se não degraded, chama `engine.auth_probe()`; captura `AuthDegradedError` → `_enter_degraded(...)`. Skip se degraded (não probar tendo certeza de que está quebrado).
  - `run()`: agendar a task junto com workers e periodic.
- `drive_sync/config.py`:
  - Novo dataclass `HealthCheckConfig(enabled: bool = True, interval_seconds: int = 3600)`.
  - Adicionar `health_check: HealthCheckConfig` em `AppConfig`, com defaults.
- `config/config.yaml`:
  - Seção comentada `health_check:` com as duas chaves e documentação curta. Operador pode desativar para máquinas que não devem fazer chamada periódica.

### Bloco 4 — sinalização (sd_notify + notify-send + log estruturado) {reviewer: code}

- `drive_sync/notifier.py` (novo arquivo):
  - Classe `Notifier` com método `degraded(reason: str)`. Encapsula três side-effects best-effort, **cada um isolado em try/except**: sinalização não pode derrubar o daemon.
  - `sd_notify`: subprocess `systemd-notify --status="degraded: <reason>"` (não adiciona dep PyPI; `systemd-notify` está na base do Fedora). Falha silenciosa se `NOTIFY_SOCKET` não estiver no env (rodando fora de systemd).
  - `notify-send`: subprocess `notify-send --urgency=critical "drive-sync" "<reason>"`. Falha silenciosa em headless (sem `DISPLAY`/`DBUS_SESSION_BUS_ADDRESS`).
  - `log`: `log.critical("[AUTH_DEGRADED] %s", reason)` no logger do módulo `drive_sync`. Tag fixa para grep/journalctl.
- `drive_sync/daemon.py`:
  - `SyncDaemon.__init__` instancia `Notifier()`; `_enter_degraded` chama `self._notifier.degraded(reason)`.
- `scripts/install.sh` + unit systemd (decisão registrada em [ADR-003](../decisions/ADR-003-type-notify-sinalizacao-degraded.md)):
  - Atualizar template da unit: `Type=notify`, `NotifyAccess=main`. Necessário para `sd_notify STATUS=...` ser aceito.
  - O daemon precisa enviar `READY=1` para o systemd considerar o serviço up sob `Type=notify` — adicionar chamada `systemd-notify --ready` ao final do bootstrap (após `_watcher.start()` e enfileiramento inicial das pastas).

### Bloco 5 — testes {reviewer: qa}

- `tests/test_sync_engine.py`:
  - `_classify_rclone_stderr`: casos felizes (8002, 9001) e negativos (texto sem código, código diferente como 503, código colado em palavra). Inclui amostras reais do journal de 2026-05-11.
  - `_run` levanta `AuthDegradedError` quando stderr matcha; retorna normal quando não.
  - `bisync_folder` e `_ensure_remote_dir` propagam `AuthDegradedError` (não capturam).
- `tests/test_daemon.py`:
  - Worker captura `AuthDegradedError` lançada pelo `engine` (mock), transita para degraded, e jobs subsequentes na fila são drenados sem ir para o engine.
  - `_enter_degraded` é idempotente — chamar duas vezes só dispara `Notifier.degraded` uma vez.
  - `_auth_probe_loop` dispara `_enter_degraded` quando `engine.auth_probe` levanta; não dispara em outras exceções; respeita `health_check.enabled=False`.
- Sinalização **não** ganha teste de integração (subprocess externo); cobertura via inspeção manual abaixo.

### Bloco 6 — atualização de documentação operacional {reviewer: doc}

- `CLAUDE.md` — nova entrada em "Operational Invariants":
  - "**Daemon degraded em falha de auth (complementa, não substitui, o invariante `bisync errors do NOT auto-recover`)**: erros bisync genéricos continuam logando e seguindo; apenas falha de auth identificada (`Code=8002`/`Code=9001` no endpoint `/api/auth/v4`) dispara pausa global dos workers e sinalização via `systemctl status` (`STATUS=degraded: ...`), `notify-send` e log tagueado `[AUTH_DEGRADED]`. Recuperação manual: `rclone config update proton 2fa <code>` + `systemctl --user restart drive-sync`. Sem auto-resume — flakiness lateral da Proton pode mascarar problemas residuais."
- `config/config.yaml` — documentar `health_check`.

## Verificação end-to-end

- `python -m pytest tests/ -v` passa com os novos testes em test_sync_engine.py e test_daemon.py.
- `python -m drive_sync --check` continua aceitando o config (com e sem a seção `health_check`).
- `python -m drive_sync --status` continua funcionando — não toca esse caminho.

## Verificação manual

Cenário 1 — reativo (falha disparada por job real):

1. Com daemon rodando em estado saudável, simular invalidação de token: editar `~/.config/rclone/rclone.conf` zerando `client_uid =` na seção `[proton]` (mesmo efeito do que vimos hoje).
2. Forçar um job: tocar arquivo em uma pasta sincronizada para o watcher disparar.
3. Confirmar que **em segundos**:
   - `systemctl --user status drive-sync` mostra `Status: "degraded: invalid_credentials: ..."`.
   - `journalctl --user -u drive-sync | grep AUTH_DEGRADED` retorna a linha.
   - Se houver sessão gráfica: notificação `notify-send` aparece com urgência crítica.
4. Tocar arquivo em **outra** pasta. Confirmar pelo journal que o job foi enfileirado mas **não** executou (drenado em degraded).
5. Renovar token: `rclone config update proton 2fa <code>` + `systemctl --user restart drive-sync`. Confirmar que `systemctl status` volta a `Status: ...` normal e os jobs voltam a rodar.

Cenário 2 — proativo (probe detecta antes de job real):

1. Reduzir `health_check.interval_seconds` para `60` no config temporariamente, restart.
2. Invalidar token (passo 1.1 acima). **Não** tocar arquivo.
3. Esperar até 60s. Confirmar a mesma sinalização do cenário 1, **sem** que nenhum job de pasta tenha sido tentado.
4. Recuperar (passo 1.5 acima). Restaurar interval para 3600s.

Cenário 3 — sinalização em headless:

1. Em sessão SSH sem `DISPLAY`, repetir cenário 1.
2. Confirmar que `notify-send` falha silenciosamente (sem crash) e que os outros dois canais (`sd_notify`, log tagueado) funcionam normalmente.

Cenário 4 — idempotência sob carga:

1. Com daemon saudável, invalidar token (passo 1.1 do cenário 1).
2. Tocar arquivos em **várias** pastas simultaneamente (`touch /storage/3.\ Resources/Projects/foo /storage/3.\ Resources/Scripts/bar ...`) para o watcher disparar 3+ jobs em paralelo.
3. Confirmar: **uma única** linha CRITICAL `[AUTH_DEGRADED]` no journal; **uma única** notify-send; `systemctl status` mostra `STATUS=` uma vez. Race entre workers não vira flood.
4. Recuperar (passo 1.5 do cenário 1).

Cenário 5 — `Notifier` degradado não derruba o daemon:

1. Simular ambiente sem `systemd-notify` no PATH: `sudo mv /usr/bin/systemd-notify /usr/bin/systemd-notify.bak` (ou setar `NOTIFY_SOCKET=` vazio via override da unit).
2. Repetir cenário 1.
3. Confirmar: daemon entra em degraded mesmo assim; log tagueado `[AUTH_DEGRADED]` aparece; daemon **não crasha** (cada canal isolado em try/except, conforme bloco 4).
4. Restaurar `systemd-notify`.

## Notas operacionais

- **Ordem dos blocos recomendada**: 1 → 2 → 4 → 3 → 5 → 6. O bloco 4 (sinalização) precisa vir antes do bloco 3 (probe) porque `_enter_degraded` depende do `Notifier`. Testes (5) ao final porque cobrem 1-3.
- **`Type=notify` quebra serviço já em produção até o `READY=1` ser enviado** — o systemd vai considerar `activating` até receber o sinal. Se o `READY=1` for esquecido ou o bootstrap der erro antes dele, `systemctl start` trava em timeout (default 1min30s). Reviewer do bloco 4 deve confirmar que o `READY=1` está no caminho-feliz do `run()`.
- **Probe via `rclone about`**: emite 1 request HTTP por execução; com interval default 1h, são 24 requests/dia/máquina — negligível dentro do uso já existente.
- **Tag `[AUTH_DEGRADED]`** abre porta para alerting externo no futuro (Promtail/Loki, fluentd) sem mudar nada no daemon; é a interface de log estruturado mínima.
- **Probe não consulta backend durante degraded**: `_auth_probe_loop` faz skip explícito se `self._degraded.is_set()`. Deliberado e consistente com a recusa de auto-resume — confirmar repetidamente que o backend está quebrado não traz informação nova, queima rate-limit e pode aprofundar o gate de CAPTCHA da Proton. Operador renova token e reinicia; degraded é volátil, restart limpa.
- **CLI `drive-sync --once`**: `AuthDegradedError` lançada durante o single-pass propaga e o processo encerra com exit code != 0 (sem traceback bonito — é caso de erro real, não fluxo nominal). Não cria estado degraded persistente porque o processo já termina. Confirmar no `__main__.py` que o entrypoint do `--once` não captura genericamente `Exception` mascarando o exit code; se capturar, adicionar `except AuthDegradedError: sys.exit(...)` antes do catch-all.
- **Rate-limit em loop de restart manual**: operador que reiniciar o serviço em loop durante a renovação verá `N` notificações para `N` restarts. Aceitável porque o vetor é manual e raro; sem tratamento (debounce de N segundos seria complexidade não justificada para o uso real).
- **Follow-up condicional**: se algum dia o operador rodar em headless real (sem sessão gráfica), registrar nova linha no BACKLOG para canal alternativo (webhook). Não antecipar agora — YAGNI.
