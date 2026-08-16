# ADR-014: Watchdog dead-man's-switch externo ao daemon

**Data:** 2026-08-16
**Status:** Proposto

## Origem

- **Incidentes:** #19 (backup Proton down 7 semanas em silêncio — auth) e #20 (crash-loop inotify
  sem sinal). Em AMBOS a detecção interna funcionou: notify-send disparou na transição e STATUS
  ficou degraded (ADR-003/005). O silêncio veio de outro lugar.
- **Plano ratificado:** [docs/plans/watchdog-dead-mans-switch.md](../plans/watchdog-dead-mans-switch.md)
  (decisões A+B do operador, 2026-08-16).

## Contexto

Dois modos de silêncio que nenhum código DENTRO do daemon cobre:

1. **Sinal sem persistência.** notify-send é edge-triggered (1 disparo na transição); perdido =
   perdido pra sempre. STATUS é pull-based (só informa quem consulta).
2. **Daemon morto não se auto-sinaliza.** Crash-loop, parado, ou nunca subiu — o processo que
   deveria alertar é o que está fora do ar.

## Decisão

Componente EXTERNO mínimo: `drive-sync-watchdog.timer` (systemd user, `OnUnitActiveSec=30min`,
`Persistent=true`) → oneshot `drive-sync --watchdog` (`drive_sync/watchdog.py`), com 3 checks —
cada um mapeado a um modo de falha real:

1. **ActiveState != active** (via `systemctl --user show`) → cobre #20. Domina os demais checks
   (marker staleness com serviço parado é ruído derivado).
2. **StatusText degraded** → cobre #19: o alerta é **re-emitido a cada ciclo** enquanto o estado
   durar (a persistência que faltava; level-triggered em vez de edge-triggered).
3. **Frescor dos success markers** (`~/.cache/drive-sync/state/<fs_key>.success`, threshold
   reusa `watcher.folder_staleness_threshold_seconds`, opt-out herdado via 0) → redundância
   externa do ADR-005 para "vivo mas inútil". Marker ausente só alarma com o serviço active há
   mais que o threshold (`ActiveEnterTimestampMonotonic` — mata falso-positivo de instalação ou
   folder novos).

Superfícies de sinal (3): `notify-send --urgency=critical` re-emitido · stdout → journal da unit ·
exit 1 → oneshot failed, visível em `systemctl --user --failed`. Sem sd_notify (o watchdog não é
o MainPID; `NotifyAccess=main` rejeitaria).

**Sinal, não ação:** nenhuma auto-remediação — consistente com "sem auto-resume" (ADR-003) e
"bisync errors do NOT auto-recover". Install: `scripts/install.sh` copia as units e habilita o
timer (`enable --now`).

## Alternativas consideradas

- **Escalação dentro do daemon** (re-notify periódico no `_periodic_full_sync`): cobre o modo 1,
  estruturalmente cega ao modo 2 (daemon morto). O externo cobre os dois com um mecanismo só.
- **`OnFailure=` na unit do daemon:** dispara só na transição para failed — edge-triggered de
  novo (o mesmo defeito), e não cobre degraded-mas-active nem staleness.
- **Canal remoto (e-mail/push/healthchecks.io):** resolveria headless, custo de dependência
  externa + credencial. Fica como follow-up se notify-send + journal se provarem insuficientes
  (mesma nota do BACKLOG § health-check).
- **Hardening do classificador auth (landmine TOTP/8002-secundário):** deliberadamente FORA
  (decisão B) — ciclo próprio, rastreado na entry existente do BACKLOG.md.

## Consequências

- Silêncio multi-semana vira impossível por construção: pior caso ~30min até o primeiro alerta,
  re-emitido a cada 30min até resolver.
- Parada DELIBERADA do daemon também alarma (é o ponto de um dead-man's-switch). Manutenção
  planejada: parar também o timer (`systemctl --user stop drive-sync-watchdog.timer`).
- Marker age em wall-clock: suspend > threshold pode gerar 1 alerta falso no resume
  (`Persistent=true` dispara ao acordar), auto-limpo após o próximo periodic do daemon. Externo
  ao processo, sem o dual-clock do ADR-007 — trade-off aceito.
- Watchdog depende do systemd user manager: se o próprio manager morrer, ninguém alerta —
  fora do escopo (exigiria vigilante do vigilante; lingering + boot cobrem o caso comum).
