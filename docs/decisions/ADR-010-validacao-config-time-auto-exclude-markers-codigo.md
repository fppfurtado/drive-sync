# ADR-010: validação config-time de `auto_exclude` contra markers de código no `drive-sync --check`

**Data:** 2026-06-01
**Status:** Proposto (promoção a `Aceito` no merge do plano consumidor `auto-exclude-check-validation.md` — sem janela de validação empírica de semanas porque a mecânica é determinística (scan de filesystem com markers fixos, raise no loader) E o cenário-alvo já foi observado no incidente 2026-05-30/31 que justifica este ADR).

## Origem

- **Investigação:** incidente 2026-05-30/31 em `archive`/`library`. Operador declarou `auto_exclude: false` em folders sob `/storage/archive/` e `/storage/areas/` (acreditando serem "dados", não código) com `exclude:` mínimo (`*.tmp`, `.zotero/**` apenas). `rclone bisync` subiu `.venv/` (multi-GB de site-packages em 9 repos arquivados sob `archive/repos/*`) + `node_modules/` (1.7GB em `areas/learning/*`). Cascata: rclone serializado ([ADR-001](ADR-001-serializar-chamadas-rclone.md)) segurou lock global por **14h** iterando `.venv/lib/python3.13/site-packages/pandas/...`; todos os 13 folders entraram em `STATUS=degraded` ([ADR-005](ADR-005-folder-staleness-degraded.md)). Recovery manual exigiu stop daemon + flip dos folders para `auto_exclude: true`.

## Contexto

Default atual da config (`auto_exclude: True` quando ausente) já é seguro — exclude_presets cobrem `.venv/**`, `node_modules/**`, `target/**`, `__pycache__/**` e congêneres. O cenário falhou porque o operador declarou `False` **explicitamente** sem perceber que o path continha código com globs de build artifacts conhecidos. Não há validação config-time impedindo essa armadilha — `drive-sync --check` aceita `auto_exclude: false` independente do conteúdo do `local_path`.

O cenário é detectável: `os.walk` em `local_path` até depth razoável encontra os markers de build artifacts conhecidos. Se markers aparecem E o operador desligou `auto_exclude`, há alta probabilidade de erro de declaração.

Item L21 do BACKLOG capturou 3 direções:

1. **erro fatal no `--check`** quando `auto_exclude: false` E scan detecta markers de código (preferida pelo custo-benefício no L21).
2. **warn-only** no `--check` (menos invasivo mas perdível).
3. **remover suporte a `auto_exclude: false`** — sempre aplica presets, deixa só `exclude:` para customização extra (mais radical, tira escape hatch).

Esta ADR formaliza a escolha (1) e define os parâmetros operacionais (markers, depth, mensagem de erro) para evitar releitura desestruturada quando alguém propuser (2) ou (3) no futuro.

## Decisão

Decisão central + dois parâmetros de escopo:

**(1) Decisão central — `drive-sync --check` rejeita com erro fatal** quando `folder.auto_exclude is False` E scan do `folder.local_path` detecta dirs cujo `Path.name` casa exato um dos markers `{".venv", "node_modules", "target"}`. Match por nome de segmento (não substring) — `.venv-backup/` ou `node_modules_old/` não disparam.

**(2) Parâmetro — Profundidade de scan = `git.max_recursion_depth`** (default 6). Reuso da infra existente em vez de novo budget de profundidade. Semantica análoga (`find_git_repos` varre subtree até depth N; este validador faz o mesmo com markers diferentes). Uniformidade > distinção semântica frágil.

**(3) Parâmetro de escopo — qualquer marker dentro de `.git/`** (incluindo `.git/objects/pack/`) está fora do escopo deste validador. `ADR-008` (`git_handling: auto|skip|bundle|plain`) já endereça estruturalmente "repo git em escopo de bisync" e cobre o cenário operador-com-folder-`git_handling: plain` + `auto_exclude: false` + repo git aninhado. Adicionar marker `.git/objects/pack/` aqui criaria caminho de erro duplicado para a mesma classe de problema. Divisão de responsabilidades: ADR-008 para classe git, ADR-010 para classe build artifacts não-git.

**Adicionalmente — escopo restrito a modos que usam bisync** (descoberto pós-merge inicial, 2026-06-01): validator também skipa folders com `git_handling in {bundle, skip}` porque esses modos não passam por `rclone bisync` do worktree (bundle dispatcha por-repo via git bundle; skip pula o folder inteiro). Apenas `auto` (bisync do conteúdo não-repo com `extra_excludes`) e `plain` (bisync puro sem treatment git) usam `auto_exclude` meaningfully — validar para `bundle`/`skip` seria false-positive cumulativo (folder pje-2.1 em modo bundle tem `target/` legítimos do Maven que não são uploaded de qualquer jeito).

