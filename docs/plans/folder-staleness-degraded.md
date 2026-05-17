# Plano — Sinalizar pasta degradada por staleness (>12h sem sucesso)

## Contexto

Em 2026-05-10 10:26:56 o daemon registrou abortagem do `rclone bisync` para `dev-projects` com `march failed with 34 error(s): first error: directory not found` — flakiness transitória do backend protondrive, não-auth. Rclone marcou o estado de trabalho como `Must run --resync to recover`; o daemon honrou o invariante consciente `bisync errors do NOT auto-recover` ([sync_engine.py:134-137](../../drive_sync/sync_engine.py)) e seguiu. A pasta ficou **7 dias parada** sem percepção, descoberta só por inspeção manual de `journalctl` em 2026-05-17.

Lacuna estrutural: [ADR-003](../decisions/ADR-003-type-notify-sinalizacao-degraded.md) cobre apenas falha de auth identificada (`Code=8002`/`Code=9001` no endpoint `/api/auth/v4`). Os outros 4 eventos `march failed` registrados no mês caíram nesse classificador (`Code=10013`). O de 10 de maio é o caso fora — primeira mensagem `directory not found`, não auth — e por isso passou invisível.

A causa-raiz do abort original é externa (backend protondrive); rclone bisync por desenho aborta a operação inteira ao primeiro erro de listagem (*"too dangerous to continue"*) e exige `--resync` manual. Não há defesa direta dentro do daemon contra a abortagem em si. A defesa viável e barata é **observabilidade**: transformar "pasta parada por dias sem ninguém perceber" em "pasta sinalizada em STATUS em até ~12h".

**ADRs candidatos:** ADR-003 (estende — novo gatilho temporal e novo escopo per-folder), ADR-004 (alinhamento de pattern in-memory cross-restart).

**Linha do backlog:** daemon: sinalizar pasta degradada após >12h sem sincronização bem-sucedida — estender ADR-003 (hoje cobre só auth Code=8002/9001) com gatilho temporal per-folder, sem pausa global. Cobre falhas bisync genéricas (ex.: `march failed: directory not found` em flakiness do protondrive) que hoje passam silenciosas até inspeção manual de journalctl. Sinalização tripla: `STATUS=degraded folders: <list>` (sd_notify), `notify-send`, log `[FOLDER_DEGRADED]`. Reset por sucesso da pasta. Incidente: dev-projects 7 dias parado em maio 2026.

## Resumo da mudança

**Entra:**
- Estado per-folder no `SyncDaemon`: `_last_successful_sync_at: dict[str, float]` (distinto de `_last_sync_at` que é from-start por ADR-004) e `_degraded_folders: dict[str, str]` (nome → razão curta).
- Detecção temporal: pasta sem sucesso há > `folder_staleness_threshold_seconds` (default 43200 = 12h) vira degraded.
- Sinalização tripla per-folder (paralela a ADR-003, sem pausar workers): log CRITICAL com tag `[FOLDER_DEGRADED] <folder>: <reason>`, `notify-send`, e STATUS agregada `degraded folders: dev-projects (>12h), library (>14h)`.
- Reset automático ao primeiro sucesso da pasta (não-notificado — silêncio é a recuperação).
- Detecção rodada como **piggyback no `_periodic_full_sync`**: antes de enfileirar cada folder, avalia staleness e dispara sinalização se cruzou o limiar. Sem novo loop dedicado.
- Linha de config opcional em `daemon:`: `folder_staleness_threshold_seconds` (default 43200, opt-out via 0).
- Testes cobrindo: detecção quando cruza limiar, idempotência (não re-notifica), reset por sucesso, STATUS agregada com múltiplas pastas degradadas, opt-out via threshold=0.

