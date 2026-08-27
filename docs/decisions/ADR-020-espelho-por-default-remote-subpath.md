# ADR-020: Espelho-por-default (mirror-by-default) para `remote_subpath`

**Data:** 2026-08-27
**Status:** Proposto

## Origem

- **Tracker:** #55 (auditoria comparativa dos layouts local `/storage/` × remoto `proton:Sync/` contra
  o perfil do operador — soberania · portabilidade · anti-ceremony · bounded-contexts, 2026-08-26).
- **Escopo deste ADR:** o **princípio de governo** + sua **primeira aplicação** (`library`→`areas`).
  A resolução da taxonomia de `pictures/` (`images`↔`photos`↔`assets` + órfãos) é trabalho distinto,
  rastreada em **#54 Achado-2** (fica de fora deste ciclo — anti-scope-conflation).
- **Restrição herdada:** o incidente de execução de #54 (2026-08-27) provou que renomear um remoto por
  edição de `remote_subpath` **causa `rc=7`** — ver §Consequências.

## Contexto

O remoto hoje é uma **projeção curada** do local, mas diverge por **drift de edição incremental**, não
por design. Duas classes de divergência, de valor oposto:

- **Exclusões — legítimas.** O config exclui blobs pesados externamente-reproduzíveis (VMs, `tools/`,
  ISOs, `3. Resources/` morto). Isto é soberania/quota bem-feita.
- **Renomeações — drift, custam sem pagar.** `areas`(local) → `library`(remoto) é resquício do rename
  PARA `Resources→areas` (o `library` remoto nunca acompanhou); idem `images`→`photos`,
  `Esquemas`→`assets` no ramo `pictures/`. Nenhuma renomeação compra nada e todas cobram: **legibilidade**
  (dois vocabulários na cabeça), **portabilidade** (o backup fala outra língua na restauração) e
  **segurança** (divergência foi o que deixou `Screenshots` cair fora da cobertura — #54).

## Decisão

Adotar **espelho-por-default, exclusão-por-exceção** como princípio de governo do config:

1. **`remote_subpath` DEVE espelhar a estrutura/nome do `local_path`.** O leaf remoto = o nome local.
   (Prefixo estrutural é permitido — ex.: `dev-projects` local `/storage/dev/projects/` → remoto
   `dev/projects` — desde que cada segmento espelhe o local.)
2. **Toda divergência é `exclude:` explícito OU motivo registrado (ADR), nunca renomeação silenciosa.**
3. **Primeira aplicação:** renomear o remoto `library`→`areas` (alinhar ao local `/storage/areas/`).
4. **Exclusões deliberadas documentadas** (via `coverage_audit.allow`, decisão consciente — não órfão):
   - `/storage/tools` — downloads reproduzíveis (re-obteníveis; fora de quota por escolha).
   - `/storage/3. Resources` — legado PARA morto (substituído por `areas/`).
   - `/storage/.Trash-1000` — lixeira, nunca backup.
5. **Cobertura de conteúdo vivo não-declarado:** `/storage/dev/api-collections` (14 itens, barato) →
   **incluir** (novo folder `dev-api-collections` → remoto `dev/api-collections`, espelhado). Soberania:
   conteúdo vivo pequeno entra, não fica órfão.

## Consequências

- **Renome de remoto exige DirMove server-side, NUNCA flip de `remote_subpath`.** O backend protondrive
  anuncia `DirMove: True` — `rclone move proton:Sync/library proton:Sync/areas` renomeia a pasta
  server-side (~instantâneo, sem re-transferir os 3.7G). **Editar `remote_subpath` p/ o nome novo sem
  o move primeiro** faz o bisync tentar **CRIAR** a pasta nova → `422 already exists` (Proton permite
  case/name-duplicate — dois folder IDs distintos observados) → cópia parcial + **`rc=7` abort**
  (incidente #54, 2026-08-27). Runbook seguro em §Runbook.
- **ADR-011 (case-duplicates) reforçado:** a premissa "Proton é case-insensitive ⇒ mesma entry" é
  **parcial** — a checagem de unicidade rejeita a criação (422) mas o listing pode expor duas pastas de
  IDs distintos. Renome sempre server-side.
- **`--check` (ADR-015) para de avisar** os órfãos dispostos (entram em `allow` ou viram folder).
- **Restauração fala a língua do local** — o backup deixa de exigir tradução mental.

## Runbook — renome seguro de remoto (aplicado a `library`→`areas`)

Ordem data-safe (espelha a recuperação bem-sucedida de #54):

1. `systemctl --user stop drive-sync` (pausa; libera o lock serializado — ADR-001).
2. **Verify-antes:** `rclone lsf proton:Sync/areas` deve dar erro/vazio (destino não existe) e
   `rclone size proton:Sync/library` confirma o conteúdo.
3. `rclone move proton:Sync/library proton:Sync/areas` (DirMove server-side).
4. **Verify-depois:** `proton:Sync/areas` tem o conteúdo; `proton:Sync/library` sumiu; parent sem
   duplicata.
5. Config runtime: `library` folder → `name: areas`, `remote_subpath: areas` (local inalterado).
6. `drive-sync --check` (OK) → `systemctl --user start drive-sync`.
7. O par novo (local `areas` ↔ remoto `areas`) faz **first-run `--resync`** — deve ser **no-op**
   (conteúdo idêntico, pasta já existe, sem criação → sem colisão). Confirmar `bisync concluído com
   sucesso` + marker fresco.

## Alternativas consideradas

- **Manter o drift (status quo).** Rejeitado: o custo (legibilidade/portabilidade/segurança) é contínuo
  e a divergência já produziu perda-de-cobertura silenciosa (#54).
- **Renomear o local em vez do remoto** (`/storage/areas`→`library`). Rejeitado: o local é a fonte da
  verdade e `areas` é o vocabulário PARA vivo; `library` é o resquício morto.
- **Re-upload (deletar remoto `library`, resync do local `areas`).** Rejeitado: 3.7G de transferência
  desnecessária + janela de risco, quando DirMove server-side resolve em ~instantâneo.
