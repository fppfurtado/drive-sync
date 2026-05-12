# Plano — Notifier: escrita direta em NOTIFY_SOCKET via stdlib socket

## Contexto

Em 2026-05-12, ao deployar o PR #4 (auth-pause-on-failure) e tentar `systemctl --user start drive-sync`, o daemon ficou em `activating` por ~45s e falhou com `Result: timeout`; systemd reiniciou em loop. Diagnóstico fechado em /debug:

Causa-raiz: `drive_sync/notifier.py:31` envia `READY=1` via `subprocess.run(["systemd-notify", "--ready"])`. Sob `NotifyAccess=main` (definido na [ADR-003](../decisions/ADR-003-type-notify-sinalizacao-degraded.md)), o systemd valida o SCM_CREDENTIALS do sender — que é o PID do subprocess child, não do MainPID do daemon — e descarta silenciosamente. systemd-notify(1) tenta truque via `--pid=auto`/pidfd para spoofar credenciais do parent, mas observamos empiricamente que não funciona neste host (Fedora 43, systemd 258). Confirmação: toggle `NotifyAccess=all` no unit faz o daemon subir normalmente; revertido para `=main` reproduz o timeout.

Impacto além do startup: `_notifier.degraded()` usa o mesmo `_systemd_notify`. Mesmo quando o auth-pause de fato disparar (caminho-feliz da feature recém-merged), `STATUS=degraded: ...` não chega ao systemctl status — quebra o canal de sinalização prometido pela ADR-003. O worker-pause em si (asyncio.Event) e o log `[AUTH_DEGRADED]` seguem funcionando; só o canal externo via systemctl está silenciado.

**ADRs candidatos:** [ADR-003](../decisions/ADR-003-type-notify-sinalizacao-degraded.md) (preservada — o contrato `Type=notify` + `NotifyAccess=main` continua válido; o fix muda apenas o mecanismo de envio). O gatilho de revisão da ADR fala em `systemd-notify --ready` literal — vale o reviewer confirmar que a invariante real ("última linha antes do bloqueio em `SyncDaemon.run()`") segue capturada após o fix mesmo sem o binário systemd-notify ser invocado.

**Linha do backlog:** notifier: substituir subprocess `systemd-notify` por escrita direta em `NOTIFY_SOCKET` — sob `NotifyAccess=main` o PID do child do subprocess é rejeitado, travando `systemctl start` em timeout (READY=1 perdido) e silenciando `STATUS=degraded:` quando auth-pause dispara.

## Resumo da mudança

- `Notifier._systemd_notify` passa a abrir `socket.socket(AF_UNIX, SOCK_DGRAM)`, conectar em `os.environ["NOTIFY_SOCKET"]` e enviar os bytes do protocolo diretamente. Sender = MainPID do daemon → `NotifyAccess=main` aceita.
- Tratar prefixo `@` no `NOTIFY_SOCKET` (abstract socket no Linux) traduzindo para NUL byte no path antes de `connect`.
- Protocolo: `b"READY=1\n"` para `ready()`, `b"STATUS=<reason>\n"` para `degraded()` (formato do `sd_notify(3)`).
- **Invariante explícita de não-bloqueio:** `socket.settimeout(1.0)` antes de `connect`/`sendto`; falha por timeout cai no mesmo `except OSError` (best-effort, log debug). A motivação do bug é "daemon trava no `systemctl start`" — abrir mão dessa garantia reproduziria o sintoma em forma nova.
- `_notify_send` permanece via subprocess (`notify-send` não envolve systemd, não tem o problema).
- Comentário do módulo (linha 3) atualizado: `notify-send` continua subprocess; `sd_notify` vira socket direto stdlib. Decisão "sem libs PyPI" preservada.
- `tests/test_notifier.py` novo, cobrindo os caminhos.

Fora de escopo:
- Adicionar lib PyPI (`sdnotify` ou bind libsystemd). **Alternativa descartada:** `sdnotify` é pure-Python e faz exatamente isso, mas stdlib resolve em ~10 linhas para um único call-site; dependência adicional é peso por nada e contraria a decisão da docstring atual.
- Alterar o contrato da ADR-003 (`Type=notify` + `NotifyAccess=main` permanecem).
- Mexer em `_notify_send`.

## Arquivos a alterar

### Bloco 1 — drive_sync/notifier.py: socket direto para sd_notify {reviewer: code}

- `drive_sync/notifier.py`: reescrever `_systemd_notify` usando `socket.AF_UNIX/SOCK_DGRAM`; suportar prefixo `@` (abstract). Best-effort: `try/except (OSError, socket.error)` com log debug, não propaga ao daemon (mesmo contrato do código atual). Atualizar docstring do módulo (linha 3) para refletir o split sd_notify-via-socket / notify-send-via-subprocess.

### Bloco 2 — tests/test_notifier.py: cobertura da nova implementação {reviewer: qa}

