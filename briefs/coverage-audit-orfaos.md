<!--
Problem Brief — drive-sync #56. Distilado do dossiê coverage-audit-orfaos.md.
Contrato problema→solução; garbage de trabalho (candidatos rejeitados, scratchpad, log) excluído.
-->

# Problem Brief: config-check audit de cobertura — órfãos sem folder cobrindo

- Frozen at: 2026-08-26 (frozen after approval on 2026-08-26)
- Mode: verifiable
- Distilled from dossier: `.throughline/dossiers/coverage-audit-orfaos.md`
- Brief version: v2
- Status: FROZEN
- Amendments: v1→v2 (2026-08-26): C3 — removida a restrição "escopo restrito a auto/plain"
  (paridade born-invalid importada do ADR-011). Surfaced durante o build (soundness-check da
  posture): a classe-cobertura sinaliza conteúdo NÃO-declarado, logo `git_handling` é ortogonal —
  todo `local_path` declarado (qualquer modo) é "conhecido". Aprovação: base na aprovação do build
  (operador, "Freeze + lightweight build", 2026-08-26); correção estritamente mais correta,
  registrada em ADR-015.

## Problem statement
Conteúdo local vivo que deveria ser backup-eado pode cair fora de todo `folder.local_path`
configurado sem que o drive-sync emita qualquer sinal, e o gap persiste silencioso.

O drive-sync escolhe cobertura subpasta-a-subpasta: cada `folder` declara um `local_path`. Não há
nenhum mecanismo que compare o conjunto declarado com o que de fato existe no filesystem — então uma
subárvore que nasce ou é renomeada fora dos `local_path` configurados não é backup-eada e ninguém
percebe. Para uma ferramenta de backup, perda-silenciosa-por-omissão é a pior classe de falha.

