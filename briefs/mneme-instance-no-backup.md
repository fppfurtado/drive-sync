<!--
Problem Brief — drive-sync #74. Distilado do dossiê mneme-instance-no-backup.md.
Contrato problema→solução; garbage de trabalho (candidatos rejeitados, scratchpad, log) excluído.
Parent frame: briefs/coverage-audit-orfaos.md (#56) — problema distinto-mas-sobreposto (aquele frama a
DETECÇÃO de órfãos; este frama uma INSTÂNCIA de alto valor + a disciplina de restore). Cruza-linka, não amenda.
-->

# Problem Brief: instância `~/mneme` (KB soberana) sem backup off-machine

- Frozen at: 2026-09-01 (frozen after approval on 2026-09-01)
- Mode: verifiable
- Distilled from dossier: `.throughline/dossiers/mneme-instance-no-backup.md`
- Brief version: v1
- Status: FROZEN
- Parent frame: `briefs/coverage-audit-orfaos.md` (#56)
- Amendments: none

## Problem statement
A instância-dados `~/mneme` — a KB soberana do operador (repo git local-only, sem remote) — não tem
nenhuma cópia fora da máquina, logo uma falha de disco destrói a KB inteira mais a história de
proveniência dos seus registros, e nenhum sinal do sistema surfa esse gap.

A KB subsidia decisões de vida (não só de código); a perda é irreversível e a classe de falha é a pior
para uma ferramenta de backup — silenciosa-por-omissão. O conteúdo está versionado em git, mas todo o
versionamento é local: sem cópia off-machine, o disco é ponto único de falha.

## Jobs in scope
- J1: Colocar a KB sob backup off-machine preservando a história git (a proveniência é a própria
  história append-mostly), pelo mecanismo soberano já estabelecido do ecossistema.
- J2: Provar que o backup RESTAURA — recuperar a KB de uma cópia off-machine para um local scratch e
  reproduzir conteúdo + história íntegros — não apenas que sincroniza.

## Non-goals (explicitly out of scope)
- N1: Consertar genericamente o ponto-cego de cobertura para conteúdo de topo sem sibling configurado —
  é o Model A do Brief pai (#56), problema distinto. Este Brief é a instância que evidencia a
  recorrência; o conserto do detector mora lá. (scope-boundary — não é deferral.)
- N2: Redundância multi-offsite / 3-2-1 estrito (um 2º destino além do offsite primário).
  `Value-rejected:` a KB não foi classificada como tier-0-irreplaceable pelo operador (2026-09-01);
  single-offsite iguala o padrão do resto do ecossistema, e um 2º destino seria gold-plating sem
  necessidade validada. (Re-abre se a KB for reclassificada tier-0.)
- N3: Higiene do ruído de cobertura (o detector lista dezenas de subárvores transitórias de estado-de-app
  como órfãs) — classe sinal-vs-ruído do detector, endereçável no repo dono. (scope-boundary.)

## Constraints
- C1: **Soberania** (valor load-bearing do operador). A KB abrange domínios pessoal/legal/financeiro; o
  offsite tem de ser encriptado-ponta-a-ponta e controlado-pelo-operador. Um host de código de terceiro
  (mesmo repo privado) é downgrade de soberania — e, no desenho vigente do mecanismo, dar um remote
  externo à KB a REMOVERIA do sync soberano (a heurística "tem remote ⟹ backup externo existe" a
  classificaria como já-coberta). O caminho soberano e o caminho externo são mutuamente exclusivos aqui.
- C2: **Preservar história/proveniência**, não só um snapshot de arquivos — o backup tem de capturar a
  história git completa, não apenas o working-tree corrente.
- C3: **Não inventar mecanismo paralelo.** O ecossistema já tem um mecanismo soberano de backup
  (sync encriptado-ponta-a-ponta + dead-man's-switch + audit de cobertura). Candidate-not-default: o
  incumbente ganha por fit-ao-problema, não por incumbência — e aqui o fit é forte porque o mecanismo já
  foi desenhado para a classe "repo git local-only" exatamente deste caso.

## Success criteria
- S1: verifiable — A KB é recuperável de uma cópia off-machine: restaurar num local scratch reproduz o
  HEAD, a contagem completa de commits, o conjunto de arquivos versionados, e passa `git fsck` limpo;
  o conteúdo derivado/regenerável (índice de busca, artefatos de IDE) legitimamente NÃO é exigido na
  restauração (é gitignored e reconstruível).
- S2: verifiable — O gap deixa de ser silencioso: a KB é um alvo de backup declarado e monitorado pelo
  dead-man's-switch existente (re-alarma enquanto o backup estiver ruim), não um órfão não-visto.

## Key facts and provenance
- F1: `~/mneme` é um repo git branch única, **sem remote configurado**, ~23M (14M de `.git`), 648
  commits, 199 arquivos versionados, working-tree limpo. basis: retrieved (probe git, 2026-09-01).
- F2: O mecanismo de backup (drive-sync) desenha explicitamente o caminho para repo git local-only:
  `git_handling: bundle` → sincroniza um `.gitbundle` (história `--all` + snapshot do worktree) para o
  remote encriptado-ponta-a-ponta; repo COM remote sai do sync (o host externo é tratado como o backup).
  basis: retrieved (ADR-008 + tabela git_handling, drive-sync, 2026-09-01).
- F3: A KB caiu num ponto-cego do audit de cobertura: o universo do audit são os diretórios-PAIS dos
  `local_path` configurados; nenhum configurado resolve para um filho-direto de `$HOME` (o único
  candidato de topo é symlink que resolve para dentro de outra subárvore), então `$HOME` nunca é
  escaneado e a KB nunca foi flagrada. Não é exclusão deliberada (não está na allowlist do audit).
  basis: retrieved (`drive-sync --check` ao vivo + config, 2026-09-01).
- F4: Esta é a **2ª instância real** da mesma classe de ponto-cego; a 1ª (`pictures/Screenshots`, #54)
  motivou o Brief pai #56. O conserto do detector para conteúdo de topo (Model A) foi deferido em #56,
  gated na decisão de mirror-by-default (#55) — **#55 landou (PR #72, 2026-09-01)**, então o gate do
  Model A plausivelmente disparou. basis: retrieved (#54/#55/#56 + git log drive-sync, 2026-09-01).
- F5: Restore-test executado (2026-09-01, código real do drive-sync `create_bundle`/`restore_from_bundle`):
  bundle→restore num scratch reproduziu HEAD `f7f255b`, 648 commits, 199 arquivos versionados, `git
  fsck` limpo, `git bundle verify` rc=0. As únicas omissões foram 3 dirs **gitignored** (`derived/`
  = índice regenerável via `mneme index rebuild`; `.cursor/`, `.obsidian/` = artefatos de IDE "never in
  the KB" por design) — corretamente fora do escopo. S1 satisfeito. Confirmado também END-TO-END na cópia REAL do Proton: o daemon gerou e subiu
  `proton:Sync/mneme/mneme.gitbundle`; download + `git bundle verify` ("complete history") + restore
  reproduziram a KB, e o `git archive` da história compartilhada é byte-idêntico entre fonte e cópia
  offsite (sha `2d03cb…`). basis: execução direta, 2026-09-01.

## Deliberate exclusions (from the dossier)
- Candidato "git remote próprio (host de código externo)" — rejeitado por C1 (soberania + a remoção
  paradoxal do sync soberano), não carregado.
- Candidato "adicionar a KB à allowlist do audit" — rejeitado (allowlist = intencionalmente-FORA do
  backup; objetivo é o oposto).
- Candidatos rejeitados, scratchpad e log do dossiê — classe working-garbage padrão do template.

## Solution-space status (NOT a solution)
- Vehicle: committed — o veículo é o mecanismo de backup incumbente (uma mudança de configuração de
  cobertura + verificação de restore), não um produto de software novo. A questão-de-veículo não está
  aberta.
- Settled when: n/a (veículo já comprometido; direção validada empiricamente pelo restore-test F5).

## Open questions carried forward
- O ponto-cego `$HOME` do audit (agora que #55 landou, Model A parece des-gated) e o ruído de cobertura
  merecem ciclo próprio no repo dono — a decidir no tracker (#56 / novo item).
- Disposição env-stack do arquivo de config de cobertura, que é ele próprio um snowflake não-versionado
  e não-backup-eado (o config que governa os backups não está sob backup): promote-declarativo vs
  keep-snowflake.
