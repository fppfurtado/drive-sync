# Plano — CLI `drive-sync --status` (snapshot observacional)

## Contexto

Hoje a única forma de ver o que o daemon está fazendo é `journalctl --user -u drive-sync -f`, que mistura ruído de runtime e não dá visão agregada por pasta. Modos de falha como o loop de restart de 18h descrito em [BACKLOG.md](../../BACKLOG.md) (linha "install: investigar regressão do symlink") e a regressão de auth Proton (linha "daemon: health-check de auth") passaram despercebidos por horas/dias — observabilidade passiva é defesa em profundidade complementar à sinalização ativa (sd_notify/notify-send) já no backlog.

A triage decidiu (em ordem de bifurcação):

1. **Forma de interação**: CLI one-shot `drive-sync --status` (não TUI live). Scriptable, sem refresh, fits Unix philosophy. Permite evoluir para TUI depois reaproveitando o renderizador.
2. **Fonte da verdade**: markers do bisync (`~/.cache/rclone/bisync/`) + `config.yaml`. Zero acoplamento ao daemon. Limitação aceita: sem visão de fila/inflight/erros — exigiria log parsing (formato vira API de fato) ou IPC (mudança arquitetural no daemon, mereceria ADR). Ambos descartados para v1.
3. **Framework**: stdlib only (formatação manual com `str.ljust`). Adicionar Rich/Textual seria overkill para um snapshot tabular de 3 colunas.

**Linha do backlog:** CLI `drive-sync --status` para visibilidade observacional do daemon — snapshot one-shot lendo markers do bisync (`~/.cache/rclone/bisync/`) e `config.yaml` para mostrar pastas configuradas, última sincronização por pasta e estado de inicialização. TUI live, controle (forçar sync/pausar pasta) e expor estado in-memory via IPC ficam como follow-ups separados.

## Resumo da mudança

Adiciona o flag `--status` ao entry-point existente (`drive-sync`/`python -m drive_sync`), mutuamente exclusivo com `--check`/`--once`. Quando acionado, carrega o `config.yaml`, varre `~/.cache/rclone/bisync/` e imprime uma tabela com:

- **Folder** — `local_path` da pasta configurada (com `~` re-contraído para legibilidade).
- **Initialized** — `yes`/`no`, baseado na presença do marker `drive-sync.<sha1(local|remote)[:16]>.initialized` (mesmo hash usado em `sync_engine._state_marker_for`).
- **Last sync** — mtime mais recente entre os arquivos `.lst`/`.lst-new` (path1 e path2) que matcheiem o local sanitizado, formatado como `YYYY-MM-DD HH:MM` ou `never` se nenhum match.
- **Remote** — URI rclone derivada via `remote_uri_for` (já existe).

Sai com exit code 0 sempre que conseguir ler config + listar diretório de markers (ausência do diretório não é erro — significa "nada sincronizado ainda"). Falha de config retorna 1 com mensagem clara (mesma rota de `--check`).

Fora do escopo (ficam como follow-ups separados):
- Fila do daemon, inflight, erros recentes (exigiriam log parsing ou IPC).
- Refresh live / TUI Textual.
- Output JSON / scriptabilidade estruturada.
- Ações de controle (forçar sync, pausar pasta).

## Arquivos a alterar

### Bloco 1 — módulo de status {reviewer: code}

- `drive_sync/status.py` (novo): expõe `render_status(cfg: AppConfig, bisync_dir: Path | None = None) -> str`. Resolve `bisync_dir` default para `~/.cache/rclone/bisync/`. Para cada `FolderConfig` enabled, computa o hash do marker duplicando inline as 2 linhas de `sync_engine._state_marker_for` (não importar private; só extrair para módulo compartilhado quando aparecer um 3º caller). Sanitiza `local_path` no mesmo formato dos `.lst` do rclone (substituir `/` por `_`, prefixar com path absoluto sanitizado) e procura por mtimes. Formata tabela com `str.ljust` (sem dependência nova). Primeira linha do output é um header de instabilidade fixo: `# drive-sync status v1 — formato textual não-estável, use --json quando disponível`. Retorna string pronta para `print`.