## Jobs in scope
- J1: Detectar subárvores de conteúdo local que nenhum `folder.local_path` cobre ("órfãos de
  cobertura") e sinalizá-las de forma actionable — paths concretos que o operador possa resolver.
- J2: Distinguir órfão-real de fora-de-escopo-intencional, para que o sinal não vire ruído (um dir
  deliberadamente não-sincronizado não deve alarmar toda execução).

## Non-goals (explicitly out of scope)
- N1: Case-duplicates DENTRO de um `local_path` (colisão case-insensitive Path1↔Path2). Classe
  distinta — ausência-de-cobertura vs colisão-de-nome; boundary, coberto por ADR-011 e por #37/#38.
  Este Brief é o irmão complementar, não o mesmo job. (scope-boundary — não é deferral.)
- N2: Auto-remediação (criar folder/config automaticamente para o órfão). `Value-rejected:` um órfão
  exige decisão do operador — se é para cobrir, com que `remote_subpath`, ou se é para excluir; inferir
  isso automaticamente viola o princípio "controle do operador, não inferência" (ADR-010/011). O audit
  é sinal, não ação.
- N3: Arm de detecção em runtime (pegar o órfão nascido com o daemon já rodando, config inalterado —
  o modo de falha exato do incidente-instância). Valor afirmado, deferido por proporcionalidade (scan
  por-ciclo tem custo a calibrar, análogo ao #37). `Trigger-source:` este Brief entregue via `--check`
  (config-time) + evidência de recorrência born-after-config em `journalctl`/relato do operador —
  fires when o audit config-time existir E surgir um órfão que só um hook runtime pegaria (drift com
  daemon ativo, sem re-`--check`). Irmão do #37 (runtime probe de case-dup).

## Constraints
- C1: **Sinal > ruído.** O valor inteiro depende de baixo falso-positivo. O universo-de-cobertura é
  derivado dos PAIS dos `local_path` já configurados (siblings), com allowlist explícita para os
  siblings intencionalmente-fora — não um scan cego de todo o filesystem.
- C2: **Warn, não fatal.** Órfão é omissão, não malformação — não causa falha de rclone. A condição
  NÃO deve bloquear startup nem falhar `--check` (cobertura-incompleta é comum e legítima). Distinto
  do fatal do ADR-010/011, cujo racional (malformação que causa rc=7; warn-only perdível em CI) NÃO
  transfere para a classe-omissão.
- C3: **Anti-ceremony + paridade arquitetural.** Reusar o padrão `_validate_*` config-time do
  ADR-010/011 onde couber; não introduzir conceito novo de config sem pagar valor. `git_handling` é
  **ortogonal** à cobertura (v2): todo `local_path` declarado conta como "conhecido pelo operador"
  independente do modo (`auto`/`plain`/`bundle`/`skip`) — o audit sinaliza apenas conteúdo
  **não-declarado**. (v1 dizia "escopo restrito a auto/plain", paridade born-invalid do ADR-011 —
  corrigido; ver Amendments.)

## Success criteria
- S1: verifiable — Com um órfão presente (dir sibling de um `local_path` configurado, com conteúdo,
  não-allowlisted) e um sibling intencional allowlisted, rodar `drive-sync --check`: o órfão é
  LISTADO com seu path concreto; o sibling allowlisted NÃO é listado; e `--check` NÃO falha por causa
  disso (exit-code de sucesso, mensagem em nível warn).
- S2: verifiable — O universo é derivado sem declaração nova de "roots": dado o conjunto de folders
  configurados, o audit examina os diretórios-pais desses `local_path` e classifica cada filho com
  conteúdo como coberto (é/está sob um `local_path`), allowlisted, ou órfão.

## Key facts and provenance
- F1: Incidente-instância — um diretório vivo alimentado pelo desktop (`pictures/Screenshots`, 82
  arquivos, escrito diariamente) ficou fora de todo backup de mai a ago/2026 porque o folder
  configurado apontava para um sibling de nome distinto (`pictures/screenshots`, minúsculo,
  congelado). Descoberto por auditoria manual, não por sinal do sistema. basis: retrieved (#54 +
  comentário, 2026-08-26).
- F2: Existe precedente arquitetural direto para validação config-time em `drive-sync --check`:
  `_validate_*_against_*(folder, max_depth)` em `config.py`, reusando `git.max_recursion_depth`
  (default 6), escopado a `auto`/`plain`. Porém esses validadores são FATAIS e checam malformação
  DENTRO de um folder declarado — a classe inversa deste Brief (completude do conjunto declarado vs
  o FS). basis: retrieved (ADR-010, ADR-011, 2026-08-26).
- F3: Modelo de universo escolhido — "siblings de configurados": o universo-a-checar = os
  diretórios-pais dos `local_path` configurados; um filho com conteúdo que não é ele próprio um
  `local_path` coberto e não está allowlisted é órfão. Modela exatamente F1 (o órfão era sibling de um
  configurado). Limitação conhecida: um diretório top-level totalmente novo (sem sibling configurado)
  não é visto por este modelo — endereçado pela deferral armada em §Deliberate exclusions. basis:
  decisão de framing 2026-08-26 (operador).
- F4: `drive-sync --check` é config-time (operador roda antes de start / em mudança de config). O
  incidente F1 nasceu com o daemon JÁ rodando e config inalterado — logo um audit só-em-`--check`
  pega o órfão no PRÓXIMO `--check` manual, não no nascimento. A alegação original do item ("teria
  pego no nascimento") só é verdadeira com o arm runtime (N3). basis: retrieved (comportamento de
  `--check` + F1, 2026-08-26).
- F5: Proton Drive é case-insensitive — fato semântico do remote que fundamenta a classe N1 (ADR-011),
  distinta desta. basis: retrieved (ADR-011, 2026-08-26).
- F6: Item-irmão #55 (adotar "espelho-por-default, exclusão-por-exceção") governa a governança de
  divergência local↔remoto; se adotado, a allowlist deste audit e os `exclude:` explícitos de #55
  convergem para o mesmo conjunto. basis: retrieved (#55, 2026-08-26).

## Deliberate exclusions (from the dossier)
- Modelo-universo A (config nova `coverage_audit: {roots, allow}`, scan de roots declarados) — NÃO
  carregado agora; escolhido o modelo B (siblings, menor ceremony, cobre F1). A cobre o ponto-cego de
  B (top-level novo) mas ao custo de declarar universo + manter allowlist. Deferral de valor-afirmado.
  `Trigger-source:` a decisão de #55 (espelho-por-default) — fires when #55 for decidido a favor de
  roots-declarados-com-exclusão-explícita, ponto em que A é a extensão natural (allow = os `exclude:`
  de #55). Rastreado em #55/#56.
- Candidatos rejeitados, scratchpad e log do dossiê — classe working-garbage padrão do template.

## Solution-space status (NOT a solution)
- Vehicle: committed — é uma feature de código no daemon existente (`drive-sync --check`), não uma
  mudança de processo/política. A questão-de-veículo não está aberta.
- Settled when: n/a (veículo já comprometido).

## Open questions carried forward
- Forma exata do mecanismo de allowlist (novo campo de config vs reuso de um existente; per-folder vs
  global) — decisão de solução, para o software-track.
- Se `--check` deve, além de listar, emitir a mesma sinalização pelos canais do daemon (log tagueado
  estilo `[COVERAGE_ORPHAN]` / `notify-send`) quando rodado — provável sim por paridade com ADR-012,
  a confirmar no design.