Mensagem de erro multi-linha com **ação recomendada única + escape hatch explícito**:

```
auto_exclude: false em 'archive' E scan de /storage/archive detectou paths de código:
  - /storage/archive/repos/proj-a/.venv/
  - /storage/archive/repos/proj-b/node_modules/

Defina `auto_exclude: true` (recomendado: cobre todos os build artifacts conhecidos sem listagem manual).

Se precisa manter `auto_exclude: false` por razão específica, adicione globs em `exclude:` que cubram os paths listados acima e re-execute `--check`.
```

Razões:

- **Defesa em profundidade contra erro de declaração** — o cenário do incidente foi operador acreditando que folder não-código (na superfície: "documentos arquivados") não exigia presets. Validação config-time detecta a divergência antes do daemon começar a syncar gigabytes.
- **Falha-fast no `--check`** — operador roda `--check` antes de start; gate antecipa a dor.
- **Markers `.venv` + `node_modules` + `target`** cobrem 3 ecossistemas dominantes (Python, JS/TS, Rust). Paridade com `auto_exclude: true` presets que já cobrem todos os 3.
- **Match exato por `Path.name`** elimina false-positives de variantes (backup, archived, etc.) que o operador pode legitimamente declarar fora de exclude.
- **Reuso de `git.max_recursion_depth`** — uniformidade arquitetural; sem novo conceito.
- **ADR-008 como divisor estrutural para `.git/`** — evita dois mecanismos defendendo a mesma classe.

## Consequências

### Benefícios

- Cenário do incidente 2026-05-30/31 (14h lock global por `.venv`/`node_modules` em archive/library) reproduzido vira erro de `--check` antes do start do daemon. Detecção move-se de "operador percebe degraded after horas" para "operador percebe ao validar config".
- Defesa em profundidade complementa, não substitui, o default seguro (`auto_exclude: True` quando ausente). Operador que omite o campo segue protegido por construção.
- Mensagem de erro instrui a ação primária recomendada (`auto_exclude: true`); operador que precisa escape hatch (`auto_exclude: false`) ainda pode declarar mas tem path concreto para começar o `exclude:` manual.

### Trade-offs

- **I/O extra no `--check`** — scan recursivo até depth=6 em cada folder com `auto_exclude: false`. Para folders grandes com `auto_exclude: true` (caso comum), zero I/O extra. Aceitável: `--check` é one-shot interativo, não loop quente.
- **Cobertura assimétrica de markers** — `.venv`/`node_modules`/`target` cobrem Python/JS/Rust. Operador com folder Java contendo `build/` (Gradle) ou C++ com `cmake-build*/` ainda passa silente. Aceitável: incidente real foi Python+JS; gatilho de revisão registrado para próximo ecossistema.
- **`.git/` fora do escopo** — operador com repo git em folder declarado `git_handling: plain + auto_exclude: false` não recebe gate aqui (recebe via ADR-008 que classifica o folder). Aceitável: separação clara de responsabilidades > dupla defesa.

### Limitações

- **Não cobre crescimento pós-`--check`** — se operador roda `--check` quando paths estão limpos e depois popula `.venv/` (ex.: `pip install -e .` num folder syncado), validação não re-roda automaticamente. Aceitável: defesa em profundidade é gate inicial, não invariante runtime; cobertura runtime seria desperdício de I/O cada periodic cycle.
- **Match por nome de segmento, não conteúdo** — pasta vazia chamada `.venv/` dispara erro (false-positive teórico, raro na prática). Aceitável: presença de dir com nome canônico é sinal forte de intenção de virtualenv.
- **Skip silente quando `local_path` não existe** — paridade com `find_git_repos` no `git_handler.py` (`/triage` step probe). Operador rodando `--check` antes de criar paths (bootstrap) não fica bloqueado.

### Mitigações

- **Mensagem de erro com paths concretos** elimina o "onde está o problema?" do operador.
- **Match exato** evita false-positives em variantes `.venv-backup`, `node_modules_old`.
- **`local_path` inexistente skip silente** preserva fluxo bootstrap.

## Alternativas consideradas

### Direção (2) do L21 — `warn-only` no `--check`

`--check` imprime warning mas retorna 0. Operador vê na primeira vez, decide ignorar ou acionar. **Recusada**: warnings são perdíveis em CI/automação, e o incidente real (14h lock global + 13 folders degraded) mostrou que warning silenciado é equivalente a sem warning. Operador headless (ex.: fixup do `mr` cross-repo) não vê warnings interativos. Fatal-error força resolução antes do daemon start.