**Fica de fora:**
- Pausa de workers per-folder (escolha explícita: outras pastas seguem; per-folder sem pausa global confirmado no /triage). Auth continua usando ADR-003 com pausa global — a serialização é apropriada para auth porque afeta todas as pastas.
- Auto-`--resync`. Conflitaria com invariante `bisync errors do NOT auto-recover`. Recuperação continua manual.
- Persistência cross-restart do contador de staleness. In-memory, alinhado com ADR-004 (cooldown). Trade-off: restart limpa o degraded; redetecção ocorre dentro do próximo `_periodic_full_sync` (a janela `now - daemon_start_time > threshold` é avaliada usando wall-clock, então restart não reseta a janela em si — só re-avalia).
- Canal alternativo headless além de sd_notify (já basta — visível em `systemctl --user status`).
- Threshold per-folder. Único global cobre o caso atual; per-folder fica como follow-up se cooldown_seconds longos justificarem.

## Arquivos a alterar

### Bloco 1 — config: campo `folder_staleness_threshold_seconds` + validação de dependência {reviewer: code}

- `drive_sync/config.py`:
  - Adicionar campo `folder_staleness_threshold_seconds: int = 43200` na dataclass de `daemon:` (ou onde `watcher.periodic_full_sync_seconds` mora). Carregar de `cfg.get("daemon", {}).get("folder_staleness_threshold_seconds", 43200)` com `0` como opt-out semântico.
  - Validar (`>=0`).
  - **Validar dependência cross-campo (ADR-005):** se `folder_staleness_threshold_seconds > 0`, exigir `watcher.periodic_full_sync_seconds > 0`. Combinação inválida → `ValueError` com mensagem `"folder_staleness_threshold_seconds > 0 requer watcher.periodic_full_sync_seconds > 0 (detecção piggyback no loop periódico — ADR-005). Defina staleness=0 para opt-out, ou habilite periodic full-sync."`. Validação roda no `_validate` ou no construtor de `AppConfig`, seguindo o pattern dos validators existentes.
- `config/config.yaml` (reference): documentar a chave com comentário explicando default 12h, semântica per-folder, opt-out via 0, **dependência de `watcher.periodic_full_sync_seconds > 0`**, e referência a ADR-005.

### Bloco 2 — daemon: estado + detecção + sinalização per-folder {reviewer: code}

- `drive_sync/daemon.py`:
  - `SyncDaemon.__init__`: novos campos `self._last_successful_sync_at: dict[str, float] = {}` e `self._degraded_folders: dict[str, str] = {}`. Reuso de `monotonic()` ou wall-clock — decidir no bloco; recomendado wall-clock (`time.time()`) porque o threshold é semântico de "horas reais sem sucesso", não tempo monotônico de processo.
  - `_process_folder`: ao retornar `True` (sucesso), setar `_last_successful_sync_at[folder.name] = time.time()`. Se folder estava em `_degraded_folders`, removê-lo e re-emitir STATUS agregada (sem `notify-send` — recuperação é silenciosa).
  - Novo método privado `_check_folder_staleness()`: para cada folder configurado, avalia `(now - _last_successful_sync_at.get(folder.name, daemon_start_time)) > threshold`. Se sim e não está em `_degraded_folders`, adiciona e dispara `self._notifier.folder_degraded(folder.name, reason)` + recomputa STATUS agregada.
  - `_periodic_full_sync`: chamar `_check_folder_staleness()` no início de cada ciclo (antes de enfileirar). Threshold=0 → método retorna no-op imediatamente.
  - Capturar `time.time()` em `__init__` como `self._daemon_start_time` (baseline para folders nunca sincronizados).

### Bloco 3 — notifier: método folder_degraded; daemon compõe STATUS agregada {reviewer: code}

Decisão (ADR-005): preservar Notifier stateless. Composição da STATUS final vive no daemon, que já é o estado-holder natural (`_degraded_reason` para auth, `_degraded_folders` para staleness).

