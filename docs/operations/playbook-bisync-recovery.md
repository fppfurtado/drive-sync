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

O estado `.lst` morreu, mas os dados locais e remotos estão intactos. Recuperação = reconstruir o baseline.

**Pré-check de integridade** (confirma que é o caso benigno — as árvores batem, não há divergência real):

```bash
# Compare o top-level dos dois lados. Se baterem, o resync será um rebuild de 0 deleções.
rclone lsf --dirs-only <remote>:<remote_root>/<remote_subpath> | sort   # Path2
ls -1 <local_path> | sort                                              # Path1
```

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

Verifique a [recuperação](#verificação-comum). Se o pré-check de integridade **não** bateu (divergência real, não só estado perdido), trate como o caso rc=1 abaixo — decida a direção conscientemente.

---

## Recuperação rc=1 — too-many-deletes (perigoso)

O bisync viu >50% dos itens do baseline sumirem do Path1 (local) e travou o freio de segurança. **A recuperação depende da sua INTENÇÃO** com aqueles itens. Diagnostique primeiro; só então escolha o branch.

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

Quando o marker **não existe**, o próximo ciclo loga `Primeira sincronização — executando --resync.` e adiciona `--resync` ao comando **com os flags e excludes exatos e AO VIVO** — inclusive os excludes `git_handling`-aware calculados na classificação daquele ciclo (ADR-008). É por isso que deletar o marker é preferível a rodar `rclone bisync --resync` à mão: você não corre o risco de espelhar flags/excludes errados transcritos do journal. Ao concluir com sucesso, o daemon re-cria o marker (`marker.touch()`).
