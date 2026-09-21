# Playbook — recuperação de safety-aborts recuperáveis do bisync

Procedimento operacional **manual e data-safe** para recuperar um folder que ficou preso abortando o `rclone bisync` a cada ciclo (degradado indefinidamente). Cobre as duas famílias de invalidação de estado que exigem `--resync` para recuperar:

- **rc=7 — stale-listings**: `cannot find prior Path1 or Path2 listings ... Must run --resync to recover` (as listagens `.lst` morreram, ex.: queda de rede no meio de um bisync longo). Cenário **benigno** quando as duas árvores ainda batem.
- **rc=1 — too-many-deletes**: `Safety abort: too many deletes (>50%, N of M) on Path1 ... Run with --force if desired` (uma mudança em massa legítima removeu >50% dos itens da visão do bisync). Cenário **perigoso** — a dica `--force` do rclone propaga as deleções e **causa perda de dados** se o conteúdo não estiver salvo em outro lugar.

> **Invariante que este playbook preserva** (ADR-003 / CLAUDE.md §Operational Invariants): _bisync errors do NOT auto-recover_. A recuperação é **sempre manual** — nenhum passo aqui introduz auto-cura no daemon. O daemon apenas loga `[BISYNC_FAIL]` e segue; cabe ao operador executar este procedimento.

Referências:
- [ADR-003](../decisions/ADR-003-type-notify-sinalizacao-degraded.md) — sinalização degraded / invariante de não-auto-recover
- [ADR-005](../decisions/ADR-005-folder-staleness-degraded.md) + [ADR-007](../decisions/ADR-007-staleness-monotonic-suspend-aware.md) — staleness per-folder (por que o folder fica `[FOLDER_DEGRADED]`)
- [ADR-014](../decisions/ADR-014-watchdog-dead-mans-switch-externo.md) — watchdog que re-emite o alerta a cada 30min enquanto o backup estiver ruim
- [ADR-008](../decisions/ADR-008-abandonar-bisync-repos-git.md) — `git_handling: auto` exclui repos com remote (relevante ao caso "re-homed em repo git")
- [playbook-flip-git-handling.md](playbook-flip-git-handling.md) — playbook irmão (a ordem purge+marker aparece lá para o cenário de flip)

---

## Passo 0 — Triage: qual é o trigger?

Descubra a causa exata antes de agir. Investigação primária:

```bash
# 1. Qual folder está degradado e desde quando?
systemctl --user status drive-sync | grep -i "Status:"          # ex.: "degraded folders: <folder> (sem sucesso há Nh)"

# 2. A linha ERROR exata do bisync (ADR-012 dá o call-site tagueado + stderr completo).
journalctl --user -u drive-sync --grep "BISYNC_FAIL" | tail -5

# 3. stderr completo da última falha daquele folder.
cat ~/.local/state/drive-sync/last-stderr-bisync-<folder_slug>.log
```

