# Playbook — flip de `git_mode` legado para `git_handling` (ADR-008)

Procedimento operacional para migrar `~/.config/drive-sync/config.yaml` do schema antigo (`git_mode: bisync|bundle|off`) para o novo (`git_handling: auto|skip|bundle|plain`) sem perder backup de repos local-only.

Referências:
- [ADR-008](../decisions/ADR-008-abandonar-bisync-repos-git.md) — decisão estrutural
- [config/config.yaml.example](../../config/config.yaml.example) — schema novo de referência

## Pré-condição

```bash
systemctl --user stop drive-sync.service
systemctl --user status drive-sync.service  # confirma "inactive (dead)"
```

Daemon parado é pré-condição obrigatória — flip envolve `rclone purge` que conflita com sync ativo.

## Snapshot opcional (recomendado)

Antes de qualquer operação destrutiva, snapshot do remote pra forensic post-incident:

```bash
mkdir -p ~/.local/state/drive-sync/snapshots
rclone lsl proton:Sync/ > ~/.local/state/drive-sync/snapshots/pre-flip-$(date +%F).txt
```

## Ordem segura — folder com repo local-only (caso `dev-scripts`)

`dev-scripts` é o caso real onde `folder.local_path` é ele próprio um repo git **sem remote**. Bundle dele é o único backup cloud. Ordem invertida deixaria janela onde nenhuma cópia cloud existe — **NÃO inverter**.

```bash
# 1. Editar config para git_handling: bundle ANTES de qualquer purge.
$EDITOR ~/.config/drive-sync/config.yaml
# Trocar:
#   - name: dev-scripts
#     git_mode: bisync          → git_handling: bundle
# Adicionar (recomendado): cooldown_seconds: 10800

# 2. Validar schema novo.
.venv/bin/python -m drive_sync --check
# Output esperado: "Configuração OK." sem ValueError.

# 3. Restart daemon.
systemctl --user start drive-sync.service

# 4. Observar primeiro ciclo de bundle subir com sucesso.
journalctl --user -u drive-sync -f
# Aguardar entry: "[dev-scripts] Bundle gerado: ..." e "Bundle sincronizado para nuvem"

# 5. Confirmar bundle no Proton.
rclone lsl proton:Sync/dev/scripts/ | grep gitbundle
# Output esperado: arquivo .gitbundle com mtime recente.

# 6. Apenas agora purgar resquícios bisync (worktree antigo no remote).
#    O bundle uploaded acima fica intacto; o purge limpa o conteúdo bisync
#    que não está mais sendo sincronizado.
rclone purge proton:Sync/dev/scripts/
#    ATENÇÃO: rclone purge é recursivo. O bundle SOBE DE NOVO no próximo ciclo
#    (daemon detecta ausência e regenera). Aguarde 1 ciclo periodic antes de
#    declarar concluído.
```

## Ordem para folders sem repo local-only

Folders cujos repos todos têm remote no GitHub (`dev-projects`, `dotfiles`, `notes/logseq`) — purge direto antes da edição é seguro (GitHub é o backup).

```bash
# 1. Purgar paths bisync-only.
rclone purge proton:Sync/dev/projects/
rclone purge proton:Sync/dotfiles/
rclone purge proton:Sync/notes/logseq/

# 2. Invalidar markers bisync do drive-sync (PASSO CRÍTICO).
#    Sem isso, daemon não dispara `--resync` no próximo ciclo e rclone bisync
#    abortará com "Safety abort: too many deletes" (estado interno aponta para
#    arquivos que estavam no remote pré-purge). Incidente real 2026-06-02 em
#    dev-projects (31581 of 31608 deletes).
#    Caminho mais simples: deletar TODOS os markers (daemon recria; --resync
#    em folders plain não-afetados é idempotente em estado consistente —
#    apenas re-cria state files sem transferir).
rm -v ~/.cache/rclone/bisync/drive-sync.*.initialized
#    Caminho cirúrgico (só os 4 folders afetados pelo flip — evita --resync
#    em plain folders): computar sha1(local|remote)[:16] por folder. Helper
#    Python em scripts/ ou one-liner inline.

# 3. Editar config.
$EDITOR ~/.config/drive-sync/config.yaml
# Trocar todas as entries:
#   git_mode: bisync   → git_handling: auto
#   git_mode: "off"    → git_handling: "plain"
#   git_mode: bundle   → git_handling: bundle
# (sed -i pode acelerar — backup do arquivo antes)

# 4. Validar.
.venv/bin/python -m drive_sync --check

# 5. Restart.
systemctl --user start drive-sync.service
```

