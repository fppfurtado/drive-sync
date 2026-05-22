# ADR-005: Sinalizar pasta degradada por staleness (gatilho temporal per-folder)

**Data:** 2026-05-17
**Status:** Proposto

## Origem

- **Decisão base:** [ADR-003](ADR-003-type-notify-sinalizacao-degraded.md) — sinalização degraded global por falha de auth. Este ADR estende o canal de sinalização com um segundo gatilho (temporal, per-folder) sem alterar o gatilho original.
- **Investigação:** /debug 2026-05-17 — `dev-projects` ficou 7 dias parada com `march failed: directory not found` (não-auth) sem percepção do operador.

## Contexto

ADR-003 cobre apenas falha de auth identificada (`Code=8002`/`Code=9001` no endpoint `/api/auth/v4`). Quando uma pasta acumula abortos `rclone bisync` por causa não-auth — exemplo concreto: flakiness do walker `march` enumerando o lado remoto, que vira "Must run --resync to recover" e congela o estado de trabalho — o invariante consciente `bisync errors do NOT auto-recover` ([sync_engine.py:134-137](../../drive_sync/sync_engine.py)) faz o daemon logar e seguir, sem sinalizar. A pasta pode ficar parada por dias até inspeção manual de `journalctl`.

Histórico empírico (período mar-mai 2026, log preservado em `~/.local/state/drive-sync/drive-sync.log`): 5 eventos `march failed`. 4 deles cobertos por ADR-003 (primeira mensagem `Code=10013`/auth); 1 não coberto (primeira mensagem `directory not found`), o que motivou o incidente dos 7 dias.

A causa-raiz dessas abortagens é externa (backend protondrive); rclone bisync por desenho aborta a operação inteira ao primeiro erro de listagem (*"too dangerous to continue"*) e exige `--resync` operacional. Não há defesa direta dentro do daemon contra a abortagem em si. A defesa viável é **observabilidade temporal**: detectar pasta parada por staleness, independente do código de erro.

## Decisão

Estender o canal de sinalização degraded (ADR-003) com um segundo gatilho **temporal e per-folder**: pasta sem sincronização bem-sucedida há mais que `folder_staleness_threshold_seconds` (default **12 horas**) entra em `_degraded_folders` no `SyncDaemon`. Sinalização tripla (paralela a ADR-003, **sem pausar workers**):

- Log CRITICAL com tag `[FOLDER_DEGRADED] <folder>: <reason>` (distinta de `[AUTH_DEGRADED]` para não confundir os triggers em busca textual).
- `notify-send` (best-effort se houver sessão gráfica).
- `STATUS=degraded folders: <lista agregada>` via `sd_notify`, com precedência para a STATUS auth-degraded global de ADR-003 quando ambos ativos.

Recuperação: ao primeiro `_process_folder` que retornar sucesso para a pasta, a entry é removida e a STATUS agregada recomputada — silenciosamente, sem `notify-send` de recuperação. Estado in-memory (`dict[str, float]` para `_last_successful_sync_at`, `dict[str, str]` para `_degraded_folders`), sem persistência cross-restart, alinhado com [ADR-004](ADR-004-cooldown-gate-periodic-full-sync.md). Detecção rodada como piggyback no `_periodic_full_sync` (sem novo loop dedicado).

Coexistência de relógios com [ADR-004](ADR-004-cooldown-gate-periodic-full-sync.md): `_last_sync_at` (cooldown) usa relógio monotônico do event loop (`asyncio.get_running_loop().time()`); `_last_successful_sync_at` (staleness) usa **wall-clock** (`time.time()`) porque o threshold é tempo absoluto (12h reais), não relativo à vida do processo. Consequência do wall-clock no restart está em Trade-offs.

Nota sobre status formal de ADR-003: figura como `Proposto` mas o canal está em produção (`drive_sync/notifier.py` implementa-o; `daemon._enter_degraded` emite). ADR-005 trata ADR-003 como decisão efetivamente aceita; promoção formal do status fica como hygiene operacional separada.