- Mensagem contém `too many deletes` → **rc=1**, vá para a [Seção rc=1](#recuperação-rc1--too-many-deletes-perigoso) (**perigoso — leia inteiro antes de agir**).
- Mensagem contém `cannot find prior Path1 or Path2 listings` / `Must run --resync to recover` → **rc=7**, vá para a [Seção rc=7](#recuperação-rc7--stale-listings-benigno).
- Mensagem contém `prior lock file found` → **rc=1, lock órfão** (causa DISTINTA de too-many-deletes — dados/listings intactos, é só um guard de concorrência preso), vá para a [Seção lock órfão](#recuperação-rc1--lock-órfão-benigno).

> **Regra transversal — NUNCA `--force` cego.** A dica `Run with --force if desired` do rclone propaga as deleções detectadas. Só é seguro depois de você ter confirmado que o conteúdo "deletado" não é dado único (existe backup em outro lugar) **e** ter decidido conscientemente que ele deve sumir do outro path. Este playbook nunca usa `--force`; usa `--resync` (reconstrução de baseline) com a ordem certa.

### Pré-condição comum

```bash
systemctl --user stop drive-sync.service
# Manutenção planejada? pare também o timer do watchdog para não alarmar:
systemctl --user stop drive-sync-watchdog.timer
systemctl --user status drive-sync.service   # confirma "inactive (dead)"
```

### Snapshot forense (recomendado antes de qualquer passo destrutivo)

```bash
mkdir -p ~/.local/state/drive-sync/snapshots
rclone lsl proton:Sync/ > ~/.local/state/drive-sync/snapshots/pre-recovery-$(date +%F).txt
```

---

## Recuperação rc=7 — stale-listings (benigno)

> **Desde [ADR-019](../decisions/ADR-019-auto-resync-gated-rc7-stale-listings.md) (#47) o caso benigno auto-recupera.** Com `rclone.auto_resync_stale_listings: true` (default), o daemon roda `--resync --dry-run` ao detectar stale-listings e, **se** provar união no-op (as duas árvores batem), reconstrói o baseline sozinho — sem este procedimento manual. Confirme com `journalctl --user -u drive-sync --grep "BISYNC_AUTORESYNC"` (`recovered`). Este procedimento manual é o **fallback** para quando o auto-resync **não** agiu: kill-switch off (`skipped (disabled)`), divergência real detectada pelo dry-run (`skipped (divergent…)`), ou o resync auto falhou. No caso divergente, o auto-resync se recusa **por design** (data-safety) — trate como o [caso rc=1](#recuperação-rc1--too-many-deletes-perigoso): decida a direção conscientemente antes de forçar.

O estado `.lst` morreu, mas os dados locais e remotos estão intactos. Recuperação = reconstruir o baseline.

**Pré-check de integridade** (confirma que é o caso benigno — o resync não destrói nada):

```bash
drive-sync --dry-run-resync <folder>
```

Roda o `--resync --dry-run` com os flags/excludes **exatos do daemon** (mesma fonte que `bisync_folder` usa) num workdir temporário — o cache real fica intocado — e classifica a divergência lendo os listings dos dois lados:

- **`VEREDITO: união NO-OP`** ou **`VEREDITO: SÓ-ADIÇÕES`** → **data-safe**, siga para a recuperação abaixo. Nenhum arquivo existe nos dois lados com conteúdo divergente, então o resync apenas cria.
- **`VEREDITO: TEM OVERWRITE`** → **NÃO é data-safe.** Os paths listados existem nos dois lados divergentes; o resync sobrescreveria o lado perdedor, destruindo aquela versão. Trate como o [caso rc=1](#recuperação-rc1--too-many-deletes-perigoso): decida a direção conscientemente.

Exit code para scripting: `0` seguro · `1` tem overwrite · `2` erro.

> **Por que não comparar o top-level à mão.** A versão anterior deste passo mandava comparar `rclone lsf --dirs-only` com `ls -1` — só diretórios, só o primeiro nível. No incidente do `areas` (#92) a divergência estava **quatro níveis abaixo** (`caldav/collections/.git/objects/…`) e esse pré-check teria passado trivialmente, sem tocar na pergunta que de fato decide.
>
> **E por que o texto do log não basta.** Medido em sandbox (rclone v1.74.3): o `--resync` **nunca deleta** — um arquivo removido de um lado é *restaurado* a partir do outro. O perigo real é o **overwrite** de um arquivo divergente nos dois lados, que o log emite como `Skipped copy as --dry-run is set` — indistinguível de uma adição inofensiva. Os listings carregam a distinção que o log perdeu; por isso o veredito vem deles.

**Recuperação** (deixe o daemon disparar `--resync` com os flags/excludes exatos e ao vivo — ver [Apêndice: o marker](#apêndice--como-o-marker-controla-o-resync)):

```bash
# Delete o marker do par → próximo ciclo do daemon vira first-run e dispara --resync sozinho.
python3 - <<'PY'
from drive_sync.config import load_config
from drive_sync import sync_engine as se
app = load_config()
folder = next(f for f in app.folders if f.name == "<folder>")
print(se._state_marker_for(folder.local_path, se.remote_uri_for(folder, app)))
PY
# → rm o caminho impresso, depois:
systemctl --user start drive-sync.service
systemctl --user start drive-sync-watchdog.timer   # se você o parou
```

Verifique a [recuperação](#verificação-comum). Se o pré-check acusou **`TEM OVERWRITE`** (divergência real, não só estado perdido), trate como o caso rc=1 too-many-deletes abaixo — decida a direção conscientemente.

---

## Recuperação rc=1 — lock órfão (benigno)

O bisync abortou `prior lock file found`: um `.lck` cujo processo dono morreu (restart/kill do daemon
mid-bisync, reboot, power-loss) sem liberar o lock. **Causa DISTINTA** de too-many-deletes abaixo —
dados e listings `.lst` intactos, é só um guard de concorrência preso.

> **Desde [ADR-022](../decisions/ADR-022-orphaned-lock-max-lock.md) (#88) o caso comum auto-expira.**
> Com `rclone.max_lock_seconds` > 0 (default 3600s/1h), todo lock escrito pelo daemon expira
> automaticamente `<dur>` após a última renovação quando o dono morre — o próximo ciclo desemboca em
> `rc=7 stale-listings` (ver [Seção rc=7](#recuperação-rc7--stale-listings-benigno)), que o auto-resync
> de ADR-019 recupera sozinho no caso benigno. Confirme com `journalctl --user -u drive-sync --grep
> "BISYNC_AUTORESYNC"` (`recovered`).
>
> Este procedimento manual é o **fallback** para quando a auto-expiração ainda não agiu (dentro da
> janela `<dur>`) ou quando o lock é **never-expire pré-existente** (escrito ANTES do deploy desta
> feature — `--max-lock` de um run posterior NÃO o expira; a expiração é pelo `TimeExpires` gravado no
> próprio lock, não pelo flag do leitor). Todo `.lck` never-expire remanescente exige esta migração
> manual **uma vez**; depois disso, todo lock novo é finite-expiry sob a feature.

**Diagnóstico — o dono está mesmo morto?**

```bash
# Localiza o .lck do par (mesmo prefixo dos .lst do folder degradado — ver Passo 0).
LCK=~/.cache/rclone/bisync/<prefixo-do-par>.lck
cat "$LCK"   # {"PID": "<pid>", "TimeRenewed": "...", "TimeExpires": "..."}

# Confirma que o PID dono NÃO existe mais (nunca remova um lock de processo vivo):
kill -0 <pid-do-lock> 2>/dev/null && echo "AINDA VIVO — não remover" || echo "morto, seguro remover"

# Confirma que nenhum rclone toca este par agora:
ps aux | grep "[r]clone.*bisync"
```

**Recuperação (data-safe, sem `--resync` manual — só destrava o guard):**

```bash
systemctl --user stop drive-sync.service   # ver Pré-condição comum acima
rm "$LCK"
systemctl --user start drive-sync.service
```

Verifique a [recuperação](#verificação-comum). Se o daemon voltar a abortar (agora `rc=7`
stale-listings), isso é **esperado e benigno** sob ADR-022/ADR-019 — deixe o próximo ciclo do
auto-resync agir, ou siga a [Seção rc=7](#recuperação-rc7--stale-listings-benigno) se preferir
confirmar manualmente.

---

## Recuperação rc=1 — too-many-deletes (perigoso)

O bisync viu >50% dos itens do baseline sumirem do Path1 (local) e travou o freio de segurança. **A recuperação depende da sua INTENÇÃO** com aqueles itens. Diagnostique primeiro; só então escolha o branch.

> **O daemon sinaliza este abort com advice safe (#52).** Ao detectar `too many deletes`, o daemon emite um log `[BISYNC_SAFETY_ABORT]` que **não** ecoa a dica `--force` cega do rclone e aponta para este branch. Confirme com `journalctl --user -u drive-sync --grep "BISYNC_SAFETY_ABORT"`. É só sinalização — a recuperação continua manual e consciente (nenhuma auto-cura no rc=1; diferente do rc=7 benigno de ADR-019).

### Passo 1 — Diagnostique: o que exatamente "sumiu"?

O safety abort compara o scan atual de Path1 contra o baseline `.lst`. Veja quais paths estão no baseline e não no scan novo:

```bash
cd ~/.cache/rclone/bisync/
# Ache o par do folder (nome sanitizado do local/remote); o .lst é o baseline bom, o .lst-new é o scan que abortou.
ls -lat | grep -i "<folder-ou-local-sanitizado>"
# Extraia os paths (última string entre aspas de cada linha) e diffe:
grep -oE '"[^"]*"$' <par>.path1.lst      | tr -d '"' | sort > /tmp/baseline.txt
grep -oE '"[^"]*"$' <par>.path1.lst-new  | tr -d '"' | sort > /tmp/atual.txt
comm -23 /tmp/baseline.txt /tmp/atual.txt        # paths que sumiram do local
```

### Passo 2 — Determine a INTENÇÃO (a árvore de decisão)

Para o conjunto que sumiu, qual dos casos se aplica?

| Caso | Sinal | O que fazer |
|---|---|---|
| **(a) Movido para fora** do folder sincronizado (de propósito) | O conteúdo agora vive em outro path local/remoto (ex.: outro folder sincronizado) | → [Branch DROP](#branch-drop--o-conteúdo-saiu-de-propósito) |
| **(b) Re-homed em repo git** com remote | Virou um repo git que `git_handling: auto` exclui (backup = GitHub, ADR-008) | → [Branch DROP](#branch-drop--o-conteúdo-saiu-de-propósito) (o cloud copy stale é redundante) |
| **(c) Deletado de fato**, mas você QUER manter sincronizando aqui | Sumiu por acidente / você quer de volta | → [Branch RESTORE](#branch-restore--o-conteúdo-deve-voltar) |

> **Confirme o backup antes de qualquer purge (C1 — data-safety by default).** Para (a)/(b), prove que o conteúdo existe em outro lugar antes de dropar do cloud:
> ```bash
> # Ex.: o conteúdo foi movido para outro folder sincronizado → confirme que chegou no cloud novo home.
> rclone lsf -R "<remote>:<novo_home>" | wc -l         # bate com a contagem local?
> find "<novo_home_local>" -type f | wc -l
> ```
> Na dúvida, **puxe uma cópia local** antes (não-destrutivo): `rclone copy "<remote>:<path_stale>" /tmp/recovery-backup/`.

### Branch DROP — o conteúdo saiu de propósito

Intenção: o conteúdo **não deve mais** viver neste folder; o cloud copy stale deve sumir. **Ordem obrigatória** (a ordem importa — ver os dois avisos abaixo):

```bash
# 1. (JÁ FEITO no Passo 2) Backup confirmado em outro lugar.

# 2. PURGE as cópias stale do cloud PRIMEIRO. (Por que primeiro: ver Aviso A.)
rclone purge "<remote>:<remote_root>/<remote_subpath>/<subpath_stale>"   # repita por dir stale

# 3. Delete o marker do par → o daemon reconstrói o baseline no próximo ciclo.
#    (Por que o marker, e não só o purge: ver Aviso B.)
python3 - <<'PY'
from drive_sync.config import load_config
from drive_sync import sync_engine as se
app = load_config()
folder = next(f for f in app.folders if f.name == "<folder>")
print(se._state_marker_for(folder.local_path, se.remote_uri_for(folder, app)))
PY
# → rm o caminho impresso.

# 4. Suba o daemon. O resync reconstrói o baseline limpo (cloud já sem os stale → sem ressurreição).
systemctl --user start drive-sync.service
systemctl --user start drive-sync-watchdog.timer
```

> **Aviso A — `--resync` é UNIÃO (superset), não sync direcional.** Se você deletar o marker e resyncar **sem** ter purgado o cloud antes, o resync copia os itens que só existem no Path2 (cloud) de volta para o Path1 (local) — **RESSUSCITANDO** o conteúdo que você moveu. É por isso que o purge do cloud vem **antes** do resync no branch DROP. (Verificado no incidente 2026-08-26: purgar antes → resync não ressuscitou.)

> **Aviso B — deletar só no cloud NÃO limpa o abort.** O safety check compara o scan de Path1 contra o baseline `.lst`, que ainda "lembra" os M itens. Enquanto o baseline não for reconstruído (via resync após deletar o marker), o abort persiste mesmo com o cloud já limpo. Purge **e** marker-delete são ambos necessários — nenhum sozinho resolve.

### Branch RESTORE — o conteúdo deve voltar

Intenção: os itens sumiram por acidente e você quer mantê-los sincronizados neste folder. Aqui a **união do resync trabalha a seu favor** — ela puxa os itens do cloud de volta para o local:

```bash
# NÃO purgue o cloud. Apenas delete o marker e deixe o resync (união) restaurar o Path1 a partir do Path2.
python3 - <<'PY'
from drive_sync.config import load_config
from drive_sync import sync_engine as se
app = load_config()
folder = next(f for f in app.folders if f.name == "<folder>")
print(se._state_marker_for(folder.local_path, se.remote_uri_for(folder, app)))
PY
# → rm o caminho impresso, depois:
systemctl --user start drive-sync.service
systemctl --user start drive-sync-watchdog.timer
```

Confira depois que os itens reapareceram em `<local_path>` e que o folder concluiu com sucesso.

---

## Verificação comum

```bash
# 1. O folder concluiu o resync com sucesso?
journalctl --user -u drive-sync --since "10 min ago" | grep "\[<folder>\]" | grep -iE "resync|sucesso|BISYNC_FAIL"
#    Espere: "Primeira sincronização — executando --resync." seguido de "bisync concluído com sucesso."

# 2. O STATUS degradou-limpou?
systemctl --user status drive-sync | grep -i "Status:"     # sem "degraded folders: <folder>"

# 3. (Branch DROP) O cloud ficou sem os stale e o local NÃO ressuscitou?
rclone lsf "<remote>:<remote_root>/<remote_subpath>/<subpath_stale>"   # vazio/ausente = ok
ls -1 "<local_path>/<subpath_stale>" 2>/dev/null                       # ausente = ok (não ressuscitou)
```

O resync pode estar enfileirado atrás do lock serializado do rclone (ADR-001) e da fila de startup — pode levar alguns minutos até a vez do folder. As notificações do watchdog param no próximo ciclo após o sucesso (serviço active, não-degradado, success marker fresco).

---

## Apêndice — como o marker controla o resync

O daemon detecta first-run por um marker próprio (`sync_engine.py:_state_marker_for` → `sync_engine.py:277`):

```
~/.cache/rclone/bisync/drive-sync.<sha1("<local>|<remote>")[:16]>.initialized
```

> **NB — não confundir com `~/.cache/drive-sync/state/<folder>.success`:** esse é o marker de **staleness/watchdog** (ADR-005), NÃO o de first-run. Deletar o `.success` **não** dispara `--resync` (o daemon roda bisync normal e reincide no abort). Só o `.initialized` acima controla o resync.

Quando o marker **não existe**, o próximo ciclo loga `Primeira sincronização — executando --resync.` e adiciona `--resync` ao comando **com os flags e excludes exatos e AO VIVO** — inclusive os excludes `git_handling`-aware calculados na classificação daquele ciclo (ADR-008). É por isso que deletar o marker é preferível a rodar `rclone bisync --resync` à mão: você não corre o risco de espelhar flags/excludes errados transcritos do journal. Ao concluir com sucesso, o daemon re-cria o marker (`marker.touch()`).
