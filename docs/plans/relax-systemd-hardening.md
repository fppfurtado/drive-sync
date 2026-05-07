# Plano — Relaxar hardening da unit systemd para destravar bisync sob protondrive

## Contexto

Diagnóstico de /debug 07/mai 2026 isolou a unit `drive-sync.service` (`systemctl --user`) como causa do EROFS espúrio em `rclone bisync` de pastas grandes. Experimento controlado (mesmas flags rclone, mesma versão, mesma config rclone): execução manual fora do systemd rodou 2h58min sem nenhum `read-only file system`; sob a unit, falha em ~33min com EROFS em `chtimes`/`mkdir`/`.partial`.

A unit usa `ProtectSystem=strict`, `PrivateTmp=yes`, `NoNewPrivileges=yes` como hardening. A mudança remove apenas `ProtectSystem=strict` (variável discriminante mais provável; remove só uma linha). Trade-off de segurança e razões de design registrados em [ADR-002](../decisions/ADR-002-relaxar-hardening-systemd-protondrive.md).

**Linha do backlog:** unit systemd: relaxar hardening removendo `ProtectSystem=strict` para destravar bisync sob protondrive — diagnóstico do /debug 07/mai 2026 (após 9+ dias de loop EROFS no `dev-projects` bloqueando 15549+ arquivos de `h3/gestaoclick-report_react/.git/objects`) isolou a unit systemd como variável discriminante: rclone bisync com flags idênticas executou 2h58min manualmente sem EROFS, e falhou em ~33min sob a unit. Trade-off (defesa em profundidade reduzida vs. funcionalidade) registrado em ADR-002.

## Resumo da mudança

Remover a linha `ProtectSystem=strict` do template `systemd/drive-sync.service`. Manter `NoNewPrivileges=yes` e `PrivateTmp=yes` para isolar a variável e preservar defesa baseline. Operador propaga a mudança para o runtime (`~/.config/systemd/user/drive-sync.service`), reverte flags diagnósticas adicionadas durante o /debug em `~/.config/drive-sync/config.yaml`, e valida observando `dev-projects` completar `--resync` sem EROFS.

Mudança no repo é mínima (1 linha removida em 1 arquivo). Esforço operacional concentrado na validação (bisync `--resync` da pasta grande pode demorar horas).

## Arquivos a alterar

### Bloco 1 — relaxar hardening da unit {reviewer: code}

- `systemd/drive-sync.service`: remover a linha `ProtectSystem=strict` da seção `[Service]`. Manter `NoNewPrivileges=yes` e `PrivateTmp=yes` (não removidos preventivamente — bisect aplicado para isolar a variável). Comentário adjacente "Hardening básico — não precisa de root nem de namespace especial" pode permanecer ou ser ajustado para refletir que `ProtectSystem` foi removido por incompatibilidade com o backend protondrive — caso de comentário justificado pela convenção do CLAUDE.md (WHY não-óbvio: referência a ADR-002).

## Verificação end-to-end

- `grep -c "ProtectSystem" systemd/drive-sync.service` → `0` (linha removida).
- `grep -c "NoNewPrivileges=yes" systemd/drive-sync.service` → `1` (mantido).
- `grep -c "PrivateTmp=yes" systemd/drive-sync.service` → `1` (mantido).
- `python -m pytest tests/ -v` → 71 testes passando (sem regressão; mudança é só na unit, não toca código Python).

## Verificação manual

A invariante real (rclone bisync executa sob a unit sem EROFS) só pode ser confirmada por observação prolongada do `dev-projects`. Passos do operador após o /run-plan completar:

1. **Reverter flags diagnósticas adicionadas durante o /debug** em `~/.config/drive-sync/config.yaml` (não está sob versionamento — runtime do usuário):
   ```yaml
   # Antes (estado pós-/debug):
   - "--transfers=1"
   - "--checkers=1"
   ...
   - "--no-update-dir-modtime"

   # Depois (estado pretendido):
   - "--transfers=4"
   - "--checkers=8"
   # remover "--no-update-dir-modtime" (era workaround paliativo, sem efeito sobre mkdir/.partial)
   ```

2. **Propagar template para runtime**:
   ```bash
   cp "/storage/3. Resources/Projects/drive-sync/systemd/drive-sync.service" ~/.config/systemd/user/drive-sync.service
   systemctl --user daemon-reload
   ```

3. **Iniciar daemon e observar**:
   ```bash
   systemctl --user start drive-sync
   journalctl --user -u drive-sync -f
   ```

4. **Critério de sucesso** (D0+algumas horas, dado o gap de 15549+ arquivos):
   - `[dev-projects] bisync concluído com sucesso` aparece pelo menos uma vez.
   - `journalctl --user -u drive-sync --since "1 hour ago" | grep -c "read-only file system"` → `0` (zero ocorrências do EROFS espúrio).
   - `journalctl --user -u drive-sync --since "1 hour ago" | grep -cE "(chtimes|mkdir|.partial).*read-only"` → `0`.

5. **Critério de falha** (precisa follow-up):
   - Reaparece qualquer `read-only file system` no log → `ProtectSystem=strict` sozinho não era suficiente. Abrir follow-up plan para remover também `PrivateTmp=yes`. Atualizar ADR-002 com a evidência.

## Pendências de validação

- **D0+algumas horas — primeiro `dev-projects` concluído**: confirmação de "1 ocorrência de bisync concluído sem EROFS" libera o status da ADR-002 de "validação empírica pendente" para "validado".
- **D7 — sessão estável**: sem reaparecimento de EROFS em qualquer pasta. Confirmação final.

## Notas operacionais

- O bisync `--resync` de Projects vai bloquear outras pastas via `_rclone_lock` (ADR-001) por horas. Comportamento esperado dado o backlog acumulado de 15549+ arquivos. Mitigação de latência para esse cenário está registrada como item separado em `BACKLOG.md` (granularidade vs. fila com prioridade) — não é escopo deste plano.
- Reverter os flags `--transfers=1 --checkers=1` no config.yaml é importante para velocidade do upload (com `--transfers=1`, gap de 15549+ files levaria muitas horas a mais).
- Se o operador tem o serviço rodando (com flags antigas): parar primeiro (`systemctl --user stop drive-sync`) antes de mexer na unit ou no config, para evitar que rclone em curso confunda estado bisync.
