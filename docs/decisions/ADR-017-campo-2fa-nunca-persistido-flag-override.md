# ADR-017: Campo `2fa` nunca persistido — `--protondrive-2fa ""` forçado + recovery interativo

**Data:** 2026-08-27
**Status:** Aceito

## Origem

- **Tracker:** [#61](https://github.com/fppfurtado/drive-sync/issues/61) — braço **J4/SP-T7** do Spec
  `specs/infra-flakiness-vs-real-failure.md` (build do #35), **deferido** no build por um risco não
  previsto (ver abaixo). Emenda o **playbook de recovery** do [ADR-003](ADR-003-type-notify-sinalizacao-degraded.md).
- **Finding do spike SP-T1** (`docs/spikes/SP-T1-2fa-cold-reauth.md`): o backend protondrive **exige**
  o campo `2fa` no cold reauth com 2FA-enabled, MAS um valor **estático** guardado no `rclone.conf`
  **nunca satisfaz** esse requisito na prática — um TOTP tem ~30s de validade e um cold reauth quase
  sempre ocorre minutos/horas após o config ter sido escrito. Logo o campo estático nunca "funciona"
  num cold reauth real; seu único efeito observável é produzir o `8002` enganoso ("invalid_credentials")
  ao re-submeter um código expirado — o **landmine** dos incidentes 2026-06-23 e 2026-05-11.
- **Gerador do landmine:** o próprio playbook de recovery do ADR-003
  (`rclone config update proton 2fa <code>` + restart) **grava** o campo estático que depois expira.
  A recuperação plantava a próxima falha.

## Decisão

1. **Forçar `--protondrive-2fa ""` (string vazia explícita) em TODA invocação rclone** do drive-sync,
   via `_base_cmd()` em `sync_engine.py` (constante `_FORCE_EMPTY_2FA`). O flag explícito-vazio
   sobrescreve o valor do config (precedência flag > config no rclone) **sem que o drive-sync jamais
   escreva o `rclone.conf`**. Todos os 5 sites de invocação passam por `_base_cmd()` — o `mkdir`, que
   antes bypassava, foi roteado por ele.

2. **O drive-sync NUNCA escreve o `rclone.conf` para manipular o campo `2fa`.** Isso descarta por
   construção a corrida de escrita concorrente que deferiu o SP-T7 (o rclone reescreve os tokens
   refreshados no mesmo arquivo; um safe-edit coordenado seria frágil e sem protocolo de lock).

3. **Recovery de 2FA genuíno passa a ser reauth interativo** (emenda o playbook do ADR-003 para o kind
   `invalid_credentials`/`captcha_required`): o operador re-estabelece tokens frescos com o TOTP usado
   **ao vivo** dentro da janela de ~30s (`rclone config reconnect proton:` ou re-login interativo) —
   **nunca** persistindo o código no config. Uma vez com tokens frescos válidos, o refresh morno do
   daemon opera normalmente e o campo `2fa` não é consultado.

## Consequências

- **Positivas:**
  - Dissolve a corrida de escrita (a razão do defer) — zero escrita do `rclone.conf` pelo daemon.
  - Stateless: sem lógica de "detectar auth-sucesso → limpar campo"; sem estado; sem gatilho.
  - Ataca o **gerador** na raiz — o playbook não planta mais o landmine.
  - Melhora o sinal: um cold reauth 2FA-required agora produz erro claro em vez do `8002` enganoso,
    que a classificação context-aware (ADR-016) já não lê como credencial-genuína sob storm.
- **Negativas / trade-offs:**
  - **Muda a UX de recovery** que o operador executa em incidente (não mais setar o campo `2fa`).
    Documentado no invariante ADR-003 do CLAUDE.md e no playbook. Aceito: o caminho antigo já era
    semi-quebrado (o campo estático não funcionava no cold reauth).
  - **Depende da semântica flag-vazia-sobrescreve-config** do rclone. Confiança **documentária**
    (precedência flag > config; a flag explícita marca `Changed=true` no pflag) — o mesmo grau de
    confiança com que SP-T1 resolveu a Q-A, sem live-auth probe (forçar cold reauth exigiria matar os
    tokens vivos + arriscar o CAPTCHA gate da Proton). **Calibração viva:** o próximo cold reauth real
    confirma o comportamento — o erro observado passa de `8002` para "2FA required". Invalidador: se um
    cold reauth ainda produzir `8002` com o flag ativo, a premissa de override caiu → reavaliar
    (env var `RCLONE_PROTONDRIVE_2FA` ou remoção one-shot em janela sem-tráfego).

## Alternativas consideradas

- **(A) Safe-edit do `rclone.conf` pós-auth** — o mecanismo original do SP-T7. Rejeitado: corre com o
  rclone reescrevendo tokens no mesmo arquivo, sem protocolo de lock; frágil e o motivo do defer.
- **(C) Zerar o campo só em janela sem-tráfego** — ainda escreve o config (estado + timing); mais
  código, mesma classe de risco de (A) em menor probabilidade. Rejeitado por (B) ser stateless.

## Nota operacional (campo estático vivo)

No host onde este ADR aterra, o `rclone.conf` pode ainda conter um `2fa = <estático>` de um recovery
passado. Com esta decisão o campo fica **inerte** (sobrescrito a vazio em toda chamada), então não há
urgência; removê-lo à mão (`rclone config update proton 2fa ""` **com o daemon parado**, ou editar o
arquivo) é higiene opcional.