### Direção (3) do L21 — remover suporte a `auto_exclude: false`

Sempre aplica presets; deixa só `exclude:` para customização extra. **Recusada**: mais radical do que necessário. Existem folders legítimos onde operador precisa desligar presets (ex.: folder de testes que valida site-packages de uma versão específica). Tirar o escape hatch fere o princípio "controle do operador, não inferência". Gate de validação é mais cirúrgico: força explicitude na declaração, não remove a opção.

### Marker `.git/objects/pack/`

Detectaria repos git grandes em folders com `auto_exclude: false`. **Forma concreta que motivou a fronteira estabelecida na Decisão (3)**. **Recusada** porque ADR-008 (`git_handling: auto|skip|bundle|plain`) já endereça repos git estruturalmente — cria dois caminhos de erro para a mesma classe de problema. Operador com repo git em folder `git_handling: plain + auto_exclude: false` recebe gate via ADR-008 (que classifica o folder pre-bisync), não aqui. Divisão de responsabilidades.

### Profundidade de scan hardcoded (3 ou 4) vs. reuso de `git.max_recursion_depth`

Hardcoded em 3 teria justificativa válida pela **semântica de busca distinta** (`find_git_repos` procura repos aninhados; este validador procura artefatos de build em qualquer nível raso). **Recusada** porque o **escopo de busca** (subtree depth-bounded em `local_path` de folder) é idêntico — manter dois budgets distintos cria distinção semântica frágil sem ganho prático. Operador que ajustar `git.max_recursion_depth` para tunar git scan pode legitimamente esperar que mover de 6→4 reduza I/O do `--check` também; manter budgets unificados respeita essa expectativa.

### Markers adicionais (`build/` Gradle, `cmake-build*/`, `dist/`, `out/`)

Cobertura completa de ecossistemas. **Recusada por YAGNI**: incidente real foi Python+JS. Rust (`target/`) adicionado por simetria + paridade com presets `auto_exclude: true`. Demais ecossistemas registram gatilho de revisão (próximo incidente análogo → adicionar marker).

### Validação runtime periodic em vez de `--check`

Re-validar a cada ciclo de periodic_full_sync. **Recusada**: I/O recorrente em folders grandes sem ganho real (operador que mudou conteúdo pode rodar `--check` manualmente). Gate config-time é suficiente para o cenário do incidente.

## Gatilhos de revisão

- **Incidente análogo em ecossistema fora dos 3 markers atuais** (Java `build/`, Gradle `build/`, C++ `cmake-build*/`, Go `vendor/`, etc.): adicionar marker correspondente. Sinal: ≥1 incidente concreto reportado onde `auto_exclude: false` + marker não-coberto resultou em sync de multi-GB.
- **False-positive significativo** (`.venv-backup/`, `node_modules-archived/` falsificando match exato e bloqueando declaração legítima): revisar critério de matching — pode incluir whitelist por path explícito, ou downgrade para warning na presença de `exclude:` cobrindo o marker. Sinal: ≥1 operador reportando bloqueio em config legítima.
- **`.git/` aparecer como problema pós-ADR-008** (folder com repo git + `auto_exclude: false` + `git_handling: plain` deliberado escapando das duas defesas): revisar divisão de responsabilidades; pode exigir co-validação entre ADR-008 e ADR-010 OU adicionar marker `.git/` aqui condicionado a `git_handling: plain`.
- **`--check` lento o suficiente para incomodar workflow interativo** — comparativo contra baseline pré-ADR-010 (`time drive-sync --check` na config real): se latência pós-merge cresce ≥2× empiricamente, considerar limite de I/O (timeout per folder) ou cache de scan results. Sinal: ≥1 reporte concreto + medição comparativa contra baseline.

## Referências

- Plano de execução: [`.claude/local/plans/auto-exclude-check-validation.md`](../../.claude/local/plans/auto-exclude-check-validation.md) (modo local — não versionado).
- BACKLOG.md item L21 (`config: revisar default e validação de auto_exclude`) — fonte da direção (1) escolhida.
- ADRs relacionados:
  - [ADR-001](ADR-001-serializar-chamadas-rclone.md) — lock global é o **amplificador** do incidente (14h por causa da serialização).
  - [ADR-005](ADR-005-folder-staleness-degraded.md) — staleness foi o **sintoma observável** (13 folders degraded).
  - [ADR-008](ADR-008-abandonar-bisync-repos-git.md) — divisor estrutural para `.git/` (fora do escopo deste ADR).
- Doutrina de defesa em profundidade: pattern ADR-002 (relaxar hardening por incidente), ADR-003 (sinalização degraded por incidente), ADR-005 (staleness threshold por incidente), ADR-009 (editable elimina vetor de regressão).