Dependência operacional entre `folder_staleness_threshold_seconds` e `watcher.periodic_full_sync_seconds`: a detecção é piggyback no loop periódico (recusa de loop dedicado preserva simplicidade — sem novo background task no daemon). Consequência: `folder_staleness_threshold_seconds > 0` exige `periodic_full_sync_seconds > 0`. O loader (`config.py`) rejeita a combinação inválida no startup com mensagem explícita; opt-out de staleness é via `folder_staleness_threshold_seconds: 0`, não desligando o periodic.

Composição da STATUS agregada vive no daemon, não no Notifier: o daemon já é o estado-holder natural (`_degraded_reason` para auth global de ADR-003, `_degraded_folders` para staleness). Compor o payload final no daemon e chamar `_systemd_notify` direto preserva o contrato fire-and-forget do Notifier (sem refatorar para stateful). Notifier ganha apenas o método `folder_degraded(folder, reason)` para log + notify-send; STATUS sd_notify continua sendo responsabilidade de quem detém o estado.

Razões:

- **Cobrir a lacuna comprovada por incidente real**: 7 dias parados sem percepção. O custo de adicionar observabilidade temporal é baixo (in-memory, sem novo IPC), e o ROI em incidentes futuros é alto.
- **Per-folder sem pausa global**: o cenário real mostrou que as outras pastas continuaram sincronizando OK durante os 7 dias do `dev-projects`. Pausar tudo penalizaria pastas saudáveis sem benefício — a falha é localizada por design (uma pasta = um estado bisync separado).
- **Gatilho temporal, não contagem de falhas**: captura a semântica observável real ("pasta parou de progredir"), é robusto a flakiness transitória (3-5 falhas em 1h não disparam falso-alarme se a 6ª deu sucesso) e independe do código de erro específico — funciona tanto para `march failed` quanto para qualquer falha futura desconhecida.
- **Default 12h**: para o folder mais conservador da config atual (`dev-projects` com `cooldown_seconds: 10800`/3h), 12h cobrem ~4 ciclos esperados. Curto o suficiente para detecção bem antes do incidente virar drama; longo o suficiente para evitar falso-alarme em pasta com cooldown longo que teve um ciclo pulado.
- **Tag distinta `[FOLDER_DEGRADED]`** (não reuso de `[AUTH_DEGRADED]`): grep operacional deve distinguir os dois cenários. Runbook de recuperação é diferente: AUTH → renovar TOTP; FOLDER → diagnosticar a pasta específica (provavelmente `--resync` manual).
- **Sinalização sem pausa de workers**: preserva o invariante de ADR-003 de pausa global como reação específica a falha de auth (que afeta todas as pastas). FOLDER degraded é sinalização pura, sem alteração de comportamento de scheduling.

## Consequências

### Benefícios

- Detecção em até ~12h + cadência do `_periodic_full_sync` (na prática, próximo ciclo). Incidente de 7 dias vira incidente de meio-dia.
- `systemctl --user status drive-sync | grep STATUS` passa a refletir o estado por pasta — visível em headless via SSH.
- Pastas saudáveis continuam fluindo: o invariante da ADR-003 (pausa global em auth) é preservado e este ADR não introduz nova pausa.
- Custo de implementação baixo: estado in-memory, piggyback no loop existente, sem novo serviço/IPC/persistência.

### Trade-offs

- Após restart do daemon, a janela de detecção é re-avaliada usando wall-clock (`now - daemon_start_time`); folders que sincronizaram com sucesso *antes* do restart aparecerão como degraded até completarem o primeiro ciclo pós-restart. Aceitável — o caso é raro e auto-corrige no próximo sucesso.
- Default 12h pode gerar falso-alarme em pasta com `cooldown_seconds` muito longo (>4h) combinado com flakiness diária. Mitigação: follow-up opcional via `FolderConfig.staleness_threshold_seconds` override per-folder, se o caso aparecer empiricamente.
- Mais um campo de config (`folder_staleness_threshold_seconds`). Opt-out via `0` documentado no `config/config.yaml` de referência.

### Limitações

