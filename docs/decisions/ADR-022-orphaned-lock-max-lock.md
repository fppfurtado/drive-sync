# ADR-022: Auto-expiração de lock órfão de bisync via `--max-lock` nativo

**Data:** 2026-09-10
**Status:** Aceito

## Origem

- **Incidente-instância:** 2026-09-09 (`archive`, travado ~14h). Um restart do daemon no meio de um
  `rclone bisync` matou o subprocess rclone sem que ele liberasse o `.lck`; a cada ciclo seguinte o
  folder abortava `rc=1: prior lock file found`, permanecendo degradado **até `rm` manual do lock**.
- **Frame:** `briefs/orphaned-bisync-lock-recovery.md` (v2, frozen 2026-09-10). Spec
  `specs/spec-orphaned-lock-max-lock.md` (v2). Tracker **#88**. Braço de **código** da família de
  recovery bisync, **PEER** de [ADR-019](ADR-019-auto-resync-gated-rc7-stale-listings.md) (rc=7).
- **Decisão base emendada:** o invariante **"bisync errors do NOT auto-recover"** (ADR-003 /
  CLAUDE.md § Operational Invariants). Este ADR abre uma **exceção restrita** a ele, na mesma forma
  gated dos precedentes ADR-019 (rc=7 gated-por-dry-run) e ADR-016 (proton_infra gated-por-probe).

## Contexto

O `.lck` de bisync é um guard de concorrência: enquanto um run vive, ele impede um segundo bisync do
mesmo par. Sem `--max-lock`, o rclone grava o lock com `TimeExpires` efetivamente infinito
(ano 2226) — nunca expira. Um run que morre **não-graciosamente** (restart/stop do daemon mid-bisync,
kill por max-runtime de ADR-018, reboot, power-loss, OOM, kill manual) deixa o `.lck` órfão, e como
nada o recicla, o folder trava **permanente e silenciosamente**.

**A geração de locks órfãos tem múltiplas fontes** — inclusive interna: o próprio `_kill_stuck_proc`
(ADR-018) faz `SIGTERM→SIGKILL` no rclone sem remover o `.lck`. Uma limpeza no shutdown handler
cobriria só a morte graciosa; um probe de liveness custom (PID) reinventaria — pior — o que o backend
já faz. A flag nativa `--max-lock` cobre **todas** as fontes de uma vez.

## Decisão

**Passar `rclone --max-lock <dur>` em toda invocação de `bisync` do par** (knob
`rclone.max_lock_seconds`, default `3600`s/1h; `0` = desligado/legado; `>0` deve ser `≥120`, o mínimo
do rclone). A flag age como **PREVENÇÃO write-time**: o rclone grava `TimeExpires = now + <dur>` e
**renova a cada `<dur>`/2** enquanto o run vive — logo um run legítimo longo segue protegido — e, quando
o dono morre, o lock deixa de renovar e **expira `<dur>` após a última renovação**.

Ponto de inserção único no builder base do cmd de bisync (`sync_engine.bisync_folder`), de modo que o
cmd principal **e** as invocações `--resync`/dry-run da recuperação rc=7 (ADR-019) herdem a flag —
senão uma recuperação rc7 reescreveria um lock never-expire e reabriria a janela.

### Modelo de recuperação — CORRIGIDO por diagnóstico (o não-óbvio)

A intuição inicial ("expira o lock → bisync prossegue limpo, sem `--resync`") é **FALSA**, e foi
refutada por um diagnóstico (`debug`) com repro ao vivo (rclone v1.74.3, grid local-local, 2026-09-10):

1. **A expiração é governada pelo `TimeExpires` GRAVADO no `.lck`** (`now > TimeExpires`), **não** pelo
   flag `--max-lock` do LEITOR contra o mtime. Consequência dura: um lock **never-expire pré-existente**
   (`TimeExpires`=2226, escrito antes desta feature) **nunca** é expirado por um run posterior com
   `--max-lock` — a feature só previne locks never-expire **futuros** (ver § Migração).
2. **Ultrapassar um lock expirado NÃO é um proceed limpo:** o rclone **remove o lock E deleta as
   listings** (o `.lck` sinaliza "run anterior crashou"), abortando **`rc=7 stale-listings`** — mesmo
   com listings pristinas.
