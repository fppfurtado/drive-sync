# ADR-013: Degrade poll-only em esgotamento inotify (sem crash-loop)

**Data:** 2026-08-16
**Status:** Proposto

## Origem

- **Incidente:** issue #20 (2026-08-16) — com a auth Proton restaurada (#19), o daemon passou a crashar
  no startup: `OSError: [Errno 28] inotify watch limit reached` em `watcher.py:start → observer.start()`.
  systemd auto-restart → crash-loop (auth OK → 30s → crash → restart), com as 14 tarefas de backup
  down e zero sinal ao operador além do journal. Daemon parado manualmente para cessar churn de CPU.
- **Contexto de pressão:** `fs.inotify.max_user_watches = 121523` (default do kernel, sem override);
  14 tarefas observadas recursivamente — `/storage/dev/projects` sozinho (repos + `.git`/node_modules)
  consome fração grande do pool per-user, competindo com VS Code/JetBrains.
- **Decisões base:** [ADR-003](ADR-003-type-notify-sinalizacao-degraded.md) (convenção de sinalização
  degraded: log tagueado + `STATUS=` + notify-send) e [ADR-005](ADR-005-folder-staleness-degraded.md)
  (degrade sinalizado sem parar o daemon; `STATUS` agregada em `_compose_status_payload`). Este ADR
  estende a mesma convenção ao subsistema watcher.

## Contexto

O watcher (inotify via watchdog) é uma **otimização de latência** — detecção de mudança em tempo real.
O mecanismo de completude é outro: o `periodic_full_sync_seconds` (default 1800s) re-enfileira todas as
tarefas de qualquer forma. Esgotamento de inotify é condição de **ambiente** (pool per-user compartilhado,
dimensionado fora do controle do daemon), não bug interno: tratá-lo como fatal converte perda de latência
em perda total de serviço — exatamente o incidente.

## Decisão

Esgotamento de recursos inotify no startup do watcher (`ENOSPC` watches / `EMFILE` instances) **degrada o
daemon para poll-only** em vez de derrubá-lo:

1. **`watcher.py`:** `FilesystemWatcher.start()` captura `OSError` com `errno ∈ {ENOSPC, EMFILE}`
   **apenas na superfície inotify** (`observer.schedule` + `observer.start`), libera watches parciais já
   criados (`unschedule_all` + `stop`, best-effort passo-a-passo — falha num passo não pula os seguintes,
   logada em WARNING) e converte em `WatchLimitError` tipada. Outros `OSError` propagam intactos (bug real
   deve crashar). A fronteira estreita é deliberada: `ENOSPC` também significa "disco cheio" (ex.: mkdir) —
   classificar isso como esgotamento de watches mandaria o operador ao sysctl errado.
   **Sem watcher parcial:** quais folders ganharam watch antes do esgotamento é opaco (falha dentro de
   `observer.start()`), e um watcher parcial daria falsa sensação de tempo real por folder. **Limitação
   conhecida (watchdog):** o emitter cujo `Inotify.__init__` estourou pode reter o fd/watches já criados
   até restart do processo — a liberação é best-effort, não garantia.
2. **Materialização de `local_path` é do daemon, não do watcher:** o mkdir de pastas ausentes saiu de
   `FilesystemWatcher.start()` para `SyncDaemon._ensure_local_paths()` (antes de `_start_watcher`) — é
   setup compartilhado pelos dois modos. No degrade, uma pasta ausente em modo bundle/skip "sincronizaria
   nada com sucesso", invisível ao staleness ADR-005.
3. **`daemon.py`:** `_start_watcher()` captura `WatchLimitError` → daemon segue **sem watcher**; workers e
   periodic full-sync operam normalmente (todo sync passa a vir do ciclo periódico). Sinalização tripla
   padrão ADR-003/005: log `[WATCHER_DEGRADED]`, `notify-send`, `STATUS=degraded: watcher off (...) —
   poll-only a cada <N>s`. Precedência de STATUS: auth > watcher > folders (watcher + folders compõem na
   mesma linha).
4. **Liveness em runtime:** `_check_watcher_liveness()` roda no piggyback do periodic (mesmo padrão do
   staleness ADR-005): observer morto OU qualquer emitter morto → mesmo degrade poll-only + sinalização
   (`watcher morreu em runtime`). Fecha o meio-estado silencioso: o watchdog engole falhas de watch em
   runtime (`contextlib.suppress(OSError)` em adds recursivos) e um emitter pode morrer sem notificar o
   Observer — o daemon acreditaria ter tempo real que já não existe.
5. **Gate do degrade:** exige `periodic_full_sync_seconds > 0`. Com o periodic desligado não resta
   mecanismo de sync — daemon "up" seria no-op silencioso (pior que crash); nesse caso o erro segue fatal,
   com mensagem actionable (aumentar `fs.inotify.max_user_watches` ou habilitar o periodic) e exit limpo
   (rc=1, `WatchLimitError` capturada em `__main__.py` — sem traceback; sob systemd `Restart=` o retry
   continua, deliberadamente: é a config do operador que tornou o erro fatal).
6. **Sem auto-recover:** watches não são re-tentados em runtime; recuperar tempo real = resolver a pressão
   (sysctl ou reduzir escopo observado) + restart. Simetria com "bisync errors do NOT auto-recover".
   Trade-off aceito: um esgotamento **transitório** (spike de watches de outro processo no boot) latcha
   poll-only até restart manual — pré-mudança o crash-loop do systemd "auto-resolvia" esse sub-caso; o
   sinal (STATUS + notify-send) é o mecanismo de recuperação, consistente com ADR-003.

## Alternativas consideradas

- **Pré-flight do budget de watches:** contar dirs antes de subir o watcher e comparar com
  `max_user_watches`. Rejeitada: o pool é per-user e compartilhado (IDEs consomem concorrentemente) — o
  número no pré-flight está obsoleto no instante seguinte; a captura do erro real é o único gate honesto.
- **Podar o conjunto observado (skip `.git`/node_modules, honrar `auto_exclude` no watcher):** reduziria a
  pressão de fato, mas é mudança ortogonal e maior (watchdog agenda watch recursivo por raiz; poda exige
  descer a árvore manualmente). Fica como otimização futura se a pressão persistir pós-degrade; o degrade
  continua necessário como piso (a poda não elimina o esgotamento, só o adia).
- **Watcher parcial (manter folders que ganharam watch):** rejeitada — ver §Decisão item 1.
- **`Restart=on-failure` com backoff no unit:** não resolve — cada tentativa re-esgota; crash-loop mais
  lento continua sendo backup down sem sinal.

## Consequências

- Sob esgotamento, latência de sync cai de ~debounce para até `periodic_full_sync_seconds` (default 30min)
  — aceitável para backup; a perda é visível via `systemctl status` (STATUS) + notify-send + journal grep
  `WATCHER_DEGRADED`.
- Staleness ADR-005 continua funcional (piggyback no periodic) — cobre o caso de o poll-only também falhar.
- Esgotamento **em runtime** tem duas camadas: adds suprimidos pelo watchdog (subtree novo não-observado,
  eventos perdidos) são cobertos pelo periodic como safety-net de *dados*; morte de observer/emitter é
  detectada pelo liveness probe (§Decisão item 4) como safety-net de *sinal*. Watch-add suprimido SEM morte
  de thread continua invisível ao probe — só o periodic mitiga (cadência, não detecção).
