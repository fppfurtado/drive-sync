# ADR-009: pipx editable como modo de install padrão (workflow self-update via scripts/update.sh)

**Data:** 2026-06-01
**Status:** Proposto (promoção a `Aceito` no merge do plano consumidor `install-pipx-editable-update.md`; sem "validação empírica pendente de semanas" porque probe (linha 22) e mudança em scripts são determinísticos)

## Origem

- **Investigação:** dor empírica recorrente + incidente histórico convergem para a mesma raiz estrutural.
  - **2026-06-01:** após `git pull` do merge de ADR-008 (`16398d3`), foi necessário rodar `pipx reinstall drive-sync` manualmente para a CLI ver o código novo. Daemon restart sozinho não bastou — binário pipx ainda apontava para snapshot do venv velho. Ritual repetido a cada feature merge.
  - **2026-05-09 (BACKLOG.md L13 pré-consolidação):** symlink `~/.local/bin/drive-sync` foi sobrescrito por arquivo regular com shebang `#!/usr/bin/python3`, causando `ModuleNotFoundError: No module named 'drive_sync'` em loop sob `Restart=` do systemd por 18 horas até intervenção manual. Suspeita primária isolada: `pip install -e .` contra Python de sistema (Fedora `pip --user` instala scripts no mesmo prefixo `~/.local/bin/` que pipx).

## Contexto

`scripts/install.sh:17` usa `pipx install --force "$PROJECT_DIR"`. Este modo cria um venv pipx-managed isolado, copia/builda o pacote do checkout e instala um snapshot. CLI e daemon executam contra o snapshot, não contra o checkout.

Implicações operacionais:

1. **Toda feature merge exige reinstall manual** para a CLI ver código novo. Daemon restart é necessário (Python sem hot-reload) mas não suficiente — o binário pipx ainda aponta para o snapshot pré-merge. Ritual: `git pull && pipx reinstall drive-sync && systemctl --user restart drive-sync.service`.
2. **Dois prefixos competem por `~/.local/bin/drive-sync`**: `pipx install` cria symlink lá; `pip install -e .` contra Python de sistema (Fedora `pip --user`) instala scripts no mesmo prefixo. Quando o segundo é executado, o symlink é sobrescrito por arquivo regular apontando ao Python errado. Vetor L13 confirmado no incidente 2026-05-09.
3. **Modo dev em CLAUDE.md** (`python -m venv .venv && .venv/bin/pip install -e .`) existe paralelamente para evitar o conflito acima — mas duplica conceitualmente o que `pipx install -e` já oferece (live-reflect de mudanças no checkout).

Probe empírico (2026-06-01) confirmou que `pipx install -e <path>` em estado já instalado falha com "drive-sync already seems to be installed. Not modifying existing installation. Pass --force to force installation" — `--force` permanece necessário para idempotência em re-execução do install.sh.

## Decisão

Três decisões coordenadas:

**(1) `pipx install -e --force "$PROJECT_DIR"` é o modo de install padrão.** `pipx install -e` aponta o entry-point pro checkout (`~/.local/bin/drive-sync` vira symlink para `~/.local/share/pipx/venvs/drive-sync/bin/drive-sync`, que executa Python diretamente sobre os módulos do checkout). Resultado: `git pull` reflete imediatamente na CLI; daemon ainda precisa restart pra reimportar módulo (Python sem hot-reload).

**(2) `scripts/update.sh` encapsula o ritual pós-`git pull`:**
```bash
git pull --ff-only
systemctl --user restart drive-sync.service
```

**(3) Modo dev separado em CLAUDE.md é eliminado.** Editable cobre live-reflect que era a única razão do venv local; seção "Development Workflow" do CLAUDE.md removida pelo Bloco 3 do plano consumidor. Decisão registra side-effect doutrinário relevante (warning sobre `pip install -e .` contra Python de sistema some junto — vetor L13 eliminado estruturalmente).

Razões:

- **Resolve dor empírica recorrente** (reinstall manual a cada merge). Ritual cai para `bash scripts/update.sh`.
- **Elimina vetor L13** estruturalmente: snapshot pipx-managed deixa de existir; editable + venv pipx é o único caminho para `~/.local/bin/drive-sync`. Não há mais "dois lugares competindo".
- **Colapsa modelo mental**: um caminho de install serve operador e dev. CLAUDE.md "Development Workflow" duplicava conceito sem ganho prático após editable.
- **`--force` necessário** apenas para re-execução do install.sh (sem ele, pipx aborta com "already installed"). Trade-off aceito: comportamento upstream do pipx, não amarração ao design.

## Consequências

### Benefícios

- CLI reflete `git pull` imediatamente. Ritual pós-merge cai para `bash scripts/update.sh` (restart do daemon).
- Vetor L13 eliminado estruturalmente — não depende de "operador lembrar de não usar `pip install -e .` contra Python de sistema".
- Documentação de install simplificada — um caminho só em `scripts/install.sh`, sem bloco dev paralelo.

### Trade-offs

- **Editable amarra o entry-point ao path do checkout** (`/storage/dev/projects/drive-sync`). Mover ou renomear o projeto quebra o entry-point — operador precisa `pipx reinstall drive-sync` após o move. Setup atual aceita: single-user, projeto syncado pelo próprio drive-sync para o mesmo path em todas as máquinas. Caminho estável **por convenção operacional do single-user atual** — nada no código força esse path. Bootstrap em máquina nova exige clone no path canonical antes do install.sh.
- **`--force` persiste no install.sh** por necessidade upstream do pipx (re-run em estado já instalado). Não é amarração ao design — comportamento pipx pode evoluir; ver gatilho de revisão.

