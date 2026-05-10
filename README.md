# drive-sync

Sincronização bidirecional de pastas locais com **Proton Drive** no Linux Fedora 43,
usando `rclone bisync` por baixo, `inotify` (via `watchdog`) para detecção de
mudanças, `git bundle` para projetos Git, e `systemd --user` para auto-start.

> Por que essa abordagem? A Proton ainda não distribui um cliente de
> sincronização nativo para Linux (anunciado para 2026). O `rclone` tem backend
> oficial para Proton Drive e é a alternativa madura disponível hoje.

---

## Mapeamento dos requisitos → onde está implementado

| Requisito | Implementação |
|---|---|
| Sincronização bidirecional, ponta faltante recebe a versão | `sync_engine.RcloneEngine.bisync_folder` (usa `rclone bisync --conflict-resolve newer`) |
| Conflito → vence o mais recente | flag `--conflict-resolve newer --conflict-loser delete` |
| Adicionar/remover pastas facilmente | `config.yaml` declarativo + `scripts/drive-sync-folder add/rm/list` |
| Inicia com o SO | `systemd/drive-sync.service` (unit `--user`) + `loginctl enable-linger` |
| Funciona transparente, sem atrapalhar | `Nice=10`, `IOSchedulingClass=best-effort`, jobs concorrentes limitados |
| Sync assíncrona, arquivo grande não bloqueia outros | `daemon.py` usa `asyncio.Queue` + N workers + `asyncio.Semaphore`; cada pasta é um job independente |
| Não esforço duplicado em subpastas | `watcher.owning_folder` + `dedupe.skip_subpaths_of_configured_folders` |
| Logs com rotação | `logging_setup.CompressingRotatingFileHandler` (rotação por tamanho + gzip) |
| Detecta projeto Git e sobe só o `git bundle` | Disponível como `git_mode: bundle` (opt-in); padrão é `bisync` que sobe worktree completo |
| Conflito de bundle → vence o mais novo | `daemon._sync_git_folder` compara `repo_last_modified` × mtime do bundle remoto |
| Projetos Git dentro de projetos Git | `git_handler.find_git_repos` (modo `bundle`) ou pelo próprio bisync (modo `bisync`) |
| Excludes automáticos para artefatos de build | `exclude_presets.default_excludes_for_code` aplicado quando `auto_exclude: true` |
| Parametrização ampla | `config.example.yaml` cobre rclone, pastas, git, watcher, dedupe, logging |

---

## Instalação no Fedora 43

```bash
git clone <este-repositorio> drive-sync
cd drive-sync
bash scripts/install.sh
```

Depois disso, configure o remote rclone uma única vez:

```bash
rclone config
# > n  (new remote)
# > name: drive
# > storage: <número correspondente a "Proton Drive">
# Forneça e-mail, senha e (se houver) 2FA.
```

> **Importante:** o rclone exige que você já tenha feito login pelo
> webclient do Proton ao menos uma vez para que as chaves de criptografia
> sejam geradas — caso contrário a autenticação falha.

Edite suas pastas:

```bash
${EDITOR:-nano} ~/.config/drive-sync/config.yaml
```

Valide e suba o serviço:

```bash
drive-sync --check
systemctl --user start drive-sync
journalctl --user -u drive-sync -f
```

Para uma visão agregada do estado das pastas (sem precisar abrir o
journal), use `drive-sync --status` — snapshot one-shot com a última
sincronização e estado de inicialização por pasta:

```text
$ drive-sync --status
# drive-sync status v1 — formato textual não-estável, use --json quando disponível

Folder                          Initialized  Last sync         Remote
------------------------------  -----------  ----------------  ------------------------
/storage/3. Resources/Projects  yes          2026-05-10 06:48  proton:Sync/dev/projects
~/Videos                        yes          2026-05-09 11:42  proton:Sync/videos
```

---

## Gerenciar pastas via CLI

```bash
# Lista o que está configurado
drive-sync-folder list

# Adiciona uma nova pasta-tarefa
drive-sync-folder add \
  --name fotos \
  --path ~/Imagens \
  --remote Fotos \
  --git-mode off \
  --exclude '*.tmp' '.thumbnails/**'

# Remove
drive-sync-folder rm --name fotos
```

