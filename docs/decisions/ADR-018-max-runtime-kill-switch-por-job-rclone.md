# ADR-018: Max-runtime kill switch por job rclone

**Data:** 2026-08-27
**Status:** Aceito

## Origem

- **Tracker:** [#45](https://github.com/fppfurtado/drive-sync/issues/45).
- **Incidente 2026-05-29/30:** um único `rclone bisync` do folder `archive` (rodando **sem excludes**,
  iterando `.venv/lib/python3.13/site-packages/pandas/...`) segurou o **lock global serializado**
  ([ADR-001](ADR-001-serializar-chamadas-rclone.md)) por **14h** sem qualquer ação automática. Como
  todo trabalho útil é rclone e o lock serializa as chamadas, **nenhum outro folder sincronizou** durante
  essas 14h. A defesa-em-profundidade existente falhou: `_check_folder_staleness`
  ([ADR-005](ADR-005-folder-staleness-degraded.md)) só **sinalizou** (13 folders degradados) — não **agiu**.

## Decisão

Introduzir um **kill switch por job rclone** com limite de runtime configurável.

1. **`_run(cmd, timeout)`** (`sync_engine.py`) envolve `proc.communicate()` em `asyncio.wait_for`. Se o
   processo não terminar dentro do `timeout`, mata o subprocess (**SIGTERM → graça de
   `_STUCK_JOB_GRACE_SECONDS` (10s) → SIGKILL**, sempre com reap via `proc.wait()`) e levanta
   `StuckJobError`. **Matar o processo aqui, não cancelar a corrotina, é essencial:** o subprocess mora
   neste escopo e é ele quem segura o lock — uma corrotina cancelada deixaria o processo **órfão segurando
   o lock**, sem resolver nada.

2. **Config em dois níveis:**
   - `rclone.max_job_runtime_seconds` — global, **default 7200 (2h)**; `0` desliga.
   - `FolderConfig.max_job_runtime_seconds` — override per-folder; `None` (ausente) herda o global, `0`
     desliga só para aquele folder. Resolução em `RcloneEngine._job_timeout(folder)`.

   O timeout é aplicado a **todos os sites de folder-work** (`bisync`, `mkdir`, `copyto` de bundle);
   o `about` do `auth_probe` fica **sem** timeout (probe leve, roda durante pausa — não deve ser morto).

3. **Sinalização per-folder (reusa [ADR-005](ADR-005-folder-staleness-degraded.md)):** o daemon captura
   `StuckJobError` em **`_process_folder`** (choke-point único que cobre tanto o `_worker` quanto o
   `_periodic_full_sync`), loga `[STUCK_JOB] <folder>: job morto após Xh (max_job_runtime)`, coloca o
   folder em `_degraded_folders` + `Notifier.folder_degraded` + STATUS agregada. **Distinto de auth
   ([ADR-003](ADR-003-type-notify-sinalizacao-degraded.md)):** NÃO pausa os workers — o kill já liberou o
   lock, os outros folders seguem.

4. **Sem auto-resume.** Restart manual continua o recovery (consistente com o invariante `bisync errors
   do NOT auto-recover`). O sucesso no próximo ciclo limpa o degraded (padrão ADR-005).

## Consequências

- **Positivas:** o caso degenerado é pego em ~2h em vez de 14h; o lock é liberado rapidamente para os
  demais folders; reusa a infra de degraded de ADR-005 (sem novo canal de sinalização).
- **Negativas / trade-offs:**
  - **Matar um rclone que ESTAVA progredindo** (só lento, não preso) cria estado parcial que pode exigir
    `--resync` manual. Aceito: cobrir o caso degenerado vale mais que poupar o "muito lento mas correto".
  - **Footgun do default global:** um folder cujo sync **legítimo** excede 2h (ex.: primeira `--resync`
    de `archive`, ~105k arquivos) seria morto a cada ciclo e ficaria **permanentemente degradado**.
    Mitigações: (a) o kill é **visível** como `[STUCK_JOB]` (não silencioso) → o operador vê e ajusta;
    (b) **override per-folder** (`max_job_runtime_seconds: 0` ou um valor maior no folder grande);
    (c) subir o global. Documentado no `config.yaml.example`.

## Alternativas consideradas

- **Enforce no nível do daemon** (`asyncio.wait_for` em volta de `_process_folder`) — **rejeitado**:
  cancelar a corrotina NÃO mata o subprocess; ele ficaria órfão segurando o lock. O kill precisa do
  handle do processo, que vive em `_run`.
- **Pausa global (como auth)** — **rejeitado**: um job preso é problema daquele folder; pausar tudo
  amplificaria o dano em vez de contê-lo. Per-folder degraded é a granularidade correta.
- **Default `0` (opt-in)** — **rejeitado**: derrotaria a defesa-em-profundidade (o incidente aconteceu
  porque nada agia sem config prévia). Default-on em 2h honra a direção do #45; o footgun é coberto pela
  visibilidade + override.

## Limitação conhecida

- **Processo em estado ininterruptível (D):** se o rclone travar numa syscall ininterruptível (ex.:
  I/O de rede/filesystem que não responde), nem o SIGKILL reap imediato — `await proc.wait()` bloquearia
  segurando o lock, reintroduzindo o stall neste caso extremo. Baixa probabilidade (rclone fala com o
  backend via sockets, que são matáveis); não corrigível sem abandonar o reap (arriscando zumbi). Aceito
  como edge de OS fora do controle do daemon.

## Follow-ups deferidos

- Distinguir "preso" de "lento mas progredindo" via progresso real (ex.: `--stats` / bytes transferidos)
  em vez de só wall-clock — evitaria o footgun de matar sync legítimo longo. Mais complexo; deferido.
