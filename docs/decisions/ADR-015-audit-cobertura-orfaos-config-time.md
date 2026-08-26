# ADR-015: Audit de cobertura de órfãos em config-time (warn)

**Data:** 2026-08-26
**Status:** Aceito

## Origem

- **Incidente-instância (#54):** o diretório vivo `/storage/pictures/Screenshots/` (maiúsculo, 82
  arquivos, escrito diariamente pelo GNOME Shell) ficou **fora de todo backup de mai a ago/2026**
  porque o `folders:` configurado apontava para um sibling de nome distinto
  (`pictures/screenshots`, minúsculo, congelado). Descoberto por auditoria manual de layout, não por
  qualquer sinal do drive-sync.
- **Frame:** `briefs/coverage-audit-orfaos.md` (v2, frozen 2026-08-26). Item de tracker **#56**.
- **Decisão base:** [ADR-010](ADR-010-validacao-config-time-auto-exclude-markers-codigo.md) /
  [ADR-011](ADR-011-deteccao-de-case-duplicates-path1-path2-em-config-time.md) — precedente do padrão
  "validação config-time em `--check`". Este ADR cobre a **classe inversa** (ver Contexto).

## Contexto

O drive-sync escolhe cobertura **subpasta-a-subpasta**: cada `folder` declara um `local_path`. Não há
mecanismo que compare o conjunto declarado com o que de fato existe no filesystem. Quando uma
subárvore nasce ou é renomeada fora dos `local_path` configurados, ela cai no vão **sem sinal** — não
é backup-eada e ninguém percebe. Para uma ferramenta de backup, perda-silenciosa-por-omissão é a pior
classe de falha.

**Classe inversa de ADR-010/011** — a distinção é load-bearing:

| | ADR-010/011 | ADR-015 (este) |
|---|---|---|
| Pergunta | O folder **declarado** é bem-formado? | Existe conteúdo **não-declarado** que deveria estar? |
| Escopo do scan | DENTRO de um `local_path` | Os PAIS dos `local_path` (siblings) |
| Falha que previne | Malformação → rclone `rc=7` (quebra sync) | Omissão → nada sincroniza (não quebra nada) |
| Policy | **Fatal** (`raise ValueError`) | **Warn** (retorna lista; não levanta) |

Os dois racionais **fatais** de ADR-010/011 **NÃO transferem**: (a) lá a malformação causa um abort
concreto de rclone; aqui o órfão simplesmente não sincroniza — não há falha downstream a prevenir; (b)
lá warn-only seria escape-hatch perdível em CI de uma condição que quebra sync; aqui
cobertura-incompleta é **comum e legítima** (muito de `/storage/` é intencionalmente fora — `tools/`,
VMs, sandbox). Bloquear startup/`--check` por isso seria desproporcional.

## Decisão

Adicionar `audit_coverage_orphans(folders, allow) -> list[Path]` em
[`drive_sync/config.py`](../../drive_sync/config.py), surfaceado por `drive-sync --check` como **aviso
(warn), não fatal** — `--check` continua retornando 0.

**Modelo B — "siblings de configurados".** O universo-a-checar são os diretórios-**PAIS** dos
`local_path` configurados. Para cada pai existente, cada filho-**diretório** com conteúdo que não é ele
próprio um path coberto (declarado, ou sob/contendo um declarado) e não está allowlisted é um órfão.
Modela exatamente o incidente (`Screenshots` era sibling do configurado `screenshots`), com **zero
declaração nova de "roots"** — anti-ceremony vs. o modelo alternativo A (ver Alternativas).

Razões:

- **`git_handling` é ORTOGONAL à cobertura.** Todo `local_path` declarado conta como "conhecido pelo
  operador" independente do modo (`auto`/`plain`/`bundle`/`skip`). O audit sinaliza apenas conteúdo
  **não-declarado**. Um folder `skip` é uma não-cobertura **declarada e consciente** (loud), não o gap
  **silencioso** que este audit caça. Isto corrige uma imprecisão do Brief v1 (ver Consequências).