- `drive_sync/notifier.py`:
  - Novo método `folder_degraded(self, folder: str, reason: str)`: log CRITICAL `[FOLDER_DEGRADED] %s: %s`, dispara `notify-send` (best-effort). **Não** chama `_systemd_notify` — STATUS é composta no daemon.
  - **Não** ganha estado interno. `Notifier.degraded(reason)` (ADR-003) também permanece fire-and-forget.
  - Tornar `_systemd_notify` acessível ao daemon (já é privado por convenção `_`; documentar no docstring que daemon pode chamar para STATUS composta, ou expor método público `send_status(payload: str)` se preferir contrato explícito — escolher no review do bloco).
- `drive_sync/daemon.py`:
  - Novo método privado `_compose_status_payload() -> str`: combina estado auth + estado folder com precedência `auth > folder`. Pseudocódigo:
    - Se `self._degraded.is_set()`: retorna `f"STATUS=degraded: {self._degraded_reason}"` (idêntico ao formato atual de ADR-003).
    - Senão se `self._degraded_folders`: retorna `f"STATUS=degraded folders: {<lista ordenada alfabeticamente por folder name>}"`.
    - Caso contrário: retorna `"STATUS="` (limpa).
  - Chamar `self._notifier._systemd_notify(self._compose_status_payload())` (ou `send_status(...)` se o método público for adicionado) após cada mudança em `_degraded_folders` (entrada nova ou remoção por recuperação). Auth-degraded já chama via `Notifier.degraded` em ADR-003 — esse caminho continua, mas pode ser refatorado pra usar `_compose_status_payload` se a refatoração ficar pequena (avaliar no review).
  - Ordem alfabética estável da lista folder evita flicker na STATUS quando o set muda de membro.

### Bloco 4 — testes {reviewer: qa}

- `tests/test_daemon_staleness.py` (novo):
  - Teste: pasta com `_last_successful_sync_at` antigo (>threshold) entra em `_degraded_folders` na próxima `_check_folder_staleness`.
  - Teste: pasta nunca sincronizada e `daemon_start_time` antigo (>threshold) também entra. Pasta nova (daemon recém-iniciado) NÃO entra.
  - Teste: idempotência — chamar `_check_folder_staleness` 2x não dispara `folder_degraded` 2x.
  - Teste: sucesso de uma pasta em `_degraded_folders` remove a entry, re-emite STATUS agregada, NÃO chama `notify-send`.
  - Teste: STATUS agregada com 2+ pastas degradadas formata corretamente (ordem estável — por nome alfabético, p.ex.).
  - Teste: opt-out — `folder_staleness_threshold_seconds=0` faz `_check_folder_staleness` ser no-op.
  - Mocks: `Notifier` é mock; `time.time()` controlado via monkeypatch ou `freezegun` (verificar se está em deps; se não, monkeypatch direto é suficiente).
- `tests/test_notifier.py` (existente, estender):
  - Teste: `folder_degraded(folder, reason)` loga CRITICAL com tag `[FOLDER_DEGRADED]` e chama `_notify_send` com summary/body esperados. Confirma que **não** dispara `_systemd_notify` (STATUS é responsabilidade do daemon).
- `tests/test_daemon_staleness.py` (composição de STATUS — moves para cá pra ficar junto do estado-holder):
  - Teste: `_compose_status_payload()` com `_degraded.is_set()=True` retorna payload auth (formato idêntico a ADR-003), mesmo com `_degraded_folders` não-vazio.
  - Teste: `_compose_status_payload()` com `_degraded` limpo e `_degraded_folders={"b":"...","a":"..."}` retorna folders em ordem alfabética estável.
  - Teste: `_compose_status_payload()` com ambos vazios retorna `"STATUS="`.

### Bloco 5 — docs: ADR-005, CLAUDE.md invariante, README {reviewer: doc}

- `docs/decisions/ADR-005-*.md`: criado pelo `/new-adr` na fase de produção do /triage. Captura: extensão de ADR-003 (gatilho temporal per-folder), recusa de pausa per-folder, justificativa do default 12h, gatilhos de revisão (ex.: caso de pasta com `cooldown_seconds` muito longo gerar falso-alarme → considerar threshold per-folder).
- `.claude/CLAUDE.md` (seção "Operational Invariants"): nova bullet referenciando ADR-005, explicando que folder-degraded é per-folder sem pausa, recuperação por sucesso, in-memory.
- `README.md` (se há seção de operação): documentar `folder_staleness_threshold_seconds` em `daemon:` no schema do config.yaml.