3. Logo a recuperação **COMPÕE com o braço rc=7 (ADR-019)**: o rc=7 resultante é auto-recuperado
   data-safe (dry-run prova união no-op no caso benigno → `--resync` reconstrói; divergência →
   permanece degradado, fail-safe). **Dependência dura: `rclone.auto_resync_stale_listings=true`** — com
   ela `false`, `--max-lock` converte o wedge permanente rc=1 num wedge rc=7 (ainda degradado, mas
   diagnosticável/recuperável pelo playbook), não auto-cura.

Fluxo completo: `run com --max-lock morre → lock para de renovar → após a janela, TimeExpires no
passado → próximo ciclo remove lock + deleta listings → rc=7 → ADR-019 dry-run no-op → --resync →
sucesso` (caso benigno). MTTR: janela + 1 ciclo de recuperação, contra horas/`rm` manual.

## Migração one-time (pré-existentes)

Como um `--max-lock` posterior **não** expira um lock never-expire já gravado (ponto 1 acima), os
`.lck` órfãos que já existem no momento do deploy exigem limpeza **manual, uma vez por host**:

```bash
# Identificar locks órfãos never-expire (TimeExpires distante) em ~/.cache/rclone/bisync/
for f in ~/.cache/rclone/bisync/*.lck; do
  [ -f "$f" ] || continue
  python3 -c "import json,sys; d=json.load(open('$f')); print('$f', d.get('PID'), d.get('TimeExpires'))"
done
# Para cada um: confirmar que o PID dono está MORTO e que nenhum rclone toca o par, então:
#   rm "<lock>"
```

**Não** se adiciona código de limpeza automática no startup: uma limpeza segura de never-expire
exigiria a prova de dono-morto por PID-probe que este ADR/Spec deliberadamente evita (o sinal de idade
é inútil para never-expire — ele nunca renova, viva ou morta a origem). A classe é finita (só os
pré-existentes) e drena com a migração; todo lock escrito após o deploy já é finite-expiry.

## Consequências

- **Positivas:** nenhum lock órfão trava um folder para sempre (dos escritos sob a feature); zero código
  de lock custom; cobre todas as fontes de morte, inclusive o kill de ADR-018; run legítimo longo segue
  protegido (renovação); concorrência genuína segue bloqueada (lock renovado).
- **Trade-offs / footguns:**
  - Janela de exposição `<dur>` (default 1h << 12h de staleness da ADR-005 → o operador em geral nem vê
    o `[FOLDER_DEGRADED]` no caso comum).
  - Dependência dura de `auto_resync_stale_listings=true` (acima).
  - Migração one-time para locks pré-existentes (acima).
  - Risco residual R1: um run vivo com renovação ESTOLADA > `<dur>` (SIGSTOP, I/O travado) auto-expiraria
    o próprio lock — mitigado por ADR-001 (serialização intra-daemon torna a corrida quase-impossível) +
    margem de renovação (30min no default) + ADR-018 (mata job vivo > 2h).
  - R2: um upgrade de rclone pode mudar a semântica de `--max-lock` — mitigado pelo knob (`0` desliga) e
    pelo sinal de regressão (reaparecimento de `prior lock file found` com bisync vivo).

**Cap de teste:** a expiração (`TimeExpires` passado → rc=7) e o contraste never-expire (rc=1, flag
irrelevante) têm regressão em `tests/test_max_lock_orphan.py` (integração rclone local-local). A
composição fim-a-fim (rc=7 → ADR-019 → sucesso) e a renovação-enquanto-vivo NÃO são re-testadas em CI
(exigiriam rclone vivo > 2min / um remote real) — cobertas pelos testes do ADR-019 + evidência viva do
frame/`debug`.

**Investigação:** `journalctl --user -u drive-sync --grep "BISYNC_FAIL"` (o `prior lock file found`
some no caso comum; se reaparece com bisync vivo, ver R2). Recovery composto:
`--grep "BISYNC_AUTORESYNC"` (o braço rc=7 que de fato recupera).
