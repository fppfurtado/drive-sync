# Spike SP-T1 — o backend protondrive exige o campo `2fa` em todo cold reauth?

- Spec: `specs/infra-flakiness-vs-real-failure.md` (SP-T1, gate de SP-T7 / J4)
- Data: 2026-08-27
- Método: introspecção do rclone (documentário) + comportamento observado nos 2 incidentes (behavioral). Teste empírico ao vivo (forçar cold reauth) **não executado** — exigiria matar os tokens vivos e o TOTP fresco do operador (risco).

## Evidência

- rclone **v1.74.3**, backend `protondrive`. Opção `--protondrive-2fa`: *"The 2FA code of your proton drive account if the account is set up with two-factor authentication."* — `Required: false`.
- O remote `proton` cacheia tokens de sessão: `client_uid`, `client_access_token`, `client_refresh_token`, `client_salted_key_pass`.
- **Refresh morno** (tokens válidos) reusa os tokens sem re-login → `2fa` não é consumido (incidente 2026-08-26/27, F2: restart pós-outage limpou sem reauth).
- **Cold reauth** (tokens mortos, ex.: `client_uid` invalidado pela race do rclone#7381 sob storm) re-roda o SRP login e, com 2FA-enabled, submete o campo `2fa` (incidente 2026-06-23, F3 — o campo estático estava expirado → `8002`/422, o landmine).

## Finding

1. O campo `2fa` é consumido **apenas** no cold reauth com tokens mortos E conta 2FA-enabled — nunca no refresh morno.
2. Um `2fa` **estático** guardado no config é **inútil no cold reauth** na prática: um TOTP tem ~30s de validade, e um cold reauth quase sempre ocorre minutos/horas após o config ter sido escrito. Logo o campo estático nunca "funciona" num cold reauth real — só produz o `8002` enganoso ao re-submeter um código expirado.
3. **Consequência para SP-T7 (J4):** limpar/zerar o campo pós-auth **não perde funcionalidade real** (o valor estático já não servia no cold reauth) e **melhora o sinal** (campo ausente → erro claro "2FA required" em vez do `8002` "invalid_credentials" enganoso).

## Verdito Q-A: RESOLVIDA (alta confiança documentária+comportamental)

Sim, o backend exige `2fa` no cold reauth (2FA-enabled) — MAS um valor estático não satisfaz esse requisito na prática, então limpá-lo é seguro.

## Risco NOVO surfaçado (fora do escopo previsto pelo Spec) → SP-T7 DEFERIDO

O Spec SP-T7 assumia um "limpar o campo" simples. Porém o **rclone reescreve os tokens refreshados no `rclone.conf`**; o drive-sync editar o mesmo arquivo para zerar `2fa` introduz uma **corrida de escrita concorrente** sobre o `rclone.conf` (risco de corromper os tokens que o rclone acabou de gravar). Um safe-edit coordenado (ou um mecanismo alternativo — ex.: env var `RCLONE_PROTONDRIVE_2FA` vazia, ou zerar só em janela sem tráfego) não está especificado.

**Disposição:** SP-T7/J4 **não é buildado neste ciclo** — surfaçado como finding para um mini-ciclo próprio (frame/design do safe-clear). O núcleo J1-J3 (SP-T2..T6) é independente e segue. O `8002` espúrio já deixa de ser lido como credencial-genuína via a classificação context-aware (SP-T3) mesmo sem o fix do landmine — J4 é defesa-em-profundidade adicional, não o núcleo.
