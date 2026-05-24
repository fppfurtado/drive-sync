# ADR-007: Gate de staleness em monotonic (suspend-aware) — dual-clock com wall-clock para reason

**Data:** 2026-05-24
**Status:** Proposto

## Origem

- **Decisão base:** [ADR-005](ADR-005-folder-staleness-degraded.md) — escolheu wall-clock (`time.time()`) para `_last_successful_sync_at` com a justificativa "threshold é tempo absoluto (12h reais), não relativo à vida do processo". Este ADR revisa essa escolha à luz de incidente com `systemd-suspend` longo.
- **Investigação:** `/debug` 2026-05-24. Sistema suspendeu em `2026-05-23 22:41:58` e acordou em `2026-05-24 07:22:33` (8h40m, confirmado por `tailscaled: monitor: time jump detected (slept 8h40m34s)`). Logo após o wake, `dev-projects` (`_last_successful_sync_at = 2026-05-23 20:01`) entrou em `STATUS=degraded folders: dev-projects (sem sucesso há 12.0h)` sem ter falhado — apenas combinou o avanço do wall-clock durante o sono com o threshold de 12h.

## Contexto

Sob suspend, o `user.slice` (e portanto o processo `drive-sync`) é congelado pelo `systemd-sleep`. Wall-clock (`time.time()`) continua avançando — é tempo astronômico. Monotonic (`time.monotonic()`, `asyncio.get_running_loop().time()`) **não** avança durante suspend — é tempo de execução do processo.

ADR-005 escolheu wall-clock para staleness com a premissa implícita de que "12h de wall-clock sem sucesso" é um proxy razoável para "pasta parada". Essa premissa quebra quando o daemon não teve oportunidade real de tentar: suspend > threshold marca pasta saudável como degraded apenas porque o sistema dormiu, e não porque há falha de sincronização para investigar. Sinal vira ruído.

O cooldown (ADR-004) já usa monotonic e se comporta corretamente sob suspend: ele bloqueia retry até o daemon ter rodado N segundos **ativos**, o que é exatamente o que se quer (não emendar retry imediatamente após wake quando o usuário pode estar prestes a mexer em arquivos). A incoerência atual é assimétrica — staleness conta sleep, cooldown não.

## Decisão

Manter `_last_successful_sync_at` (wall-clock) como existe e introduzir em paralelo `_last_successful_sync_at_mono` (monotonic). O **gate** de `_check_folder_staleness` passa a consultar o dicionário monotonic; o **reason** anexado ao `Notifier.folder_degraded` e à `STATUS=degraded folders:` é computado como `min(elapsed_wall, elapsed_mono)` — wall-clock dá o "tempo real" quando coincide com o tempo ativo do daemon, monotonic limita superiormente para evitar reportar gap de cadência do `_periodic_full_sync` (default 1800s) como se fosse inatividade adicional. Adicionar `_daemon_start_monotonic = time.monotonic()` como baseline para folders nunca sincronizados, paralelo ao `_daemon_start_time` (wall-clock) que **permanece** como fallback do dicionário wall-clock para o reason.

Razões:

- **Resolve o incidente sem revogar trade-off real.** "12h de daemon ativo sem sucesso" continua sendo o invariante operacional alvo de ADR-005 (pasta que de fato não progride); o que muda é só a unidade de medida do gate, que passa a refletir corretamente o conceito.
- **Reason em wall-clock preserva a UX.** A mensagem `"sem sucesso há X.Xh"` continua reportando horas reais decorridas — informação útil ao operador (sob suspend longo, diz "sem sucesso há 12.5h" mesmo que o gate só dispare quando 12h ativas tiverem passado). Separar a unidade do gate da unidade do display elimina a tensão entre os dois requisitos.
- **Alinha com pattern de ADR-004.** Cooldown já é monotonic. Uniformizar o relógio do gate de staleness para monotonic remove a assimetria entre os dois gates e a confusão de manter dois conceitos de tempo em decisões análogas.
- **Custo mínimo, sem nova dependência.** Apenas um segundo dicionário e baseline no `__init__`. Não envolve D-Bus, subscrição a `PrepareForSleep` do logind, nem heurística de time-jump.

## Consequências

### Benefícios

- Falso-positivo de degraded após suspend longo (cenário recorrente em laptop usado normalmente) deixa de ocorrer.
- Coerência interna entre os dois gates do daemon (cooldown e staleness) — ambos passam a contar "tempo ativo do daemon".
- Reason em wall-clock continua dando visibilidade real ao operador no `systemctl status` e no `notify-send`.

### Trade-offs

