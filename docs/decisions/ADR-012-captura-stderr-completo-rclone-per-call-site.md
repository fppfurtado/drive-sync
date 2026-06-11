# ADR-012: Captura completa de stderr per call-site em `~/.local/state/drive-sync/`

**Data:** 2026-06-11
**Status:** Proposto

## Origem

- **Investigação:** /debug 2026-06-11 do incidente archive 2026-06-10 — Proton API retornou `500 GET /drive/shares/.../links/...: Internal server error (Code=500, Status=500)` durante bisync. Side-effect: state corruption (`.path[12].lst` renomeados para `.lst-err`) demandando `rclone bisync ... --resync` manual. Daemon logou `[archive] bisync falhou (rc=7):  exist?` por ~21h (tail-truncate de `err.strip()[-500:]` em `sync_engine.py:241`); o erro real `Bisync critical error: 500 GET ...` ficou oculto. Diagnóstico levou ~30min de cross-ref entre `.lst-err` mtimes + journal pra reconstituir causa-raiz, porque o stderr completo só existia no journal em janela limitada.
- **Investigação anterior:** incidente library 2026-05-31 (dev-projects) — `[dev-projects] bisync falhou (rc=1): s` (uma letra do `Safety abort: too many deletes (>50%, 68412 of 104018) on Path1`). Mesmo gap, repetido.
- **Decisão base:** [ADR-005](ADR-005-folder-staleness-degraded.md) registra gap observacional explícito (`folder degraded reporta 'sem sucesso há Xh' mas não diz POR QUÊ`). Este ADR fecha esse gap.
- **Decisão base:** [ADR-003](ADR-003-type-notify-sinalizacao-degraded.md) estabeleceu convenção de tags `[AUTH_DEGRADED]` + `STATUS=` para discriminação operacional via `journalctl --grep`. Este ADR estende a convenção para falhas bisync/bundle/mkdir.

## Contexto

`drive_sync/sync_engine.py` trunca stderr de rclone em 4 call-sites de log de erro: `err.strip()[-300:]` ou `err.strip()[-500:]` em linhas 181 (`_ensure_remote_dir`), 241 (`bisync_folder`), 264 (`upload_bundle`), 287 (`download_bundle`). rclone emite stderr longo com mix de notices/warnings/errors; os últimos N chars raramente preservam a primeira linha `ERROR:` que carrega a causa-raiz.

Consequência operacional:

- **Diagnóstico custoso pós-incidente:** /debug de 2026-06-11 levou 30min de cross-ref para reconstituir um erro que `cat ~/.local/state/drive-sync/last-stderr-bisync-archive.log` resolveria em segundos.
- **Defesa em profundidade rompida:** ADR-005 sinaliza degraded mas o canal de log não diz POR QUÊ; operador depende de inspeção manual do `journalctl --user -u drive-sync` com janela temporal correta + grep heurístico.
- **Padrão recorrente:** 2 incidentes em ~10 dias com sintoma idêntico (incidente 2026-05-31 + incidente 2026-06-10) — gap não é eventualidade isolada.

Restrições relevantes:

- Subprocess `rclone` é serializado por ADR-001 (lock global); custo de I/O extra per call-site é desprezível.
- Estado persistente vive em `~/.local/state/drive-sync/` (XDG state), onde `drive-sync.log` já é escrito; sem nova permissão systemd a configurar.
- Convenção de tags `[AUTH_DEGRADED]` / `[FOLDER_DEGRADED]` já estabelecida (ADR-003, ADR-005) — nova surface deve seguir uniformidade.
- Filesystem name sanitization é fronteira: `folder.name` e `rel_subpath` vêm de input do operador (config.yaml) e scan de FS — podem conter `/`, espaços, Unicode arbitrário.

## Decisão

Persistir o stderr completo de cada call-site de erro do rclone em um arquivo dedicado por operação, com retenção de 1 run (overwrite), e logar a primeira linha `ERROR:` extraída como sumário no journal:

1. **Schema de arquivo:** `~/.local/state/drive-sync/last-stderr-<op>[-<sub_slug>]-<folder_slug>.log`, onde:
   - `<op>` é uma das 4: `bisync`, `mkdir`, `upload-bundle`, `download-bundle`.
   - `<folder_slug>` é `re.sub(r"[^A-Za-z0-9._-]", "_", folder.name)` — previne path traversal e nomes inválidos no FS.
   - `<sub_slug>` aparece apenas para `upload-bundle` e `download-bundle` (chave per-(folder, rel_subpath)); bundle itera N sub-repos por folder, chave per-folder pura sobrescreveria a cada repo perdendo o mais informativo.
   - Diretório base resolve via `os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")` + `/drive-sync/`; mkdir idempotente.