- Não cobre a *causa-raiz* da abortagem (flakiness do backend protondrive ou de qualquer falha bisync). Apenas observabilidade — recuperação continua manual via `--resync`. Auto-recovery foi recusada por conflitar com o invariante `bisync errors do NOT auto-recover` ([CLAUDE.md → Operational Invariants](../../CLAUDE.md)).
- Detecção atrasa até ~12h. Para falhas que se manifestam em segundos (ex.: auth), continuar usando ADR-003. Os dois gatilhos coexistem.
- Invocação manual fora do systemd (`python -m drive_sync` direto): `NOTIFY_SOCKET` ausente; canal sd_notify vira no-op (já coberto pelo Notifier em ADR-003). Log e `notify-send` continuam funcionando.

## Alternativas consideradas

### Gatilho por contagem de falhas consecutivas (N=3 ou N=5)

Mais simples de implementar (contador inteiro por pasta), mas sensível a rajadas curtas: 3 falhas em 1h por flakiness genuína disparam falso-alarme se o sistema se auto-recuperar depois. N=5+ atrasa a sinalização sem ganho semântico. **Recusada** — gatilho temporal captura melhor "pasta parou de progredir", que é a invariante operacional real, e é robusto a flakiness intermitente.

### Pausa global per-folder degraded (mesmo modelo de ADR-003)

Pausar workers ao detectar pasta degradada simplificaria a sinalização (só precisaria reutilizar `Notifier.degraded`), mas penalizaria pastas saudáveis: o cenário real mostrou que durante os 7 dias do `dev-projects`, todas as outras pastas continuaram sincronizando OK. **Recusada** — pausa global é apropriada para falhas que afetam todas as pastas (auth), não para falhas localizadas.

### Auto-`--resync` em pasta degradada

Resolveria o sintoma sem intervenção manual, mas conflita com o invariante consciente `bisync errors do NOT auto-recover` ([CLAUDE.md](../../CLAUDE.md) → Operational Invariants). `--resync` é destrutivo (escolhe um lado como source-of-truth e pode sobrescrever mudanças remotas legítimas em janelas longas de divergência). **Recusada** — invariante existe por razão (segurança de dados); este ADR é sobre observabilidade, não sobre alterar a política de recuperação.

### Persistência cross-restart do contador

Disco-backed dict de `_last_successful_sync_at` sobreviveria a restarts. Mas restart imediato re-avalia a janela contra wall-clock corretamente, e a única consequência da volatilidade é o caso raro descrito em Trade-offs (folder OK pré-restart aparecendo como degraded até próximo ciclo). **Recusada** — alinhamento com ADR-004 (cooldown também in-memory pela mesma razão); custo de IO + lock + migração > benefício no caso degenerado.

## Gatilhos de revisão

- **Falso-alarme em pasta com `cooldown_seconds` muito longo** (>4h) combinado com flakiness intermitente diária: introduzir `FolderConfig.staleness_threshold_seconds` override per-folder. Sinal objetivo: ≥2 incidentes consecutivos reportados pelo operador onde o STATUS apontou degraded mas o próximo ciclo se recuperou sem ação manual.
- **`bisync errors do NOT auto-recover` revisado**: se o invariante mudar (ex.: auto-`--resync` controlado adicionado), reabrir este ADR — o gatilho temporal pode virar input pro classificador de recovery em vez de só sinalização.
- **Nova superfície de sinalização introduzida** — sinal objetivo: `grep -E "requests\.post|smtplib|webhook|http\.client" drive_sync/notifier.py` retorna match, OU contagem de métodos públicos do Notifier (`grep -c "    def [a-z]" drive_sync/notifier.py`) passa de N para N+1. Re-avaliar precedência `auth-degraded > folder-degraded` na nova superfície e onde injeção de estado degraded deve viver.

## Referências

- Plano de execução: [docs/plans/folder-staleness-degraded.md](../plans/folder-staleness-degraded.md)
- ADR anterior estendido: [ADR-003](ADR-003-type-notify-sinalizacao-degraded.md)
- ADR de pattern in-memory alinhado: [ADR-004](ADR-004-cooldown-gate-periodic-full-sync.md)
- Invariante referenciado: `CLAUDE.md` → "Operational Invariants" → `bisync errors do NOT auto-recover`.