### Limitações

- **Não cobre cenário multi-máquina compartilhada** (≥2 operadores OU paths diferentes do checkout por host). Padrão profissional seria release tag + wheel publicada via `hatch build`. Overkill no escopo atual; gatilho de revisão registrado.
- **Migração one-shot manual** (documentada inline; ver `## Migração` abaixo).

## Migração

Operador no host atual roda UMA VEZ, após `git pull` do commit que aceita ADR-009:

```bash
pipx reinstall drive-sync
```

Confirmação: `pipx list` deve mostrar `drive-sync` sem warning `symlink missing or pointing to unexpected location`; `which drive-sync` deve retornar symlink (não arquivo regular). Hosts subsequentes herdam o modo via `scripts/install.sh` fresh (sem passo manual). Bloco 3 do plano consumidor replica esta instrução em CLAUDE.md §Installation para descobrabilidade.

## Alternativas consideradas

### `pipx uninstall && pipx install` dentro de `update.sh`

Mantém pipx no modo snapshot, mas `update.sh` reinstala o pacote a cada `git pull`. **Recusada porque**:

- (a) `pipx install` re-cria o venv do zero a cada update (~5-15s para rebuild de wheel local + reinstall de deps), enquanto editable só re-aponta o entry-point — re-run de `update.sh` em workflow real (várias vezes ao dia em sessões de desenvolvimento) acumula latência sem ganho.
- (b) **Não elimina o vetor L13** — `~/.local/bin/drive-sync` continua como prefixo compartilhado vulnerável a `pip install -e .` contra Python de sistema. Snapshot diferente, mesma colisão estrutural.
- (c) Mantém duplicação do ritual (pipx-managed venv + venv-dev-local em CLAUDE.md) que editable colapsa em um caminho só.

### Release tag + wheel publicada (multi-machine production pattern)

`hatch build` produz wheel; install via `pipx install drive-sync-<version>.whl`. Profissional para projeto multi-máquina ou ≥2 operadores. **Recusada por YAGNI**: single-user atual, projeto syncado pelo próprio drive-sync para mesmo path em todas as máquinas. Gatilho de revisão registrado.

### Manter `pipx install --force` + reinstall manual a cada `git pull`

Status quo. **Recusada**: dor empírica recorrente (manifesta a cada merge: hoje 2026-06-01, descoberta original 10/mai 2026 quando `drive-sync --status` recém-mergeado falhou com `unrecognized arguments`); vetor L13 permanece estruturalmente vulnerável.

### Detection logic em install.sh (`--force` condicional vs. sempre)

`if pipx list | grep -q drive-sync; then pipx install -e --force "$PROJECT_DIR"; else pipx install -e "$PROJECT_DIR"; fi` — usa `--force` apenas em re-run, mantém cold install semanticamente limpo (sem `--force` em estado vazio). **Recusada**: `--force` em cold install é no-op idempotente (não há instalação prévia para forçar sobre); ramificação aumenta superfície de manutenção (testar 2 caminhos vs. 1) sem ganho real. `pipx install -e --force` sempre é determinístico e cobre os dois cenários sem condicional.

## Gatilhos de revisão

- **Cenário multi-máquina compartilhada emerge** (≥2 operadores no mesmo projeto, OU paths diferentes do checkout por host): considerar release tag + wheel publicada via `hatch build` (já é dev dependency). Sinal: ≥1 operador adicional reportando setup OU ≥1 host com checkout em path diferente.
- **`pipx install -e` ganha idempotência nativa** (upstream pipx evolui para re-run sem `--force`): remover `--force` do install.sh. Sinal: changelog pipx documenta mudança ou issue upstream fechado.
- **`pip install -e .` contra Python de sistema causa regressão** apesar de pipx editable como padrão (operador legacy, cross-repo, ou novo workflow não-coberto): elevar warning de volta em CLAUDE.md OU adicionar detection no install.sh. Sinal: ≥1 incidente de symlink overwritten após ADR-009 aceito — `pipx list` reportando warning `symlink missing or pointing to unexpected location` OU `which drive-sync` retornando arquivo regular em vez de symlink, em sessão `journalctl --user -u drive-sync` com `ModuleNotFoundError: No module named 'drive_sync'`.
- **Editable amarração ao path causa dor empírica** (operador moveu o checkout, ou caminho difere entre máquinas no setup syncado): considerar entry-point indireto (wrapper script) ou voltar para snapshot install. Sinal: ≥1 reporte concreto de "movi o projeto e quebrei o daemon".

## Referências

- Plano de execução: [`.claude/local/plans/install-pipx-editable-update.md`](../../.claude/local/plans/install-pipx-editable-update.md) (modo local — não versionado).
- ADRs relacionados (pattern de elevação de workflow decisions a ADR): [ADR-002](ADR-002-relaxar-hardening-systemd-protondrive.md) (relaxar hardening systemd), [ADR-003](ADR-003-type-notify-sinalizacao-degraded.md) (Type=notify para sinalização degraded).
- Itens BACKLOG.md consolidados (pré-`/triage` de hoje): L13 (`install: investigar regressão do symlink ~/.local/bin/drive-sync`) + L15 (`install: trocar pipx install --force por pipx install -e .`) — ambos removidos de `## Próximos` no commit deste ADR + plano.
- Probe empírico documentado: 2026-06-01 — `pipx install -e <path>` em estado já instalado falha com "already seems to be installed. Pass --force to force installation".