- **Sinal, não ação.** O audit lista paths; o operador decide cobrir (novo `folders:`) ou excluir
  (`coverage_audit.allow`). Não cria config automaticamente — órfão exige decisão humana (cobrir com
  que `remote_subpath`? ou excluir?), alinhado ao princípio "controle do operador, não inferência"
  (ADR-010/011).
- **Allowlist como escape-hatch legítimo.** `coverage_audit.allow: [<paths>]` declara os siblings
  intencionalmente-fora. Um path é allowlisted se casa exato OU é subpath de uma entry. Distinto de
  ADR-011 (que **não** tem escape-hatch, porque case-insensitive é fato semântico do remote): aqui a
  intenção-de-não-cobrir é uma escolha legítima do operador.
- **Opt-out global.** `coverage_audit.enabled: false` desliga o audit inteiro.
- **Profundidade barata.** Scan é depth-1 a partir de cada pai (só filhos imediatos) + um walk
  bounded por candidato para checar "tem conteúdo". Não reusa o walk depth-6 do ADR-010/011 — o modelo
  siblings não precisa.

## Consequências

- **Emenda ao Brief (v1→v2):** o Brief v1 C3 dizia "escopo restrito aos modos que fazem bisync de
  worktree (auto/plain)" — paridade importada de ADR-011 que **não se aplica** à classe-cobertura
  (`git_handling` é ortogonal). Corrigido em v2; o build implementa git-handling-agnóstico.
- **Ponto-cego conhecido (deferral armada):** o modelo B não vê um diretório top-level **totalmente
  novo** (sem sibling configurado). O modelo A (roots declarados + allowlist) cobre isso, deferido/
  armado no gatilho da decisão de #55 (espelho-por-default) — ali `allow` converge com os `exclude:`
  explícitos de #55.
- **Locus só config-time (deferral armada, N3 do Brief):** `--check` é config-time; o incidente nasceu
  com o daemon rodando e config inalterado, então este audit pega o órfão no **próximo `--check`**, não
  no nascimento. O arm runtime/watchdog (irmão do #37) é deferido/armado.
- **Falso-positivo possível:** um sibling que é repo git com remote (backup-eado no GitHub) é flageado
  como órfão — o audit não consulta `git remote`. Mitigação: `coverage_audit.allow`. Refinamento
  git-remote-aware é candidato futuro se a fricção surgir.

## Alternativas consideradas

- **Modelo A — roots declarados + allowlist:** config `coverage_audit: {roots, allow}`, scan de roots
  declarados. Mais completo (pega top-level novo), mas ceremony (declarar universo + manter allowlist)
  e acopla com a decisão ainda-aberta de #55. Deferido/armado, não rejeitado.
- **Fatal (paridade literal ADR-010/011):** rejeitado — órfão não quebra sync; bloquear por
  cobertura-incompleta (comum e legítima) é desproporcional (ver Contexto).
- **Auto-remediação (criar folder pro órfão):** rejeitado (`Value-rejected` no Brief N2) — exige
  decisão do operador sobre cobrir/excluir + `remote_subpath`.

## Gatilhos de revisão

- **#55 decidido a favor de espelho-por-default** → estender para o modelo A (roots + allow).
- **Recorrência de órfão nascido com daemon ativo** (born-after-config, sem re-`--check`) → materializar
  o arm runtime (N3, irmão do #37).
- **Fricção de falso-positivo por repos-git-com-remote** → avaliar refinamento git-remote-aware.

## Referências

- Brief: `briefs/coverage-audit-orfaos.md` (v2)
- Tracker: #56 (este), #54 (incidente-instância), #55 (espelho-por-default, gate do modelo A), #37
  (runtime probe, irmão do arm N3)
- ADRs: ADR-010, ADR-011 (precedente config-time; classe distinta)