- **Inconsistência pós-restart, em direção oposta a ADR-005.** Restart zera o monotonic; folders pré-restart caem no baseline `_daemon_start_monotonic`. ADR-005 errava para falso-positivo após restart (folder OK há >12h reais aparecia como degraded até primeiro sucesso); ADR-007 erra para falso-negativo (folder com falha real preexistente fica invisível por até `folder_staleness_threshold_seconds` ativas pós-restart). Aceitável porque (a) restart é evento raro/operacional, (b) próximo `_process_folder` com falha real ainda registra o erro no log, e (c) o cenário-alvo recorrente deste ADR (suspend de laptop) é muito mais frequente que falha-real-cruzando-restart.
- **Dois dicionários espelhados.** Adiciona uma linha de manutenção (atualizar ambos em `_process_folder`). Invariante (após qualquer sucesso de `_process_folder`, ambos os dicionários refletem o evento) é o que o plano cobre com teste; quebra silenciosa do espelhamento faz o gate divergir do reason.

### Limitações

- Não cobre o caso em que o operador deixa o sistema ligado mas sem rede por 12h+ ativas — o daemon teria oportunidade de tentar, falharia, e o gate dispararia corretamente como falha real. Distinto do cenário-suspend e fora do escopo deste ADR.

## Alternativas consideradas

### Detectar wake via D-Bus (`PrepareForSleep` do logind)

Subscrever o sinal `org.freedesktop.login1.Manager.PrepareForSleep`, capturar o intervalo de sleep e estender `_last_successful_sync_at` em +`sleep_duration` no wake. Semanticamente expressivo (representa o conceito "houve um suspend"). **Recusada** por nova dependência PyPI (`dbus-next`/`jeepney`), complexidade de inicialização e tratamento de falha (sem sessão D-Bus disponível, instalação `--user` sem barramento, etc.) — overkill para um problema que monotonic resolve por construção.

### Heurística de time-jump no loop periodic

Comparar a cada iteração `time.time()` delta contra `time.monotonic()` delta esperado; gap > N segundos assume suspend e ajusta o baseline. Sem nova dependência. **Recusada** por imprecisão (só roda na cadência do `periodic_full_sync`, default 1h) e por reintroduzir lógica heurística onde monotonic-direto é exato — código adicional sem ganho.

### Mudar threshold para `CLOCK_BOOTTIME`

`BOOTTIME` conta tempo desde boot incluindo suspend — equivalente funcional ao wall-clock no que tange contar sleep. **Recusada** porque não resolve o problema (também marca degraded sob suspend); só evitaria confusão semântica se o objetivo fosse "tempo desde boot" em vez de "tempo de atividade do daemon".

## Gatilhos de revisão

- **Reason `sem sucesso há X.Xh` reportado com `X < (folder_staleness_threshold_seconds / 3600)`** (gate disparou mas reason mostra valor abaixo do threshold em horas): sinal de que o dicionário monotônico avançou sem o wall-clock correspondente — quebra do espelhamento entre os dois dicionários. Detectável com `journalctl --user -u drive-sync | grep -oP 'sem sucesso há \K[\d.]+(?=h)'` comparado contra `threshold/3600`. Reabrir para consolidar o espelhamento em invariante checada (ex.: assert no ponto de escrita).
- **Persistência cross-restart introduzida** para qualquer um dos dicionários (`grep -E "(pickle|json\.dump|sqlite)" drive_sync/daemon.py` retorna match relacionado a `_last_successful_sync_at*`): reavaliar a estratégia dual-clock — monotonic não é persistível (zera no boot), o que pede re-derivação no startup. Não é caso de uso atual.
- **Suspend tratado por outro componente** (ex.: systemd unit drop-in com `RestartSec`/`Restart=on-watchdog` configurado para reiniciar o daemon no resume, ou hook no `org.freedesktop.login1`): se o restart pós-wake virar política, este ADR fica obsoleto — restart re-avalia tudo contra `_daemon_start_monotonic`.

## Referências

- ADR estendido: [ADR-005](ADR-005-folder-staleness-degraded.md)
- ADR de pattern monotonic alinhado: [ADR-004](ADR-004-cooldown-gate-periodic-full-sync.md)
- Plano de execução: [`.claude/local/plans/staleness-suspend-aware.md`](../../.claude/local/plans/staleness-suspend-aware.md)

Hygiene operacional pendente (escopo separado, não toca este commit): ADR-005 e ADR-003 ainda figuram como `Proposto` apesar de estarem em produção. A existência deste ADR-007 (revisão de ADR-005 em produção) é evidência adicional para promover ambos a `Aceito` em commit futuro.