### Bloco 2 — wiring no entry-point {reviewer: code}

- `drive_sync/__main__.py`: adicionar `--status` no parser num grupo mutuamente exclusivo com `--check`/`--once`. Quando acionado, chama `load_config`, depois `render_status(cfg)`, imprime e retorna 0. Caminho não cria daemon, não toca event loop — análogo ao `--check`.

### Bloco 3 — testes {reviewer: qa}

- `tests/test_status.py` (novo): cobrir
  - cenário "pasta com `.lst` recente" — fixture com tmp_path como bisync_dir, criar arquivos `.lst` com mtime conhecido, asserir que aparece formatado.
  - cenário "pasta sem `.lst`" — coluna `Last sync` = `never`.
  - cenário "marker `.initialized` presente vs. ausente" — coluna `Initialized` reflete corretamente.
  - cenário "bisync_dir inexistente" — não crasha; tudo `never` / `no`.
  - cenário "match de path com caracteres especiais" — pasta com espaço/acento no `local_path` casa com o `.lst` correspondente (forma real do dado: nomes tipo `home_fppfurtado_..proton_3._Resources_Projects_cnj.path1.lst`).
  - cenário "header de instabilidade" — primeira linha do output começa com `# drive-sync status v1` (asserção textual simples).

### Bloco 4 — doc no README {reviewer: doc}

- `README.md`: acrescentar `drive-sync --status` na seção de uso (ao lado de `--check` e `--once`), com 1 exemplo de saída. Limitar a 3-5 linhas — não duplicar o que está neste plano.

## Verificação end-to-end

- `python -m pytest tests/ -v` passa (incluindo `test_status.py` novo).
- `.venv/bin/drive-sync --status` no ambiente real do operador imprime tabela com as pastas configuradas em `config.yaml` e exit code 0.
- `.venv/bin/drive-sync --status --check` (combinação inválida) sai com erro de argparse e exit code != 0.

## Verificação manual

Rodar contra o ambiente real do operador (que tem markers acumulados de uso de produção):

1. `.venv/bin/drive-sync --status` — confirmar que **todas** as pastas de `~/.config/drive-sync/config.yaml` aparecem; pelo menos 1 com `Last sync` recente (minutos/horas) e pelo menos 1 com timestamp antigo (meses) — corresponder visualmente com `ls -lt ~/.cache/rclone/bisync/ | head`.
2. Renomear temporariamente `~/.cache/rclone/bisync/` (ou apontar para tmpdir vazio via env var de teste, se introduzida) e rodar de novo — esperar `Last sync = never` e `Initialized = no` em todas, sem traceback.
3. Rodar com `--config /caminho/inexistente.yaml` — esperar mensagem clara (não traceback nu) e exit != 0.
4. Inspecionar visualmente o alinhamento das colunas com pelo menos uma pasta de path longo (ex.: `~/Projects/h3/gestaoclick-report_react`) — colunas devem permanecer alinhadas.

## Notas operacionais

- Bloco 1 antes de Bloco 2 (entry-point depende do módulo). Bloco 3 pode ir em paralelo com 2 ou logo após 1. Bloco 4 (doc) por último.
- Sanitização de path do rclone bisync é a parte mais frágil — encoding exato depende da versão do rclone. Se o matching falhar contra dados reais, fallback aceitável: listar todas as `.lst` e fazer best-effort textual em vez de match exato. Decidir no momento se o problema aparecer.
- Header de instabilidade na primeira linha do output (`# drive-sync status v1 — formato textual não-estável, use --json quando disponível`) é deliberado para sinalizar que `awk`/`grep` sobre o output é uso por sua conta e risco. Quebra futura do layout quando `--json` chegar fica justificada pelo aviso prévio.