## Verificação end-to-end

- `python -m pytest tests/ -v` passa (incluindo `test_daemon_staleness.py` novo e adições em `test_notifier.py`).
- `python -m drive_sync --check` passa em config com e sem a chave nova.
- `grep -rn "folder_staleness_threshold_seconds" drive_sync/ config/ docs/` cobre os 4 paths esperados (config.py, daemon.py, config.yaml, ADR-005).

## Verificação manual

Daemon real, threshold baixo para acelerar:

1. Parar daemon: `systemctl --user stop drive-sync`.
2. Editar `~/.config/drive-sync/config.yaml` adicionando em `daemon:` (criar a seção se não existir):
   ```yaml
   daemon:
     folder_staleness_threshold_seconds: 90   # 1.5 min para o teste
   ```
3. Escolher uma pasta de teste com `git_mode: bisync` e sync rápida (ex.: `dotfiles`).
4. Quebrar a sync da pasta de teste: temporariamente renomear o `local_path` da pasta (ex.: `~/.dotfiles` → `~/.dotfiles.test-backup`) — daemon vai falhar ao processar.
5. Religar daemon: `systemctl --user start drive-sync`.
6. Aguardar ≥ 2 minutos. Verificar:
   - `systemctl --user status drive-sync | grep STATUS` mostra `STATUS=degraded folders: dotfiles (>1.5min sem sucesso)` (ou equivalente).
   - `journalctl --user -u drive-sync -n 50 | grep FOLDER_DEGRADED` registra a linha tagueada CRITICAL.
   - `notify-send` aparece como notification do desktop (se houver sessão gráfica).
7. Restaurar a pasta: `mv ~/.dotfiles.test-backup ~/.dotfiles`. Aguardar próximo ciclo de sync.
8. Verificar:
   - `systemctl --user status` não mostra mais `dotfiles` na lista degraded.
   - `journalctl` NÃO mostra nova `notify-send` de recuperação (silenciosa).
9. Cleanup: remover/reverter `folder_staleness_threshold_seconds` do config; restart.

## Notas operacionais

- **Ordem dos blocos**: 1 → 2 → 3 → 4 → 5. Bloco 4 depende de 2 e 3. Bloco 5 pode rodar em paralelo após 1-3 fecharem (precisa do número final do ADR-005 que o `/new-adr` atribui).
- **ADR-005 é gerada via `/new-adr` no passo 5 do /triage** que produziu este plano (delegação canonical). Não duplicar conteúdo aqui; este plano cita ADR-005 como decisão estrutural complementar.
- **Atenção do reviewer do bloco 2**: confirmar que `_last_successful_sync_at` é setado APENAS no caminho de sucesso (`_process_folder` retorna True), NUNCA no de erro — esse é o ponto que distingue do `_last_sync_at` from-start do ADR-004.
- **Atenção do reviewer do bloco 3**: precedência auth-degraded vs folder-degraded em STATUS deve estar testada (bloco 4) e claramente comentada no notifier. Auth tem precedência por ser mais severo.
- **Follow-up condicional**: se algum operador reportar falso-alarme em pasta com `cooldown_seconds` muito longo (>4h), considerar threshold per-folder via `FolderConfig.staleness_threshold_seconds` override.
- **BACKLOG.md no done**: convenção atual do toolkit (per `/triage`) é que caminho-com-plano não escreve no BACKLOG durante o /triage; `/run-plan` adiciona a `**Linha do backlog:**` (acima, em `## Contexto`) em `## Concluídos` no done. Nenhuma alteração manual em `BACKLOG.md` é necessária dentro dos blocos deste plano.