2. **Retenção = 1 (overwrite):** próximo run sobrescreve o arquivo anterior. Justificativa: rclone bisync opera sob invariante CLAUDE.md "bisync errors do NOT auto-recover" — após falha, próxima tentativa só acontece em resposta a watcher event ou ciclo `periodic_full_sync_seconds` (default 3600s = 1h); janela de inspeção do operador é suficiente.

3. **Tags de log uniformes** com convenção ADR-003/ADR-005, uma por call-site:
   - `[BISYNC_FAIL]`
   - `[BUNDLE_UPLOAD_FAIL]`
   - `[BUNDLE_DOWNLOAD_FAIL]`
   - `[MKDIR_FAIL]`
   Formato canonical de log: `[<folder>] [<TAG>] <summary> (full stderr: <path>)`. `journalctl --user -u drive-sync --grep "BISYNC_FAIL"` vira primary investigation tool sem conhecimento out-of-band do schema de arquivos.

4. **Sumário no log = primeira linha `ERROR:`:** regex `^.*ERROR\s*:\s*(.+)$` (multiline). Quando o stderr não contém linha `ERROR:` (cenário raro: rclone abort prematuro), fallback ao tail-truncate atual (`err.strip()[-500:]`) preserva comportamento legado e zero regressão observacional.

5. **Out-of-scope desta decisão:**
   - Linha 92 (`AuthDegradedError.stderr_tail` em `_classify_rclone_stderr`) — payload de exceção, semântica distinta; cobertura exigiria refactor do consumer downstream (ADR-003). Deferred.
   - Integração com `drive-sync --status` — `cat ~/.local/state/drive-sync/last-stderr-*.log` é forense direta suficiente; UI hook seria entrada de backlog própria.

## Consequências

### Benefícios

- **Diagnóstico em segundos para o caso comum:** operador executa `cat ~/.local/state/drive-sync/last-stderr-bisync-<folder_slug>.log` e tem stderr completo do último incidente. Cross-ref multi-fonte (mtimes + journal + state cache) deixa de ser necessária.
- **Discoverability sem conhecimento out-of-band:** journal mostra `(full stderr: <path>)` literalmente — operador grep `journalctl --grep "BISYNC_FAIL"` descobre o canal naturalmente.
- **Uniformidade com ADR-003/ADR-005:** convenção de tags `[<DOMAIN>_<STATE>]` fica consistente; mecânica de investigação operacional é uma só.
- **Defesa em profundidade real para ADR-005:** "sem sucesso há Xh" agora tem cross-ref ao arquivo com o "POR QUÊ" — gap fechado mecanicamente.

### Trade-offs

- **Retenção = 1 perde histórico de rebound.** Se incidente intermitente flaky a cada ciclo emergir como caso real (Proton 500 piscando em alta cadência sobrescrevendo stderr da falha original), forense fica limitada ao último run. Mitigação: gatilho de revisão registrado abaixo; reabertura para rotação `.1/.2/.3` é forward-compat (schema overwrite continua sendo o caso default; sufixos numéricos seriam opcionais).
- **Custo de I/O extra per call-site falho:** stderr pode chegar a MBs (rclone verbose com notices + warnings); um write síncrono por call-site falho. Desprezível dado que: (a) `rclone` já é serializado (ADR-001), (b) escrita só acontece em path de erro (raro vs success path), (c) arquivos são local (não rede).
- **Multiplicação de arquivos em folder bundle multi-repo:** folder bundle com N sub-repos pode acumular até N × 2 arquivos `last-stderr-upload-bundle-*` + `last-stderr-download-bundle-*` simultaneamente. Para N=30 (tjpa/pje-2.1 não chega perto mas é o caso real bundle), isso é 60 arquivos no diretório — operacionalmente trivial mas ruidoso em `ls`. Mitigação: granularidade per-(folder, rel_subpath) é necessária para evitar sobrescrita mútua (o problema que motivou esta decisão); o ruído é o preço da granularidade.

### Limitações