O serviço é reiniciado automaticamente após `add`/`rm`.

---

## O que pode ser parametrizado (visão geral)

Tudo no `config.yaml`:

- **rclone**: nome do remote, raiz remota, binário, flags globais (transfers, retries, etc.).
- **folders[]**: por tarefa — caminho local, subpath remoto, `enabled`,
  `git_mode` (`off`/`bisync`/`bundle`), `auto_exclude`, padrões `exclude`,
  `debounce_seconds`.
- **git**: diretório dos bundles, sufixo, `bundle_all`, `recursive_detection`,
  `max_recursion_depth`.
- **watcher**: tamanho da fila, `max_concurrent_jobs`, sync periódica de
  rede de segurança, atraso inicial.
- **dedupe**: ligar/desligar a deduplicação de subpastas.
- **logging**: nível, arquivo, `max_bytes`, `backup_count`, eco no console.

---

## Notas técnicas relevantes

### rclone bisync × rclone sync
`rclone sync` é unidirecional (espelha A→B). Para "ponta faltante recebe a
versão E em conflito vence o mais recente", o correto é `rclone bisync`,
que mantém estado em `~/.cache/rclone/bisync/`. Na primeira execução de
um par é necessário `--resync` para construir esse estado — o engine faz
isso automaticamente via marker file.

### `git_mode`: `off` × `bisync` × `bundle`

A taxonomia mudou na v0.2 e vale entender quando usar cada um:

- **`off`** — bisync puro, sem nenhum tratamento de Git. Use para pastas
  que não têm código (Documents, Pictures, Videos).
- **`bisync`** (padrão) — bisync mais a aplicação automática de excludes
  comuns de artefatos de build (`node_modules/**`, `__pycache__/**`,
  `target/**`, `.venv/**` etc.). Use para todas as pastas com código.
  O worktree completo é sincronizado, incluindo arquivos não commitados,
  e o `.git/` vai junto — o repo permanece utilizável tanto local quanto
  na nuvem.
- **`bundle`** — empacota cada repositório com `git bundle create --all` e
  sobe só o arquivo binário. **Opt-in.** Use apenas quando você tiver
  repos com `.git/` muito grande (anos de histórico, blobs versionados),
  e o custo de sincronizar arquivo a arquivo for proibitivo. Trade-off
  importante: bundle não inclui worktree nem mudanças não commitadas — só
  o histórico publicado. Para a maioria dos casos, `bisync` ganha.

### Por que NÃO empacotamos repos Git por padrão?

A primeira versão deste projeto usava `git bundle` para todos os repos,
inspirada na ideia de "menos chamadas de API à Proton". Mas isso traz dois
problemas: (1) mudanças não commitadas não viajam — você precisa sempre
lembrar de commitar antes de trocar de máquina; (2) na nuvem, o repo vira
um único arquivo opaco, sem possibilidade de navegar pelo webclient.
A v0.2 inverteu o padrão: bisync com excludes inteligentes resolve a
maioria dos casos com a melhor experiência. `bundle` ficou como ferramenta
para casos extremos.

### Repositórios aninhados
`find_git_repos` faz DFS e **continua** descendo mesmo após encontrar um
`.git`. Cada repo vira um bundle separado, com hierarquia espelhada em
`~/.cache/drive-sync/bundles/<tarefa>/<rel>/<nome>.gitbundle`.
Isso significa que um monorepo com submódulos ou um workspace com vários
repos lado a lado é tratado corretamente.

### Critério de "mais novo" para bundles
Usamos `mtime` de `.git/HEAD`, `.git/packed-refs` e tudo dentro de
`.git/refs/` — não os arquivos do worktree. Mudanças não commitadas
**não** disparam regeração do bundle, o que é o comportamento desejado
(você quer sincronizar o histórico, não rascunhos).

### Limitações conhecidas do backend drive do rclone
- O backend não preserva `mtime` ainda; `bisync` usa hash + tamanho como
  fallback. Em pastas com muitos arquivos, a primeira `--resync` pode
  demorar. Isso melhora à medida que a Proton avança no SDK 2026.
