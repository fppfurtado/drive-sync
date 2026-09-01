# ADR-021: Coverage-audit Model A — roots declaradas + allowlist glob

**Data:** 2026-09-01
**Status:** Aceito

## Origem

- **Tracker:** #77 — a deferral armada do #56/[ADR-015](ADR-015-audit-cobertura-orfaos-config-time.md)
  disparou.
- **Frame:** `briefs/coverage-audit-orfaos.md` (v2, FROZEN) — §Deliberate exclusions deferiu o
  "Modelo-universo A" com `Trigger-source:` a decisão de #55 (espelho-por-default). O gate disparou:
  #55/[ADR-020](ADR-020-espelho-por-default-remote-subpath.md) landou (PR #72, 2026-09-01).
- **2 instâncias reais da classe** que o Modelo B não pegava: `pictures/Screenshots` (#54, era sibling
  — pego por B) e **`~/mneme` (#74), filho-direto de `$HOME`** — nenhum `local_path` torna `$HOME`
  visível ao Modelo B, então o ponto-cego ficou. Confirmado ao vivo: `drive-sync --check` NÃO
  flagrava `~/mneme`.

## Contexto

[ADR-015](ADR-015-audit-cobertura-orfaos-config-time.md) entregou o audit de cobertura pelo **Modelo B**
("siblings de configurados"): o universo-a-escanear = os diretórios-**pais** dos `local_path`
configurados. Isso cobre o caso em que o órfão nasce **sibling** de algo já coberto (o incidente
Screenshots). Mas deixa um **ponto-cego estrutural**: um diretório de topo que **não tem nenhum sibling
configurado** nunca entra no universo de B. O caso canônico é `$HOME` — só entra no scan de B se algum
folder for configurado com `local_path` diretamente sob ele; hoje `~/mneme` é esse folder, mas isso é
**incidental** (remover/renomear o folder mneme reabre o ponto-cego).

Dois jobs, o mesmo redesign de universo:

1. **Ponto-cego (Model A):** dar um universo de cobertura **declarado** que independa da presença
   incidental de um folder sob a raiz.
2. **Ruído:** o ato de trazer `$HOME` ao universo (via mneme, hoje, ou via root declarada) faz
   `--check` cuspir dezenas de dotdirs de env-stack (`~/.cache`, `~/.config`, `~/.local/share/*`,
   `~/.rustup`…) que **não são trabalho do drive-sync** (território chezmoi/dotfiles). Medido: **76+
   órfãos** com `$HOME` no universo e allowlist vazia. O sinal real afoga. O Model A é inútil sem uma
   allowlist robusta.

## Decisão

### 1. Model A — `coverage_audit.roots` (universo declarado)

Adicionar `roots: list[Path]` a `CoverageAuditConfig`. Em `audit_coverage_orphans`, o universo-a-checar
passa a ser a **união**:

```
parents = { pai(local_path) : folder configurado }   (Modelo B, #56)
        ∪ { root : root em coverage_audit.roots }      (Modelo A, este ADR)
```

O resto do scan é idêntico (filho-diretório com conteúdo, não-coberto, não-allowlisted → órfão). Uma
root declarada é escaneada **independente** de haver folder configurado sob ela — desacopla o audit de
`$HOME` da presença incidental do folder `~/mneme` (config churn não reabre o ponto-cego). `roots: []`
(default) = comportamento #56 puro (só Modelo B), **backward-compatible**.

### 2. Allowlist glob (`coverage_audit.allow` aceita padrões)

`allow` passa a aceitar **duas formas**, distinguidas pela presença de magic chars (`*`, `?`, `[`):

- **Literal** (como #56): casa **exato ou subpath** (ex.: `/storage/tools` cobre tudo sob ele).
- **Glob** (novo): casado por `fnmatch` contra o path resolvido do filho (ex.: `~/.*` silencia todos
  os dotdirs de `$HOME`; `~/.local/share/*` silencia o env-stack sob ele).

Isto torna a root `$HOME` **usável**: em vez de enumerar ~90 dotdirs um a um (inviável, frágil), o
operador escreve um punhado de padrões. Converge com a governança de **exclusão-por-exceção** do
#55/ADR-020 (§Decisão item 4 já roteia exclusões deliberadas para `coverage_audit.allow`).

**Backward-compatible:** entries sem magic char mantêm exatamente a semântica exato/subpath do #56.

## Alternativa rejeitada — auto-skip de dotdirs por heurística

Pular automaticamente filhos `.`-prefixados resolveria parte do ruído sem config nova. **Rejeitada**
por dois motivos: (a) **insuficiente** — não pega `~/.local/share/*` (os filhos ali não são dotdirs:
`pipx`, `mise`, `containers`…) nem os não-dot (`Downloads`, `VirtualBox VMs`, `actions-runner`,
`ProgramasRFB`…), que também afogam o sinal; (b) **mágica implícita** — decidir cobertura por uma
convenção de nome que o operador não declarou viola o princípio "controle do operador, não inferência"
(ADR-010/011/015 N2). O glob é **explícito, soberano e portável** (é só config), e é a leitura natural
do job-de-ruído do #77 ("allowlist robusta, convergente com #55").

## Consequências

- **`--check` volta a ser legível com `$HOME` no escopo.** Medido no FS real: 76 → 5 órfãos com um
  conjunto de globs razoável, e `~/mneme` corretamente **não**-flageado (coberto).
- **O operador precisa popular `allow` ao declarar `$HOME` como root.** Escanear `$HOME` é
  "exclusão-por-exceção" invertida (quase tudo é env-stack, fora do drive-sync) — custo consciente,
  documentado em `config.yaml.example`. Alternativa mais barata para quem não quer manter a allowlist:
  declarar `/storage` como root (já quase todo coberto por B) e manter `~/mneme` visível via seu
  próprio folder (aceitando o resíduo de config-churn). Decisão do operador, por config.
- **Limitação da heurística glob (aceita, low-severity).** `_is_glob` classifica por
  presença de `*?[`. (a) Um path **literal** cujo nome de dir contém esses chars (raro —
  `~/Downloads/[torrents]`) é lido como glob e o `fnmatch` não casa → a exclusão falha
  silenciosamente (workaround: allowlistar o pai sem o metachar). (b) `fnmatch` não trata
  `/` especial, então um glob amplo (`~/.*`) casa **através de `/`**, silenciando também
  conteúdo aninhado — na direção de exclusão é conservador-seguro, mas o operador deve
  saber que `~/.*` esconde tudo sob qualquer dotdir, não só os dotdirs diretos. Não
  migrado para `PurePath.match` (que trata `/`) porque isso quebraria a conveniência
  `~/.local/share/*` e a convergência com os globs rclone do #55.
- **Symlinks em `$HOME` apontando para `/storage` são resolvidos pelo alvo.** `audit_coverage_orphans`
  `.resolve()`a cada filho (herdado de #56); um `~/projects → /storage/dev/projects` é reportado como
  o path `/storage/...`. Correto (dedup por path real), mas pode surpreender — na prática o alvo
  costuma já estar coberto pelos folders de `/storage`, então não flageia.
- **Arm runtime (N3 do brief) segue deferido** — este ADR é config-time (`--check`), como #56. Um
  órfão nascido com o daemon já rodando e config inalterado só é pego no próximo `--check`. Irmão de
  #37 (runtime probe de case-dup).

## Invariante (resumo para CLAUDE.md)

Órfãos de cobertura (`drive-sync --check`, warn não-fatal) agora escaneiam **dois universos unidos**:
Modelo B (pais dos `local_path` — #56) **∪** Modelo A (`coverage_audit.roots` declaradas — este ADR),
fechando o ponto-cego de top-level-sem-sibling (o caso `~/mneme` filho-direto de `$HOME`, #74).
`coverage_audit.allow` aceita **path literal** (exato/subpath) **e glob** (`*?[`, via fnmatch) — o glob
é o que torna `$HOME`-como-root usável (silencia os dotdirs de env-stack sem enumerá-los), convergindo
com os `exclude:` do #55/ADR-020. `roots: []` default = comportamento #56 puro.