- Stderr de operações que NÃO chegam ao subprocess rclone (ex.: erro em `_classify_rclone_stderr` raise antes de logar; erro de I/O ao escrever o próprio arquivo de captura) não é capturado por esta decisão. Cobertura é dos 4 call-sites de `log.error("...", err.strip()[-N:])` listados, não exaustiva do daemon.
- Convenção de tags `[<DOMAIN>_<STATE>]` se torna implícita-quasi-canonical; futuros call-sites (ex.: probe `auth_probe`) precisarão adotar o mesmo padrão sem documento canonical único listando todas as tags. Trade-off aceito: doc-as-needed > doc-as-spec ex-ante.

### Mitigações

- Tests dedicados (Bloco 2 do plano `sync-engine-full-stderr-capture`) cobrem: sanitização, overwrite semantics, fallback quando sem `ERROR:`, XDG_STATE_HOME, granularidade per-sub_slug em bundle.
- `## Gatilhos de revisão` abaixo registra condição clara para reabertura (rotação multi-run).

## Alternativas consideradas

### A1: Retenção rotacionada `.1/.2/.3` desde o início

Manter N=3 ou N=5 runs por chave; cada novo run promove `.log → .1 → .2 → .3` (descartando .3). Vantagem: preserva sequência de falhas correlacionadas; cobre cenário rebound. Descarte: complexidade desproporcional dado que invariante `bisync errors do NOT auto-recover` + cadência mínima ≥ 1h do `periodic_full_sync_seconds` + cooldown opt-in (ADR-004) tipicamente ≥ 1h em folders configurados (default 10800s = 3h em dev-projects) somam pisos múltiplos tornando o cenário rebound improvável; YAGNI até evidência empírica. Gatilho de reabertura preservado.

### A2: Cobertura também da linha 92 (`AuthDegradedError.stderr_tail`)

Aplicar o mesmo schema ao payload da `AuthDegradedError`. Descarte: semântica diferente — payload de exceção é consumido por daemon downstream (Notifier per ADR-003), não é log call direto. Refactor exigiria mudança no consumer e revisita ADR-003 § Decisão. Eixo independente, deferred para entrada de backlog própria se surgir caso de uso.

### A3: Logging estruturado (JSON) em vez de plain stderr

Escrever payload JSON com campos `{timestamp, op, folder, rc, first_error, stderr_full}`. Vantagem: queryável via `jq`; integra com tooling de observabilidade futura. Descarte: overhead conceptual (esquema, evolução de schema, migração) para benefício marginal dado que `cat` + `grep` resolvem o caso comum; plain stderr alinha com `journalctl` (operador já usa). Forward compat: arquivo plain pode virar JSON via novo `last-stderr-bisync-*.json` paralelo se demanda emergir.

### A4: Não introduzir tags `[<DOMAIN>_<STATE>]`

Manter formato livre `[<folder>] bisync falhou (rc=%d): %s`, descobrindo arquivo por path convention. Descarte: rompe uniformidade com ADR-003/ADR-005; `journalctl --grep "BISYNC_FAIL"` não funcionaria; operador precisaria conhecer schema de path para grep heurístico (gap parcialmente reintroduzido). Custo de uniformidade é negligenciável.

## Gatilhos de revisão

- **≥1 incidente onde rebound apaga stderr útil antes de inspeção:** cenário onde flakiness intermitente (Proton 500 piscando a cada ciclo, falha persistente sobrescrevendo registro original em < 1h) confirme limitação real da retenção = 1. Reabrir para rotação `.1/.2/.3` (alternativa A1 absorvida).
- **Demanda por consulta queryável estruturada:** tooling externo de observabilidade (Grafana, Loki) exigindo schema estável → reabrir para A3 (JSON).
- **Cobertura de `AuthDegradedError.stderr_tail` viraria requisito:** novo incidente onde tail-truncate em payload de exceção comprometa diagnóstico de auth. Reabrir para A2 + revisita de ADR-003.
- **Nova convenção de tags canonical:** se ≥1 outro ADR introduzir esquema concorrente de prefixos de log (ex.: `[SEVERITY]:`), consolidar em ADR transversal de logging conventions.

## Implementação

<!-- Preenchido pós-implementação pelo /run-plan -->

- Plano: `.claude/local/plans/sync-engine-full-stderr-capture.md` (modo local per ADR-047).