### Para o folder com repo local-only (sequência da seção acima)

Adicionar passo entre `rclone purge proton:Sync/dev/scripts/` (step 6) e fechamento: invalidar marker do dev-scripts especificamente (sha1 do `local|remote` ou nuke total como acima).

## Renomeio em massa `off` → `plain`

11 folders não-git no exemplo declaram `git_mode: "off"` (Documents, Pictures/*, library, videos, etc.). Renomeio mecânico:

```bash
cp ~/.config/drive-sync/config.yaml ~/.config/drive-sync/config.yaml.bak
# Regex cobre as formas YAML reais: aspas duplas, simples, sem aspas, com
# espaços variáveis. YAML 1.1 interpreta `off` sem aspas como booleano false,
# então configs históricas costumam usar `"off"` ou `'off'`.
sed -i -E 's/git_mode:[[:space:]]+["'\'']?off["'\'']?/git_handling: "plain"/g' ~/.config/drive-sync/config.yaml
sed -i -E 's/git_mode:[[:space:]]+["'\'']?bundle["'\'']?/git_handling: "bundle"/g' ~/.config/drive-sync/config.yaml
sed -i -E 's/git_mode:[[:space:]]+["'\'']?bisync["'\'']?/git_handling: "auto"/g' ~/.config/drive-sync/config.yaml
# Conferir resultado:
grep -n "git_mode\|git_handling" ~/.config/drive-sync/config.yaml
# Output esperado: ZERO ocorrências de "git_mode"; todas migradas para "git_handling".
# Caso reste algum git_mode (forma não coberta pela regex), editar manual e re-rodar.
```

`drive-sync --check` rejeita qualquer `git_mode` remanescente com hint citando este playbook.

## Confirmação pós-flip

```bash
# 1. Status snapshot — folders ativos, sem degraded.
.venv/bin/python -m drive_sync --status

# 2. Aguardar 1 ciclo periodic_full_sync (default 3600s ≈ 1h).
sleep 3700

# 3. Re-verificar status — todos folders devem estar verdes.
.venv/bin/python -m drive_sync --status

# 4. Confirmar bundles esperados localmente.
find ~/.cache/drive-sync/bundles/ -name '*.gitbundle' -mtime -1

# 5. Confirmar notify-send NÃO disparou flip falso-positivo
#    (primeira classificação pós-restart é silenciosa).
journalctl --user -u drive-sync --since "1 hour ago" --grep "REPO_MODE_FLIP"
# Output esperado: vazio (sem flips na primeira janela).
```

## Recovery se boot falhar

```bash
# Caso `drive-sync --check` falhe ou o daemon não suba:

# 1. Restaurar backup do config.
cp ~/.config/drive-sync/config.yaml.bak ~/.config/drive-sync/config.yaml

# 2. EDITAR para git_handling (sem coerce, falha-fast simétrica).
#    Não há rollback para git_mode — schema antigo é rejeitado pelo loader.

# 3. Caso folder local-only fique sem backup cloud pós-purge:
#    Gerar bundle manual ANTES de retentar:
git -C /storage/dev/scripts bundle create \
  ~/.cache/drive-sync/bundles/dev-scripts/dev-scripts.gitbundle --all
# Daemon detecta o bundle no próximo ciclo e mantém sincronizado.
```

## Cross-refs

- Falha-fast simétrica e mapping `bisync→auto / bundle→bundle / off→plain` documentados em ADR-008 §Decisão.
- Trade-off WIP-cross-device (perda do default bisync que documentava "syncs uncommitted changes") em ADR-008 §Trade-offs.
- Proxy `git remote -v` falsificável: log enriquecido `[REPO_SKIP] <repo> (has_remote: <url>)` + override via `repo_overrides` em ADR-008 §Limitações.
