# ADR-004: Cooldown por folder gate-keia também o periodic_full_sync

**Data:** 2026-05-14
**Status:** Proposto

## Origem

- **Investigação:** plano [folder-sync-cooldown](../plans/folder-sync-cooldown.md) durante `/triage` 2026-05-14. O design-reviewer flagou que cobrir também o `periodic_full_sync` é decisão estrutural duradoura (modifica safety-net global documentada em `CLAUDE.md`), exigindo ADR antes da implementação. Caso real disparador: incidente 2026-05-13/14 com `tjpa/pje-2.1` sob `git_mode: bundle` (bundle multi-GB re-uploadado por ciclo do periodic, custo proibitivo).

## Contexto

O daemon `drive-sync` tem duas fontes de enfileiramento por folder:

1. **Watcher (eventos do filesystem):** debounce agrupa rajada, posta na queue.
2. **`periodic_full_sync_seconds`** (default 1800s em [`drive_sync/config.py`](../../drive_sync/config.py), `WatcherConfig`): task no daemon enfileira **todas** as pastas a cada N segundos — safety-net global contra eventos perdidos.

A introdução de `cooldown_seconds` por folder (plano `folder-sync-cooldown`) cria uma decisão semântica: o gate cobre apenas eventos do watcher, ou também os ciclos do `periodic_full_sync`?

A escolha modifica um invariante implícito documentado em `CLAUDE.md` ("periodic full-sync task at a configurable interval", sem ressalvas): hoje o periodic é safety-net garantida — toda pasta sincroniza no máximo a cada `periodic_full_sync_seconds`. Mudar isso é decisão estrutural duradoura, daí o ADR.

## Decisão

Cooldown gate-keia **todos** os enfileiramentos da pasta com `cooldown_seconds > 0`, incluindo os ciclos do `periodic_full_sync`. Implementação no consumer (worker do daemon), de modo que tanto eventos do watcher quanto da task periódica passam pelo mesmo gate.

Razões:

- **Resolve o caso motivador.** Sob bundle mode com `.git/` de GBs, o custo é re-upload do bundle inteiro por job. Sem cobrir o periodic, `cooldown_seconds: 14400` (4h) ainda permite re-upload a cada ciclo do periodic (1800s default) — feature vira meio-fix.
- **Mitiga parcialmente trade-off conhecido do [ADR-001](ADR-001-serializar-chamadas-rclone.md).** ADR-001 aceitou explicitamente que "pasta grande passa a bloquear pasta pequena no front rclone" (inversão da premissa de paralelismo do CLAUDE.md). Sob serialização do rclone, um job longo de bundle bloqueia todas as outras pastas na fila pelo mesmo período. Cooldown por folder dá ao operador uma ferramenta para reduzir esse bloqueio no caso específico em que ele dói — não é só "compõe", é mitigação parcial de um trade-off já aceito. Ganho duplo: economiza upload **e** libera fila para folders normais.
- **Estado em um lugar só.** Gate no worker significa `_last_sync_at` vivendo onde a queue é consumida; periodic e watcher passam pelo mesmo caminho, sem duplicar lógica em dois layers.

## Consequências

### Trade-offs

- **Safety-net global enfraquece para folders com cooldown.** Pasta com `cooldown_seconds: 14400` perde a garantia de "no máximo 1800s entre syncs" — passa a ser "no máximo 14400s". Operador que opta por cooldown está explicitamente assumindo esse trade-off.
- **Eventos perdidos só são recuperados no fim da janela.** Se o watcher falhar em capturar um evento (inotify saturado, race), a recuperação via periodic é adiada até a janela do cooldown abrir. Mitigação: o operador pode forçar sync manual via `rclone bisync` ou (futuro) `drive-sync --force-sync <folder>`.

### Benefícios

- **Folders caros viáveis.** `git_mode: bundle` em repos com `.git/` na casa dos GB volta a ser opção realista. Sem cobrir o periodic, bundle mode permanece inviável.
- **Fila serial do rclone respira.** Folders normais não esperam atrás de bundle de 4h.

### Limitações

- **Política não-uniforme entre folders.** Pastas sem cooldown configurado mantêm semântica original do periodic; pastas com cooldown a perdem. Coexistência exige operador entender as duas semânticas. Trade-off aceito pela alternativa "cooldown global" ter custo maior (acoplamento + perda da granularidade).
- **Sem cancellation cruzada com `_degraded` ([ADR-003](ADR-003-type-notify-sinalizacao-degraded.md)).** Task diferida continua dormindo durante degraded; ao acordar, deposita na queue e o degraded-gate do worker descarta. Custo desprezível, sem retrabalho extra.
- **Tasks diferidas precisam ser canceladas no shutdown** (`SIGTERM`/`SIGINT`, ciclo de vida systemd). Detalhe de implementação delegado ao plano `folder-sync-cooldown`; aqui só registra o requisito.

## Alternativas consideradas

### Cooldown só nos eventos do watcher (periodic ignora)

Preservaria a safety-net na semântica original. **Recusada** porque não resolve o caso motivador: bundle multi-GB seguiria re-uploadando por ciclo do periodic (1800s default), e cooldown 4h teria efeito apenas sobre rajadas curtas do editor — não sobre o custo recorrente que é o bloqueador real do `tjpa/pje-2.1`.

### Cooldown global em `WatcherConfig` (não por folder)

Política única para todas as pastas. **Recusada** como YAGNI invertido: motivador é caso específico de bundle em repo grande, não regra geral. Default global agressivo (4h para tudo) degradaria UX de folders pequenos; default permissivo (alguns minutos) não resolveria o caso motivador. Per-folder permite cada caso ser tratado com o valor adequado.

### Remover `periodic_full_sync` globalmente quando qualquer folder tem cooldown

Cooldown vira contrato exclusivo para os folders com cooldown e periodic some para os demais. **Recusada** por acoplamento excessivo — pasta sem cooldown não tem motivo para perder safety-net só porque outra pasta tem cooldown. Decisões devem ser por escopo (folder), não por presença/ausência de feature em outro folder.

## Gatilhos de revisão

- **≥2 entradas com `cooldown_seconds > 0` em `config.yaml` real do operador** (`grep -c "cooldown_seconds:" ~/.config/drive-sync/config.yaml` ≥ 2): reabrir para considerar default global em `WatcherConfig` como conveniência (não substituindo o per-folder).
- **`periodic_full_sync` removido ou semântica revogada por outra ADR** (`grep periodic_full_sync drive_sync/daemon.py` retorna vazio, ou ADR posterior com `Substitui: ADR-004`): este ADR fica obsoleto.
- **Gap observado > `cooldown_seconds` no log com folder em modo cooldown** (`journalctl --user -u drive-sync | grep "Iniciando job.*<folder>"` + verificação manual de intervalos): indica evento perdido não recuperado pela próxima janela → considerar mecanismo de força (`drive-sync --force-sync <folder>` ou similar) que bypassa o gate.
