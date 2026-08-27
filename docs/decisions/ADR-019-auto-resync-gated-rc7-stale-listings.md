# ADR-019: Auto-recuperação data-safe (gated) de rc=7 stale-listings

**Data:** 2026-08-27
**Status:** Aceito

## Origem

- **Incidentes-instância (2×):** 2026-08-22/24 (`archive`, travado ~38h) e 2026-08-27 (`library`,
  travado ~18h). Em ambos, uma interrupção no meio de um `rclone bisync` longo (queda de rede no 1º;
  storm de 5xx da Proton no 2º) destruiu os arquivos de estado `.lst` do bisync — **dados locais e
  remotos intactos, só o baseline morreu** — e o daemon passou a abortar a pasta a cada ciclo com
  `rc=7: cannot find prior Path1 or Path2 listings ... Must run --resync to recover`, permanecendo
  degradado **até `--resync` manual**.
- **Frame:** `briefs/auto-resync-gated-rc7-stale-listings.md` (v1, frozen 2026-08-27). Spec
  `specs/spec-autoresync-rc7-stale-listings.md` (v1). Tracker **#47**. Braço de **código** da família
  de recovery bisync; o braço-doc (playbook manual) é `briefs/recovery-safety-abort-bisync.md`.
- **Decisão base emendada:** o invariante **"bisync errors do NOT auto-recover"** (ADR-003 /
  CLAUDE.md § Operational Invariants). Este ADR abre uma **exceção restrita** a ele, na mesma forma
  do precedente [ADR-016](ADR-016-classe-proton-infra-auto-resume-gated.md) (exceção gated-por-probe).

## Contexto

O caminho de erro do bisync (`sync_engine.bisync_folder`) retornava falha em qualquer `rc≠0` e
**deliberadamente não** invocava `--resync` — o comentário no código documentava o invariante ("não
invocamos automaticamente para não causar perda de dados"). O motivo é real: `rclone bisync --resync`
é uma operação de **união** (superset) — arquivos presentes só no lado remoto (Path2) são copiados de
volta ao local (Path1). Disparar um resync cego **ressuscita conteúdo movido/deletado**; não é neutro.

Mas o modo de falha stale-listings é **estrutural em pastas grandes** (quanto maior a árvore, mais
longa a janela de listagem em que uma interrupção corrompe o estado) e o caso **dominante é benigno**:
o estado morreu, mas as duas árvores ainda coincidem. Nesse caso o `--resync` é um no-op (reconstrói o
baseline, 0 transferências, 0 deleções) — auto-recuperável sem violar o espírito do invariante, desde
que a segurança do dado seja **provada antes de agir**.

## Decisão

**Abrir uma exceção RESTRITA e fail-safe ao invariante, gated por prova data-safe (dry-run).** Em
`bisync_folder`, quando um abort casa a assinatura stale-listings (`_is_stale_listings`) **E** o knob
`rclone.auto_resync_stale_listings` está on (default `true`) **E** o guard de 1-tentativa permite:

1. **Prova (dry-run):** roda `bisync ... --resync --dry-run` com os flags/excludes live
   (git_handling-aware). A saída prova união no-op sse — e só se — **não há** nenhuma linha
   `... as --dry-run is set` (cada uma seria uma cópia que o resync real faria) **E** o bloco de
   stats fecha em `Transferred: 0 B / 0 B` (`_dryrun_resync_is_noop`; parse fixada no spike
   `docs/spikes/SP-T1-autoresync-dryrun-parse.md`, rclone v1.74.3).
2. **Age só se provado:** união no-op → roda o `--resync` real, reconstrói o baseline, a pasta retoma
   na **mesma chamada**. Divergência real ou dry-run ambíguo/inesperado → **NÃO age**, permanece
   degradado + sinalizado (o caminho de hoje). Fail-safe por construção: a parse erra sempre para o
   lado de não-agir (C2/S2 do Spec).

**Guard de 1-tentativa-por-episódio:** um `set` de markers em `RcloneEngine` evita re-disparar o
dry-run a cada ciclo num folder divergente preso. Limpo em qualquer sucesso de bisync; reseta no
restart (novo processo → tentativa fresca, simétrico ao re-avaliar-janela de ADR-007).

**Observabilidade:** tag dedicada `[BISYNC_AUTORESYNC]` (desfechos `attempted` / `recovered` /
`skipped (divergent…)` / `skipped (resync real falhou…)`), no padrão das tags de ADR-012. Recuperação
bem-sucedida é **silenciosa no `notify-send`** (só journal), como o reset de degraded de ADR-005 — sem
double-signal com o watchdog externo (ADR-014). Investigação:
`journalctl --user -u drive-sync --grep "BISYNC_AUTORESYNC"`.

## Consequências

- **MTTR do caso benigno cai de horas/dias para ~1 ciclo** — o objetivo (S1). O incidente `library`
  (~18h no escuro, com o watchdog re-alarmando a cada 30min) deixa de existir para o caso dominante.
- **O invariante segue valendo para o caso perigoso.** Divergência genuína (união não é no-op) NUNCA
  auto-recupera — o espírito de "não ressuscitar conteúdo deletado" é preservado pela gate, não
  abandonado. A recuperação manual (playbook) continua sendo o caminho para o caso divergente e para
  `rc=1` too-many-deletes.
- **Custo:** uma passada de listagem extra (o dry-run) além da que o resync faria de qualquer modo —
  bounded, e só num folder já preso (C3). Sob o lock serializado (ADR-001), como todo job rclone.
- **Kill-switch:** `rclone.auto_resync_stale_listings: false` restaura o comportamento legado
  (BISYNC_FAIL + degradado + recovery manual) — escape hatch se a parse do dry-run algum dia se provar
  não-confiável (ex.: mudança de formato num bump de rclone; gatilho de revisão registrado no spike).
- **Habilita `rc=1`:** com J1 existindo, a extensão da auto-recuperação a `rc=1` too-many-deletes
  (guard-rail mais delicado — divergência real, direção do resync importa) pode herdar o padrão do
  pre-check. Reavaliação armada no Brief-pai (`recovery-safety-abort-bisync.md` §N2).

## Alternativas consideradas

- **Interpretar a saída da própria tentativa de recuperação** (1 passada em vez de 2) — **rejeitado,
  inseguro:** o `--resync` real já muta; não dá para "olhar depois" e decidir não-agir. A prova tem de
  preceder a ação → dry-run dedicado.
- **Pre-check por comparação top-level de árvores** (a direção original do tracker) — **rejeitado como
  a gate:** proxy raso que deixa passar divergência aninhada (fere C2). Sobrevive só como possível
  otimização de early-reject barato antes do dry-run, não implementada.
- **Hash item-a-item da árvore inteira** — **rejeitado:** seguro mas excede o custo aceitável (C3) sem
  ganho sobre provar o no-op da própria união.
- **`--resync-mode path1`** (usado na recuperação manual do Brief-pai) — **não usado no caminho auto:**
  inócuo depois de provar no-op (não há nada a resolver em direção alguma); a segurança vem do gate
  dry-run, não do mode.
