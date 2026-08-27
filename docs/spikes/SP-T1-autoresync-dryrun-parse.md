# Spike SP-T1 — parse de `rclone bisync --resync --dry-run` (data-safety gate)

- Spec: `specs/spec-autoresync-rc7-stale-listings.md` (SP-T1) · tracker #47
- rclone testado: **v1.74.3** (mesma do host de produção)
- Data: 2026-08-27
- Resolve: R1 do Spec (toda a garantia de data-safety S2 repousa nesta parse)

## Pergunta

Como provar, de forma robusta, que um `rclone bisync ... --resync` seria **união no-op**
(0 transfers, 0 deleções) ANTES de executá-lo — condição que libera a auto-recuperação
gated (D2/C2). O `--resync` real é união e MUTA (ressuscita conteúdo só-remoto no local,
F2), então a prova tem de vir de um `--dry-run` dedicado, não da própria tentativa.

## Experimento

Par bisync local↔local (dois dirs), sem tocar a Proton. `--resync` inicial estabelece o
baseline `.lst`; depois `--resync --dry-run` nos dois cenários. As mensagens de dry-run
(`... as --dry-run is set`) e o bloco `Transferred:` são emitidos pelo **core do rclone**
(sync engine), não pelo backend — logo o formato é backend-agnóstico e vale para o
`protondrive` remoto (assumption registrada abaixo).

### Caso A — árvores idênticas (NO-OP, safe)

```
NOTICE: - Path2    Resync is copying files to    - Path1
NOTICE: - Path1    Resync is copying files to    - Path2
NOTICE:
Transferred:   	          0 B / 0 B, -, 0 B/s, ETA -
Checks:                 6 / 6, 100%, Listed 16
Elapsed time:         0.0s
```

Note: as duas linhas `Resync is copying files to` são **header fixo** (aparecem mesmo no
no-op) — NÃO são sinal de mutação. Não há nenhuma linha `... as --dry-run is set`.

### Caso B — divergente (p2 tem `only-remote.txt` que p1 não tem; p1 perdeu `b.txt`)

```
NOTICE: - Path2    Resync is copying files to    - Path1
NOTICE: b.txt: Skipped copy as --dry-run is set (size 5)
NOTICE: only-remote.txt: Skipped copy as --dry-run is set (size 18)
NOTICE: - Path1    Resync is copying files to    - Path2
Transferred:   	         23 B / 23 B, 100%, 0 B/s, ETA -
Checks:                 4 / 4, 100%, Listed 20
Transferred:            2 / 2, 100%
Elapsed time:         0.0s
```

Tell da divergência: uma linha `... Skipped copy as --dry-run is set` **por arquivo** que
seria copiado + `Transferred: 23 B / 23 B` (bytes ≠ 0) + a linha de contagem
`Transferred: 2 / 2`.

## Decisão de parse (o sinal a implementar em SP-T4)

O output vai para **stderr** (onde `_run` já captura). Em `--resync` não há deleções
(união nunca deleta — F2), então enumerar as would-be-copies cobre toda a superfície de
mutação. `rc` é **sempre 0** no dry-run — inútil como sinal.

**`is_noop` ⟺ AMBOS:**
1. **zero** ocorrências do padrão `as --dry-run is set` (enumera diretamente cada operação
   que o resync real faria — copy, e por robustez qualquer verbo: `Skipped \S+ as --dry-run
   is set`); **E**
2. o bloco de stats casa `Transferred:\s+0 B / 0 B` (confirmação secundária).

**Fail-safe (o cerne da C2/S2):** qualquer desvio — ≥1 linha `as --dry-run is set`, bytes
≠ 0, output não-parseável, ou stats ausente — é tratado como **NÃO-no-op** → NÃO
auto-recupera, permanece degradado. A parse erra sempre para o lado de não-agir.

Regexes propostos (SP-T4):
- `_DRYRUN_WOULD_MUTATE_RE = re.compile(r"as --dry-run is set")`
- `_DRYRUN_ZERO_BYTES_RE = re.compile(r"Transferred:\s+0 B / 0 B")`

## Assumptions / invalidators

- **Formato backend-agnóstico** (validado em backend local): as mensagens de dry-run e o
  bloco `Transferred:` vêm do core do rclone, não do `protondrive`. Invalidado se, numa
  execução real contra a Proton, o dry-run no-op emitir linha `as --dry-run is set` ou
  bytes ≠ 0 espúrios — sinal: `[BISYNC_AUTORESYNC] skipped (divergent)` num folder que o
  operador sabe estar íntegro. Mitigação: kill-switch (D5) + este doc registra a versão.
- **Requer ambos os sinais** (skip-lines E `0 B / 0 B`): se um rclone futuro omitir o
  bloco de stats, a condição (2) falha → fail-safe (não age). Gatilho de revisão: bump de
  rclone major/minor re-roda este experimento.