- `tests/test_notifier.py` (novo): monkeypatch em `socket.socket` para capturar payload. Cenários:
  - `ready()` envia `b"READY=1\n"` quando `NOTIFY_SOCKET=/tmp/sock` no env.
  - `degraded("foo")` envia `b"STATUS=degraded: foo\n"` pelo socket e dispara `_notify_send` (mockado via monkeypatch em `subprocess.run`).
  - Sem `NOTIFY_SOCKET` no env, `_systemd_notify` é no-op (não tenta socket).
  - `NOTIFY_SOCKET=@my-abstract` traduz para path com NUL inicial em `connect()`.
  - `socket.connect` levantando `OSError` é capturado (log debug, sem raise).

## Verificação end-to-end

- `python -m pytest tests/test_notifier.py -v` passa.
- `python -m pytest tests/ -v` passa (suite completa — não regredir `test_daemon.py` que mocka o Notifier no nível mais alto).
- **Gatilho da ADR-003 preservado**: `git diff drive_sync/daemon.py` (após o batch) mostra `self._notifier.ready()` ainda como última linha antes de `await self._stop_event.wait()` em `SyncDaemon.run()`. Critério binário, load-bearing para a ADR-003 ficar honrada.

## Verificação manual

Lacuna que o plano anterior (auth-pause-on-failure.md) não cobriu: os 5 cenários assumiam daemon já em execução. Aqui o cenário 1 é o que falsifica o fix se não funcionar.

1. **Cold start sob `systemctl --user start`** (crítico — o sintoma original do bug):
   - `pipx install --force "/storage/3. Resources/Projects/drive-sync"` para deployar o branch.
   - `systemctl --user restart drive-sync` → aguardar até 60s.
   - `systemctl --user is-active drive-sync` retorna `active` (não fica em `activating` nem entra em loop de restart).
   - **Descartar regressão silenciosa** (achado do design-reviewer): `systemctl --user show drive-sync -p Type,NotifyAccess` retorna literalmente `Type=notify` + `NotifyAccess=main` (não voltamos a `=all` por engano nem o unit perdeu o `Type=notify`). E `systemctl --user show drive-sync -p ActiveEnterTimestamp -p ActiveState` mostra `ActiveState=active` com `ActiveEnterTimestamp` dentro de segundos do `restart` (não via `timeout→Restart` mascarando).
   - `journalctl --user -u drive-sync --since "2 min ago"` não contém `start operation timed out` nem `Failed with result 'timeout'`.

2. **Canal `STATUS=degraded:` chega ao systemd** (exercita ADR-003 end-to-end, agora pela primeira vez de verdade):
   - Com daemon rodando, forçar erro de auth: `rclone config update proton 2fa 000000` (TOTP inválido) → tocar um arquivo em qualquer pasta watched, aguardar debounce + job.
   - `systemctl --user status drive-sync` mostra linha `Status: "degraded: invalid_credentials (Code=8002) — tail: ..."` logo após `Main PID:`.
   - Recuperação: `rclone config update proton 2fa <real-code>` + `systemctl --user restart drive-sync`.

3. **Headless / invocação manual fora do systemd** (sanity-check do no-op):
   - `python -m drive_sync` direto no terminal (sem `NOTIFY_SOCKET` no env).
   - Daemon arranca normal, sem erros relacionados ao Notifier.

## Notas operacionais

- Ordem dos blocos: Bloco 1 antes de Bloco 2 (testes precisam da nova API).
- Reviewer do Bloco 1: atenção a (a) tradução do prefixo `@` para NUL byte (Linux-specific, fácil de errar); (b) `socket.settimeout()` curto antes de `connect` para não bloquear bootstrap se o socket estiver inacessível; (c) garantir que a falha de socket é silenciada (best-effort, mesmo contrato do subprocess atual com `check=False`).
- Reviewer do Bloco 2: validar que o teste do abstract socket usa o byte NUL real (`\x00`), não a string `@`. Confirmar que cenário sem `NOTIFY_SOCKET` não dispara abertura de socket (testar via mock que `socket.socket` não foi chamado).
- Gatilho de revisão da ADR-003 ("systemd-notify --ready deixa de ser a última linha..."): o termo literal "systemd-notify" sai do caminho-feliz, mas a invariante semântica (chamada que envia `READY=1` é a última antes de `await self._stop_event.wait()`) é preservada. Reviewer final do /run-plan deve confirmar no diff de `daemon.py` que `self._notifier.ready()` em linha ~257 continua sendo a última linha antes do bloqueio. A ADR não exige edit — o gatilho captura a estrutura do bootstrap, não o nome do binário.
- Pendência operacional fora deste plano: daemon em produção segue parado com pipx pre-PR-4 + unit Type=notify, e o resync de `library`/`dev-projects` está pendente da sessão anterior. Resolução: executar este plano via /run-plan, reinstalar via pipx, então retomar o resync na ordem original (library → dev-projects → start daemon).
