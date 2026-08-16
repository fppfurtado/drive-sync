# Plano — watchdog dead-man's-switch externo (#19, arm durável)

**Data:** 2026-08-16 · **Ratificado:** operador (decisões A+B, sessão 2026-08-16)

## Problema

Dois incidentes de silêncio multi-semana (auth 7 semanas, #19; crash-loop inotify, #20) com a
detecção interna FUNCIONANDO: notify-send disparou na transição e STATUS ficou degraded. O gap
não é detecção — é (1) **persistência do sinal** (uma notificação perdida = silêncio eterno;
STATUS é pull-based) e (2) **daemon morto não se auto-sinaliza** (crash-loop/parado: nenhum
código dentro do daemon cobre).

## Decisões ratificadas

- **A — mecanismo:** dead-man's-switch **externo ao daemon**: `drive-sync-watchdog.timer`
  (systemd user, 30min, `Persistent=true`) → oneshot rodando `drive-sync --watchdog`.
- **B — escopo:** a landmine do TOTP estático / discriminação 8002-secundário (direções a/b/c
  do BACKLOG.md) fica FORA — ciclo próprio, rastreado na entry existente do BACKLOG.

## Checks do `--watchdog` (cada um cobre um incidente real)

1. **Serviço não-active** (`systemctl --user show`: ActiveState) — cobre #20 (crash-loop/parado).
2. **STATUS degraded** (StatusText) — re-emite o alerta a cada ciclo enquanto persistir — cobre
   #19 (AUTH_DEGRADED perdido uma vez = perdido pra sempre).
3. **Frescor dos success markers** (`~/.cache/drive-sync/state/<fs_key>.success`, threshold =
   `watcher.folder_staleness_threshold_seconds`, opt-out herdado via 0) — redundância externa do
   ADR-005; cobre daemon "vivo mas inútil" que o próprio STATUS não reporte.
   Marker AUSENTE só alarma se o serviço está active há > threshold (ActiveEnterTimestampMonotonic
   — mata falso-positivo de instalação/folder novos).

## Sinalização

`notify-send --urgency=critical` re-emitido a cada ciclo com problemas (a persistência que faltava)
+ stdout → journal da unit (`journalctl --user -u drive-sync-watchdog`) + exit 1 (oneshot failed →
visível em `systemctl --user --failed`; terceira superfície).

## Não-objetivos

- Auto-remediação (restart automático etc.) — sinal, não ação; consistente com "sem auto-resume"
  (ADR-003) e "bisync errors do NOT auto-recover".
- Canal remoto (e-mail/push) — notify-send + journal + --failed por ora; canal headless é
  follow-up se virar caso real (mesma nota do BACKLOG § health-check).

## Limitações aceitas

- Marker age em wall-clock: suspend > threshold pode gerar 1 alerta falso no resume
  (`Persistent=true` dispara ao acordar), auto-limpo no ciclo seguinte ao periodic do daemon.
  Externo ao processo, sem acesso ao dual-clock do ADR-007 — aceito e documentado.
- Parada DELIBERADA do daemon também alarma (é o ponto de um dead-man's-switch); manutenção
  planejada: `systemctl --user stop drive-sync-watchdog.timer` junto.

## Entregáveis

`drive_sync/watchdog.py` (checks + alerta) · `--watchdog` no `__main__.py` ·
`systemd/drive-sync-watchdog.{service,timer}` · install.sh habilita o timer ·
testes (estado do serviço, markers, composição do alerta, exit codes) · ADR-014 · CLAUDE.md.
