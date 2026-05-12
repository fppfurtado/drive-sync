# ADR-003: Adotar `Type=notify` na unit systemd para sinalização de estado degraded

**Data:** 2026-05-11
**Status:** Proposto

## Origem

- **Plano de execução:** [docs/plans/auth-pause-on-failure.md](../plans/auth-pause-on-failure.md) — bloco 4 da execução depende desta decisão.
- **Bloqueante de revisão:** design-reviewer (2026-05-11) flagou que a migração `Type=simple → Type=notify` é decisão estrutural duradoura da mesma classe que [ADR-002](ADR-002-relaxar-hardening-systemd-protondrive.md) (política de unit systemd com gatilho de revisão próprio).

## Contexto

Em 2026-05-11 o daemon acumulou 292 ocorrências de `Code=8002` (falha de auth do backend protondrive) ao longo de ~10h sem qualquer sinalização ao operador; a intervenção manual só aconteceu por descoberta acidental. O plano [auth-pause-on-failure](../plans/auth-pause-on-failure.md) responde a esse gap com um estado `degraded` no `SyncDaemon` que precisa ser sinalizado por três canais — entre eles, `sd_notify STATUS=...` (visível em `systemctl --user status drive-sync`). O `sd_notify` é o canal nativo do systemd para estado interno do serviço; funciona em headless (onde `notify-send` não chega) e tem custo operacional zero.

A unit atual ([scripts/install.sh](../../scripts/install.sh), template renderizado em `~/.config/systemd/user/drive-sync.service`) usa `Type=simple`. Sob `Type=simple`, o systemd não escuta `sd_notify` (`NotifyAccess=none` por default) — qualquer `STATUS=` enviado é descartado silenciosamente. Para o canal funcionar, a unit precisa:

1. Mudar para `Type=notify` (ou ativar `NotifyAccess=main` em `Type=simple`, mas `Type=notify` é o padrão idiomático e permite mais sinais — `READY=1`, `RELOADING=1`, `STOPPING=1`).
2. Sob `Type=notify`, o systemd considera o serviço `activating` até receber `READY=1`. Se o daemon não enviar `READY=1` (ou o bootstrap crashar antes), `systemctl start` trava no timeout default (`TimeoutStartSec=1min30s`).

A escolha de `Type=notify` introduz uma dependência operacional nova: o caminho-feliz do `SyncDaemon.run()` precisa emitir `READY=1` ao final do bootstrap (após watcher iniciado e fila inicial preenchida). Falha em emitir não é silenciosa — vira timeout de start visível ao operador.

## Decisão

Migrar a unit systemd de `Type=simple` para `Type=notify`, com `NotifyAccess=main`.

Razões:
- `sd_notify STATUS=...` é o canal de sinalização de estado interno mais barato e nativo do ambiente em que o daemon roda (systemd --user). Recusá-lo por não querer mexer no `Type=` significa perder o canal mais valioso em headless.
- `Type=notify` é estritamente **aditivo** às diretivas de hardening preservadas por [ADR-002](ADR-002-relaxar-hardening-systemd-protondrive.md) (`NoNewPrivileges=yes`, `PrivateTmp=yes`): toca apenas o protocolo de readiness/sinalização, não namespace/mount/filesystem. Não há reversão implícita das proteções relaxadas/mantidas naquela ADR.
- Alinhamento com ADR-002 como precedente de gênero: política de unit systemd já foi tratada como decisão estrutural duradoura; este ADR segue o mesmo padrão de registro e gatilhos de revisão.

## Consequências

### Benefícios

- `systemctl --user status drive-sync` passa a refletir o estado real do daemon — `STATUS=degraded: ...` aparece a segundos do primeiro erro de auth, sem depender de log parsing ou sessão gráfica.
- Canal nativo funcionando libera o caminho para futuras sinalizações de estado (`RELOADING=1` em reload de config, `STOPPING=1` em shutdown limpo) sem nova decisão de plataforma.

### Trade-offs

- Bootstrap do daemon ganha uma responsabilidade nova: emitir `READY=1` (via `systemd-notify --ready` ou equivalente) ao final do caminho-feliz de `SyncDaemon.run()`. Sob `Type=notify`, o systemd considera o serviço `activating` até receber esse sinal; esquecer ou ordenar errado trava o `systemctl start` em timeout (~90s default) — falha ruidosa, mas falha. Reviewer do bloco 4 do plano deve confirmar que `READY=1` está no caminho-feliz.
- `Type=notify` em conjunto com `PrivateTmp=yes` (mantido da ADR-002) precisa de confirmação empírica de não-regressão no primeiro deploy — o histórico do projeto recomenda paranoia com hardening da unit. Status do ADR sai de `Proposto` para `Aceito (validação empírica pendente)` ao merge do plano, seguindo o padrão da ADR-002.

### Limitações

- Não cobre invocação manual fora do systemd (`python -m drive_sync` direto): nesse caso `NOTIFY_SOCKET` não está no env, e o `Notifier.degraded` faz no-op no canal sd_notify (já previsto no plano). Sem trade-off real — invocação manual de fora não esperaria `systemctl status` mesmo.

## Alternativas consideradas

### Manter `Type=simple` sem `sd_notify`

Perde o canal nativo de status; restariam `notify-send` (não funciona headless) e log tagueado (precisa `journalctl --grep`). **Recusada** por reduzir a visibilidade do degraded justamente no cenário (headless/SSH) em que ela mais importa.

### `Type=notify-reload` ou `Type=dbus`

`notify-reload` agrega ciclo de `RELOADING=1` baseado em sinal — fora do escopo (não há reload de config hoje). `Type=dbus` exigiria o daemon ganhar nome de barramento D-Bus dedicado — overkill para o caso. **Recusadas** como complexidade não justificada.

### `Type=simple` com `NotifyAccess=main`

Tecnicamente permite `sd_notify STATUS=` (preserva o canal de status); o que se perde são os sinais idiomáticos do `Type=notify` — `READY=1`, `RELOADING=1`, `STOPPING=1`. **Recusada** porque os sinais idiomáticos cobrem evolução futura já citada nos Benefícios (reload de config, shutdown limpo); abdicar deles agora paga complexidade adicional sem retorno proporcional.

## Gatilhos de revisão

- **`rclone#7381` resolvido upstream e pause-on-failure removido**: se o backend protondrive deixar de exigir auth recorrente e o estado degraded virar dead code, o `sd_notify STATUS=` perde valor primário. Considerar voltar para `Type=simple` para reduzir superfície da unit.
- **`systemd-notify --ready` deixa de ser a última linha do caminho-feliz de `SyncDaemon.run()` antes do `await self._stop_event.wait()`**: reabrir o ADR antes da mudança. A invariante objetivamente observável (grep no `drive_sync/daemon.py:run`) substitui o julgamento subjetivo de "o que pode falhar".

## Referências

- Plano de execução: [docs/plans/auth-pause-on-failure.md](../plans/auth-pause-on-failure.md)
- ADR anterior sobre política de unit: [ADR-002](ADR-002-relaxar-hardening-systemd-protondrive.md)
- systemd documentation: `systemd.service(5)` — seções `Type=`, `NotifyAccess=`, `sd_notify(3)`.
