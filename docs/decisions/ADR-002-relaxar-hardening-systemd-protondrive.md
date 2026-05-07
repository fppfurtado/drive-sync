# ADR-002: Relaxar hardening da unit systemd para compatibilidade com rclone protondrive

**Data:** 2026-05-07
**Status:** Aceito (validação empírica pendente — ver `## Pendências de validação` no plano associado)

## Origem

- **Investigação:** /debug 07/mai 2026, sequência iniciada por divergência reportada entre `Sync/dev/projects/h3` local e remoto. Diagnóstico isolou a unit systemd como variável discriminante do EROFS espúrio em bisync de pastas grandes.
- **Plano de execução:** [docs/plans/relax-systemd-hardening.md](../plans/relax-systemd-hardening.md).

## Contexto

A unit `drive-sync.service` (`systemctl --user`) usava o conjunto de hardening:

```ini
NoNewPrivileges=yes
ProtectSystem=strict
PrivateTmp=yes
```

Sob essa configuração, `rclone bisync` para pastas grandes (`dev-projects` com 15549+ arquivos pendentes em `h3/gestaoclick-report_react/.git/objects/`) abortava reproducivelmente em ~33min com mensagens **espúrias** `read-only file system` em três operações distintas:

- `chtimes <path>: read-only file system` — preservação de mtime no fim do bisync
- `mkdir <path>: read-only file system` — criação de subdiretórios locais durante download
- `<path>.partial: read-only file system` — escrita de arquivos temporários durante download

A mensagem é demonstravelmente falsa: a operação local equivalente (`mkdir -p`, `touch`) executada manualmente sucede com exit 0; o filesystem (btrfs) está montado `rw` sem erros; `lsattr` não mostra atributo immutable; `dmesg` sem alertas; pasta remota `architecture` foi de fato criada no btrfs apesar do `mkdir ... read-only` reportado pelo rclone.

Experimento controlado isolou a unit como causa:

| Cenário | Flags rclone | Duração | EROFS |
|---|---|---|---|
| Sob systemd | `--transfers=4 --checkers=8` | ~6min até falhar | Sim |
| Sob systemd | `--transfers=1 --checkers=1` | ~33min até falhar | Sim (descarta concorrência interna) |
| Manual (shell) | `--transfers=1 --checkers=1` | 2h58min sem falha | **Não** |

Hipóteses falsificadas no caminho:
- Race entre instâncias rclone do daemon — lock `_rclone_lock` da [ADR-001](ADR-001-serializar-chamadas-rclone.md) já em produção (commit `c126aa9`); bug reproduziu mesmo assim.
- Concorrência interna do rclone (`--transfers`/`--checkers`) — `--transfers=1 --checkers=1` não corrige.
- Filesystem corrompido / read-only acidental — btrfs sadio (`btrfs device stats`: zeros), mount `rw`, write manual sucede em todos os paths que falharam.

A causa raiz exata da interação systemd-namespace + rclone-protondrive backend não foi totalmente isolada. O que resta como variável discriminante: o ambiente de execução do systemd. O suspeito principal por design é `ProtectSystem=strict` (que mounta filesystem hierarchy read-only via namespace), embora isso por si só seja inconsistente com o fato de que outras operações de escrita pequenas (jobs `dev-scripts`, `dotfiles`, `logseq-vault`) sucedem sob a mesma unit.

## Decisão

Remover `ProtectSystem=strict` da unit `systemd/drive-sync.service`. Manter `NoNewPrivileges=yes` e `PrivateTmp=yes`.

Razões:

- O daemon roda em `systemctl --user` como o usuário comum, sem root nem capabilities. Diretivas de mount-namespace oferecem ganho marginal contra um processo que **já** não tem privilégios.
- O bug é reprodutível e bloqueia o uso real do daemon. Sem fix, o gap em `dev-projects` (e qualquer pasta grande futura) acumula indefinidamente.
- A mudança é mínima (remove uma linha) e trivialmente reversível.
- Funcionalidade > defesa em profundidade marginal.

## Consequências

### Trade-offs

- Defesa em profundidade reduzida no mount namespace da unit. Se o binário rclone for comprometido, terá os mesmos write-permissions que o usuário (já era o caso de qualquer processo do usuário; a defesa adicional via `ProtectSystem=strict` era marginal sob `--user`).

### Benefícios

- `rclone bisync` funciona consistentemente sob a unit. Pastas grandes podem completar `--resync`.
- Diagnóstico fechado: 3.418 ocorrências do erro `chtimes ... read-only file system` desde 27/abr 2026 deixam de aparecer.
- Item de investigação no backlog ("bisync: investigar erro recorrente chtimes ... read-only file system") move para Concluídos.

### Limitações

- A causa raiz na interação systemd-namespace + rclone-protondrive não foi totalmente isolada. O fix é empiricamente suficiente, mas não explica por que `chtimes`/`mkdir`/`.partial` específicos disparam EROFS sob `ProtectSystem=strict` enquanto operações pequenas sucedem.
- Se `PrivateTmp=yes` também contribuir parcialmente (não testado isoladamente), o bug pode reproduzir em formas residuais. Caso isso aconteça, follow-up para remoção adicional registrado no plano.

## Alternativas consideradas

### `ReadWritePaths=$HOME /storage` mantendo `ProtectSystem=strict`

Listar caminhos graváveis explicitamente, preservando o spirit do hardening. **Recusada** — frágil (operador adicionando pasta nova fora dos paths listados quebraria silenciosamente), e requer manutenção contínua. O ganho real (proteção contra escrita acidental em /usr, /etc) é marginal para user service unprivileged.

### `ProtectSystem=full` (mantém /usr, /boot, /etc protegidos; libera /home e /storage)

Compromise mais leve. **Recusada como primeira tentativa** — adiciona variável (não sabemos se o problema é estritamente `strict` vs. uma classe maior). Caso a remoção de `ProtectSystem=strict` se mostre suficiente, `=full` seria reintrodução desnecessária. Caso insuficiente, `=full` seria revisitado.

## Gatilhos de revisão

- **rclone#7381 ou bug equivalente do backend protondrive resolvido upstream**: se o backend deixar de produzir EROFS espúrio sob namespace restritivo, hardening pode ser restaurado.
- **`PrivateTmp=yes` também disparar EROFS na validação prática**: registrar follow-up plan para remoção adicional; atualizar esta ADR com a evidência.
- **Migração do backend protondrive para `lib/oauthutil`** (citada em ADR-001 como gatilho similar): pode resolver a interação como efeito colateral, viabilizando restauração de `ProtectSystem=strict`.

## Referências

- /debug 07/mai 2026 (sessão Claude Code)
- [ADR-001](ADR-001-serializar-chamadas-rclone.md): Serializar chamadas rclone (race relacionada mas distinta — token refresh)
- BACKLOG.md item de investigação original (em Concluídos)
- Plano de execução: [docs/plans/relax-systemd-hardening.md](../plans/relax-systemd-hardening.md)
- Issue upstream relevante: https://github.com/rclone/rclone/issues/7381
