"""Manipulação de projetos Git: detecção, empacotamento e desempacotamento.

Regra (do requisito): se a pasta é um projeto Git, sincronizamos um bundle
na nuvem que reflete o ESTADO ATUAL DO WORKTREE — incluindo arquivos
modificados, em stage e untracked. Não apenas commits.

Como isso é feito sem sujar o histórico do usuário:

1. Para cada repo, criamos um commit-snapshot em uma ref isolada
   (refs/drive-sync/snapshot). Esse commit captura tudo: HEAD atual,
   arquivos modificados, em stage e untracked. O processo usa um index
   temporário (GIT_INDEX_FILE) para NÃO mexer no index real do usuário.

2. Geramos o bundle contendo: --all (todo o histórico do usuário) +
   refs/drive-sync/snapshot (o snapshot do worktree atual).

3. Após o bundle, a ref de snapshot é apagada localmente (vive só dentro
   do bundle). O worktree, o index e o HEAD do usuário ficam intactos.

Na restauração:
- Extraímos o bundle, posicionamos HEAD onde o usuário estava,
- E aplicamos os arquivos do snapshot por cima do worktree.

Suporta repositórios aninhados (projeto dentro de projeto).
"""
from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .config import FolderConfig, GitConfig

log = logging.getLogger(__name__)

# Refs isoladas usadas para o snapshot do worktree.
SNAPSHOT_REF = "refs/drive-sync/snapshot"
# Ref legada (pré-#17): bundles gerados pelo código antigo ainda carregam este
# marcador. O restore a apaga defensivamente para não deixar lixo no repo do
# usuário durante a janela de transição; bundles novos nunca a criam (#17).
_LEGACY_HEAD_MARKER_REF = "refs/drive-sync/head-at-snapshot"


# ---------------------------------------------------------------------------
# Classificação de repos para dispatch (ADR-008)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RepoClassification:
    """Resultado da classificação de um repo descoberto sob um folder em `auto`."""

    repo_path: Path
    repo_subpath: str  # relativo ao folder.local_path
    mode: Literal["skip", "bundle"]
    reason: Literal["no_remote", "has_remote", "override"]
    remote_url: str | None  # primeira URL retornada por `git remote -v`, None quando vazio


def _get_remote_url(repo_path: Path) -> str | None:
    """Retorna a primeira URL do output de `git remote -v`, ou None se vazio.

    Output típico: `origin\\tgit@github.com:user/repo.git (fetch)\\norigin\\t...(push)\\n`.
    Stderr não é necessário (sem remote → exit 0 + stdout vazio).
    """
    result = subprocess.run(
        ["git", "-C", str(repo_path), "remote", "-v"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        # Falha do git (não-repo, .git corrupto, etc.) — tratado como sem remote.
        log.debug("git remote -v falhou em %s: %s", repo_path, result.stderr.strip())
        return None
    first_line = result.stdout.strip().split("\n", 1)[0].strip()
    if not first_line:
        return None
    # Forma fixa: `<name>\t<url> (fetch|push)` — segundo campo é a URL.
    return first_line.split(None, 2)[1]


def classify_repos(
    folder: FolderConfig, git_cfg: GitConfig
) -> list[RepoClassification]:
    """Classifica repos descobertos sob folder.local_path conforme ADR-008.

    - `git remote -v` vazio → mode=bundle, reason=no_remote
    - com remote → mode=skip, reason=has_remote (URL capturada pra log)
    - match em folder.repo_overrides → mode=override.mode, reason=override (precedência total)
    - repo sem HEAD: classifica normal (bundle/skip per remote); create_bundle no-op silente
    - worktree linkada → mode=skip, reason=linked_worktree (estrutural, sem consultar
      remote — história vive no repo principal; #24). Segue virando --exclude no bisync
      (invariante ADR-008: repo git nunca é bisyncado). Submodule (.git arquivo com
      gitdir → modules/) classifica normal

    Não chama Notifier nem persiste estado — caller (daemon) gerencia flip detection.
    """
    repos: list[Path] = []
    if git_cfg.recursive_detection:
        repos = find_git_repos(folder.local_path, git_cfg.max_recursion_depth)
    elif is_git_repo(folder.local_path):
        repos = [folder.local_path]

    override_map = {o.repo_subpath: o.mode for o in folder.repo_overrides}

    classifications: list[RepoClassification] = []
    for repo in repos:
        rel = repo.relative_to(folder.local_path) if repo != folder.local_path else Path(".")
        repo_subpath = str(rel) if rel != Path(".") else ""

        if repo_subpath in override_map:
            mode = override_map[repo_subpath]
            remote_url = _get_remote_url(repo)
            log.info(
                "[%s] [REPO_%s] %s (override)",
                folder.name, mode.upper(), repo_subpath or "<root>",
            )
            classifications.append(RepoClassification(
                repo_path=repo, repo_subpath=repo_subpath,
                mode=mode, reason="override", remote_url=remote_url,
            ))
            continue

        if is_linked_worktree(repo):
            log.info(
                "[%s] [REPO_SKIP] %s (linked_worktree)",
                folder.name, repo_subpath or "<root>",
            )
            classifications.append(RepoClassification(
                repo_path=repo, repo_subpath=repo_subpath,
                mode="skip", reason="linked_worktree", remote_url=None,
            ))
            continue

        remote_url = _get_remote_url(repo)
        if remote_url is None:
            log.info(
                "[%s] [REPO_BUNDLE] %s (no_remote)",
                folder.name, repo_subpath or "<root>",
            )
            classifications.append(RepoClassification(
                repo_path=repo, repo_subpath=repo_subpath,
                mode="bundle", reason="no_remote", remote_url=None,
            ))
        else:
            log.info(
                "[%s] [REPO_SKIP] %s (has_remote: %s)",
                folder.name, repo_subpath or "<root>", remote_url,
            )
            classifications.append(RepoClassification(
                repo_path=repo, repo_subpath=repo_subpath,
                mode="skip", reason="has_remote", remote_url=remote_url,
            ))

    return classifications


def detect_repo_mode_flips(
    folder_name: str,
    prev_state: dict[str, str],
    current: list[RepoClassification],
) -> list[tuple[str, str, str]]:
    """Compara classificação atual com prev_state; retorna eventos como (subpath, old, new).

    prev_state vazio (primeiro ciclo pós-restart) → retorna [] (silencioso).
    Log [REPO_MODE_FLIP] em WARNING level — flip é evento "olhe aqui" alinhado com
    a heurística de grep do operador (`journalctl --grep REPO_MODE_FLIP` casa
    com outros eventos WARNING como [FOLDER_DEGRADED] de ADR-005). Caller (daemon)
    dispara Notifier.
    """
    if not prev_state:
        return []
    flips: list[tuple[str, str, str]] = []
    for c in current:
        old_mode = prev_state.get(c.repo_subpath)
        if old_mode is not None and old_mode != c.mode:
            log.warning(
                "[%s] [REPO_MODE_FLIP] %s: %s→%s",
                folder_name, c.repo_subpath or "<root>", old_mode, c.mode,
            )
            flips.append((c.repo_subpath, old_mode, c.mode))
    return flips


def is_git_repo(path: Path) -> bool:
    """Verifica se `path` é a raiz de um repositório Git (tem .git)."""
    return (path / ".git").exists()


def is_linked_worktree(path: Path) -> bool:
    """True se `path` é worktree linkada (`.git` ARQUIVO com gitdir → .git/worktrees/<n>).

    Worktree não é repo autônomo: branches/história vivem no repo principal —
    o bundle do principal já captura os refs; bundlá-la é duplicação GB-escala
    de estado efêmero (#24). Submodule também usa `.git` arquivo, mas com
    gitdir → .git/modules/<n> — retorna False (conteúdo não vive no
    superproject, segue elegível a bundle).
    """
    gitfile = path / ".git"
    if not gitfile.is_file():
        return False
    try:
        content = gitfile.read_text(errors="replace")
    except OSError:
        return False
    for line in content.splitlines():
        if line.startswith("gitdir:"):
            parts = Path(line.split(":", 1)[1].strip()).parts
            return len(parts) >= 3 and parts[-2] == "worktrees" and parts[-3] == ".git"
    return False


def find_git_repos(root: Path, max_depth: int) -> list[Path]:
    """Retorna todos os diretórios .git encontrados, até max_depth níveis.

    Suporta projetos Git dentro de projetos Git: NÃO para a recursão ao
    encontrar um .git — continua descendo para encontrar os aninhados.
    """
    repos: list[Path] = []

    def _walk(current: Path, depth: int) -> None:
        if depth > max_depth:
            return
        if is_git_repo(current):
            repos.append(current)
            # Worktree linkada: entra na lista (o modo auto precisa dela pro
            # --exclude — invariante ADR-008), mas SEM descer no subtree (#30):
            # o exclude dela já cobre tudo abaixo, e descer redescobria repos
            # aninhados que viravam bundles duplicados por worktree.
            if is_linked_worktree(current):
                return
        try:
            for child in current.iterdir():
                if child.is_dir() and child.name != ".git" and not child.is_symlink():
                    _walk(child, depth + 1)
        except (PermissionError, OSError) as exc:
            log.debug("Sem acesso a %s: %s", current, exc)

    _walk(root, 0)
    return repos


def bundle_path_for(repo: Path, source_root: Path, bundles_dir: Path, suffix: str) -> Path:
    """Calcula o caminho do bundle preservando a hierarquia."""
    rel = repo.relative_to(source_root) if repo != source_root else Path(repo.name)
    return bundles_dir / rel.with_suffix(rel.suffix + suffix)


# ---------------------------------------------------------------------------
# Critério de "mais novo": mtime mais recente do worktree (fora de .git/).
# É isso que permite ao daemon reagir a mudanças NÃO commitadas.
# ---------------------------------------------------------------------------
def worktree_last_modified(repo: Path) -> float:
    """Retorna o mtime mais recente de qualquer arquivo do worktree.

    Considera todos os arquivos rastreados E não rastreados, exceto:
    - O diretório .git/
    - Arquivos ignorados pelo .gitignore (que vão ser excluídos do snapshot
      mesmo, então não devem disparar regeração).

    Se o repo for grande, isso evita varrer arquivos enormes ignorados
    como node_modules ou venv.
    """
    result = subprocess.run(
        ["git", "-C", str(repo), "ls-files",
         "--cached",            # arquivos no index
         "--others",            # untracked
         "--exclude-standard",  # respeita .gitignore
         "-z"],
        capture_output=True,
    )
    if result.returncode != 0:
        log.debug("ls-files falhou em %s; usando mtime de .git", repo)
        head = repo / ".git" / "HEAD"
        return head.stat().st_mtime if head.exists() else 0.0

    files = [f for f in result.stdout.split(b"\x00") if f]
    head_mtime = (repo / ".git" / "HEAD").stat().st_mtime if (repo / ".git" / "HEAD").exists() else 0.0

    if not files:
        return head_mtime

    latest = 0.0
    for f in files:
        try:
            m = (repo / f.decode("utf-8", errors="replace")).stat().st_mtime
            if m > latest:
                latest = m
        except OSError:
            continue

    # Considera também o histórico (caso só tenham sido feitos commits sem mexer
    # em arquivos — ex.: rebase sem alterar conteúdo).
    return max(latest, head_mtime)


# ---------------------------------------------------------------------------
# Snapshot do worktree em uma ref isolada (sem mexer no index do usuário)
# ---------------------------------------------------------------------------
def _create_worktree_snapshot(repo: Path) -> bool:
    """Cria um commit em SNAPSHOT_REF capturando o estado atual do worktree.

    Usa um GIT_INDEX_FILE temporário para NÃO tocar no index real do usuário.
    Retorna True se o snapshot foi criado.
    """
    env = os.environ.copy()
    tmp_index = tempfile.NamedTemporaryFile(prefix="proton-snap-idx-", delete=False)
    tmp_index.close()
    # Git rejeita index EXISTENTE vazio ("index file smaller than expected") —
    # em repo sem HEAD nada escreve o index antes do `add -A` e o snapshot
    # falhava (#27). Path inexistente faz o git criar index fresco.
    os.unlink(tmp_index.name)
    env["GIT_INDEX_FILE"] = tmp_index.name

    try:
        # 1. Inicia o index temporário a partir do HEAD atual (se existir).
        head_check = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "HEAD"],
            capture_output=True, text=True, env=env,
        )
        has_head = head_check.returncode == 0
        head_sha = head_check.stdout.strip() if has_head else None

        if has_head:
            r = subprocess.run(
                ["git", "-C", str(repo), "read-tree", "HEAD"],
                capture_output=True, text=True, env=env,
            )
            if r.returncode != 0:
                log.error("read-tree falhou em %s: %s", repo, r.stderr.strip())
                return False

        # 2. Adiciona TUDO (tracked modificados + untracked, respeitando .gitignore).
        r = subprocess.run(
            ["git", "-C", str(repo), "add", "-A", "."],
            capture_output=True, text=True, env=env,
        )
        if r.returncode != 0:
            log.error("add -A falhou em %s: %s", repo, r.stderr.strip())
            return False

        # 3. Escreve a tree.
        r = subprocess.run(
            ["git", "-C", str(repo), "write-tree"],
            capture_output=True, text=True, env=env,
        )
        if r.returncode != 0:
            log.error("write-tree falhou em %s: %s", repo, r.stderr.strip())
            return False
        tree_sha = r.stdout.strip()

        # 4. Cria o commit-snapshot. Tem o HEAD original como pai (se houver),
        #    para que o bundle reuse os objetos do histórico.
        commit_cmd = ["git", "-C", str(repo), "commit-tree", tree_sha,
                      "-m", "drive-sync: worktree snapshot"]
        if has_head:
            commit_cmd += ["-p", head_sha]
        r = subprocess.run(commit_cmd, capture_output=True, text=True, env=env)
        if r.returncode != 0:
            log.error("commit-tree falhou em %s: %s", repo, r.stderr.strip())
            return False
        snapshot_sha = r.stdout.strip()

        # 5. Aponta a ref isolada para esse commit. O index real do usuário
        #    NÃO foi tocado (usamos GIT_INDEX_FILE).
        r = subprocess.run(
            ["git", "-C", str(repo), "update-ref", SNAPSHOT_REF, snapshot_sha],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            log.error("update-ref %s falhou em %s: %s", SNAPSHOT_REF, repo, r.stderr.strip())
            return False

        # Onde o HEAD estava NÃO precisa de ref própria: é o primeiro parent
        # do commit-snapshot (passo 4: commit-tree -p head_sha). A restauração
        # o deriva de SNAPSHOT_REF^ — um ref a menos, e some o edge case que
        # abrigava o 2º bug do #27 (#17).
        return True
    finally:
        try:
            os.unlink(tmp_index.name)
        except OSError:
            pass


def _delete_snapshot_refs(repo: Path) -> None:
    """Remove as refs internas do snapshot do repo.

    A ref do snapshot existe só durante a janela de geração do bundle. A ref
    legada head-at-snapshot pode vir num bundle antigo (pré-#17) restaurado —
    apagada defensivamente (`update-ref -d` em ref inexistente é no-op).
    """
    for ref in (SNAPSHOT_REF, _LEGACY_HEAD_MARKER_REF):
        subprocess.run(
            ["git", "-C", str(repo), "update-ref", "-d", ref],
            capture_output=True,
        )


# ---------------------------------------------------------------------------
# Geração do bundle (com snapshot embutido)
# ---------------------------------------------------------------------------
def create_bundle(repo: Path, dest: Path, bundle_all: bool = True) -> bool:
    """Cria/atualiza um bundle do repositório, capturando o worktree atual.

    O bundle inclui --all (todo o histórico) + refs/drive-sync/*
    (snapshot do worktree). Repo sem commits gera bundle snapshot-only
    (tree vazia se o worktree estiver vazio) — retornar False aqui
    envenenaria o success agregado do folder no modo auto (#27).
    """
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Cria o snapshot. Se falhar, ainda tentamos um bundle convencional
    # (pelo menos o histórico vai pra nuvem).
    has_snapshot = _create_worktree_snapshot(repo)
    if not has_snapshot:
        log.warning("[%s] Snapshot do worktree falhou; gerando bundle só com histórico.", repo)

    # Confere se há ALGO para empacotar (commit ou snapshot).
    head_check = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "HEAD"],
        capture_output=True, text=True,
    )
    snap_check = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", SNAPSHOT_REF],
        capture_output=True, text=True,
    )
    if head_check.returncode != 0 and snap_check.returncode != 0:
        log.warning("[%s] Sem commits nem snapshot; pulando bundle.", repo)
        _delete_snapshot_refs(repo)
        return False

    # Monta a lista de refs para o bundle.
    refs_args: list[str] = []
    if bundle_all and head_check.returncode == 0:
        refs_args.append("--all")
    if has_snapshot:
        # O parent do commit-snapshot (head-at-snapshot) viaja no bundle como
        # ancestral de SNAPSHOT_REF — sem ref dedicada a empacotar (#17).
        refs_args.append(SNAPSHOT_REF)
    if not refs_args:
        # Repo sem HEAD mas com snapshot — só o snapshot.
        refs_args = [SNAPSHOT_REF]

    tmp = dest.with_suffix(dest.suffix + ".tmp")
    result = subprocess.run(
        ["git", "-C", str(repo), "bundle", "create", str(tmp), *refs_args],
        capture_output=True, text=True,
    )

    # Sempre limpa as refs do snapshot, dê o que der.
    _delete_snapshot_refs(repo)

    if result.returncode != 0:
        log.error("Falha ao criar bundle de %s: %s", repo, result.stderr.strip())
        if tmp.exists():
            tmp.unlink()
        return False

    tmp.replace(dest)
    log.info("Bundle gerado: %s (%d bytes, snapshot=%s)",
             dest, dest.stat().st_size, has_snapshot)
    return True


# ---------------------------------------------------------------------------
# Restauração: aplica o snapshot por cima do worktree
# ---------------------------------------------------------------------------
def restore_from_bundle(bundle: Path, dest_repo: Path) -> bool:
    """Reconstrói/atualiza um repositório a partir de um bundle.

    Caso 1: dest_repo não existe.
        - clone <bundle> dest_repo
        - se houver SNAPSHOT_REF, faz checkout do snapshot por cima do
          worktree para restaurar arquivos modificados/untracked.

    Caso 2: dest_repo existe.
        - fetch das refs do bundle (incluindo SNAPSHOT_REF, se presente).
        - se SNAPSHOT_REF chegou, restaura o worktree ATÉ o estado do snapshot
          (preservando o HEAD do usuário onde estava).
    """
    if not dest_repo.exists():
        dest_repo.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["git", "clone", str(bundle), str(dest_repo)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            log.error("Falha ao clonar %s em %s: %s", bundle, dest_repo, result.stderr.strip())
            return False
        # `git clone` só traz refs/heads/* — fazemos um fetch suplementar para
        # também trazer refs/drive-sync/* (snapshot do worktree). Limitamos
        # ao prefixo do snapshot para não tentar sobrescrever o branch que
        # acabou de ser checked out (o git recusa esse caso).
        subprocess.run(
            ["git", "-C", str(dest_repo), "fetch", str(bundle),
             "+refs/drive-sync/*:refs/drive-sync/*"],
            capture_output=True,
        )
        # Aplica o snapshot, se vier no bundle.
        _apply_snapshot_if_present(dest_repo, fresh_clone=True)
        log.info("Repositório restaurado a partir de bundle: %s → %s", bundle, dest_repo)
        return True

    if not is_git_repo(dest_repo):
        log.error("Destino %s existe mas não é um repo Git; recusando fetch.", dest_repo)
        return False

    # Em repo já existente: dois fetches separados.
    # 1. refs/heads/* — pode falhar se a branch correspondente estiver
    #    checked out aqui; nesse caso, o usuário tem mudanças locais não
    #    commitadas e não queremos sobrescrever silenciosamente. Logamos
    #    aviso e seguimos para aplicar o snapshot, que reflete o estado
    #    completo do worktree do outro lado.
    branch_fetch = subprocess.run(
        ["git", "-C", str(dest_repo), "fetch", str(bundle), "+refs/heads/*:refs/heads/*"],
        capture_output=True, text=True,
    )
    if branch_fetch.returncode != 0:
        log.warning("[%s] Não foi possível atualizar branches (provavelmente checked out): %s",
                    dest_repo, branch_fetch.stderr.strip()[-200:])
    # 2. refs internas (snapshot) — sempre OK porque ninguém faz checkout delas.
    snap_fetch = subprocess.run(
        ["git", "-C", str(dest_repo), "fetch", str(bundle),
         "+refs/drive-sync/*:refs/drive-sync/*"],
        capture_output=True, text=True,
    )
    if snap_fetch.returncode != 0 and branch_fetch.returncode != 0:
        log.error("Ambos os fetches falharam para %s", bundle)
        return False

    _apply_snapshot_if_present(dest_repo, fresh_clone=False)
    # Limpa as refs internas localmente — elas só servem ao mecanismo,
    # não devem ficar no repo do usuário.
    _delete_snapshot_refs(dest_repo)
    log.info("Repositório atualizado a partir de bundle: %s → %s", bundle, dest_repo)
    return True


def _apply_snapshot_if_present(repo: Path, fresh_clone: bool) -> None:
    """Se o bundle trouxe SNAPSHOT_REF, aplica seu conteúdo ao worktree.

    A mecânica:
    - Em clone fresco: o HEAD aponta para o branch padrão. Posicionamos no
      head-at-snapshot (SNAPSHOT_REF^ — o parent do commit-snapshot) e
      aplicamos a tree do SNAPSHOT_REF por cima do worktree.
    - Em repo já existente: NÃO mexemos no HEAD do usuário (que pode estar
      em outro branch). Apenas materializamos os arquivos do snapshot no
      worktree usando `git checkout-index`.

    Importante: untracked e modificações locais que coincidam com arquivos
    do snapshot serão sobrescritos. Esse é o comportamento desejado quando
    a nuvem está mais nova — equivalente a "puxar a versão remota".
    """
    snap_check = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", SNAPSHOT_REF],
        capture_output=True, text=True,
    )
    if snap_check.returncode != 0:
        return  # bundle não trouxe snapshot — só histórico

    snapshot_sha = snap_check.stdout.strip()

    # Em clone fresco, o `git clone` já criou um worktree a partir do
    # branch padrão do bundle. Posicionamos o HEAD onde o usuário estava
    # — o primeiro parent do commit-snapshot (SNAPSHOT_REF^), sem ref
    # dedicada (#17) — e depois materializamos o snapshot por cima.
    # Snapshot sem parent (repo de origem sem commits) → nada a reposicionar.
    if fresh_clone:
        head_at_snapshot = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", f"{SNAPSHOT_REF}^"],
            capture_output=True, text=True,
        )
        if head_at_snapshot.returncode == 0:
            subprocess.run(
                ["git", "-C", str(repo), "reset", "--hard", head_at_snapshot.stdout.strip()],
                capture_output=True,
            )
        # A ref interna não pertence ao usuário.
        _delete_snapshot_refs(repo)

    # Materializa os arquivos do snapshot no worktree.
    # Estratégia:
    #   1. read-tree do snapshot em um index temporário (não toca no real).
    #   2. checkout-index --all --force a partir desse index.
    # Resultado: arquivos do snapshot aparecem no worktree; o index real
    # do usuário fica como estava antes.
    env = os.environ.copy()
    tmp_index = tempfile.NamedTemporaryFile(prefix="proton-restore-idx-", delete=False)
    tmp_index.close()
    # Mesmo racional do snapshot (#27): index existente vazio é rejeitado pelo
    # git; path inexistente → index fresco no read-tree.
    os.unlink(tmp_index.name)
    env["GIT_INDEX_FILE"] = tmp_index.name

    try:
        r = subprocess.run(
            ["git", "-C", str(repo), "read-tree", snapshot_sha],
            capture_output=True, text=True, env=env,
        )
        if r.returncode != 0:
            log.warning("read-tree do snapshot falhou em %s: %s", repo, r.stderr.strip())
            return
        r = subprocess.run(
            ["git", "-C", str(repo), "checkout-index", "--all", "--force"],
            capture_output=True, text=True, env=env,
        )
        if r.returncode != 0:
            log.warning("checkout-index do snapshot falhou em %s: %s", repo, r.stderr.strip())
            return
        log.info("[%s] Worktree restaurado a partir do snapshot.", repo)
    finally:
        try:
            os.unlink(tmp_index.name)
        except OSError:
            pass


def should_replace_bundle(local_repo: Path, bundle: Path) -> bool:
    """Decide se o bundle local precisa ser regerado.

    Critério: qualquer mudança no worktree (commitada OU não) torna o bundle
    obsoleto. Usa worktree_last_modified, que considera arquivos rastreados
    e untracked não-ignorados.
    """
    if not bundle.exists():
        return True
    return worktree_last_modified(local_repo) > bundle.stat().st_mtime


# Mantido como alias por compatibilidade — usado pelo daemon para comparar
# com o mtime do bundle remoto.
def repo_last_modified(repo: Path) -> float:
    """Alias de worktree_last_modified (preserva a API anterior)."""
    return worktree_last_modified(repo)


# ---------------------------------------------------------------------------
# Wrapper de alto nível usado pelo sync engine
# ---------------------------------------------------------------------------
class GitHandler:
    def __init__(self, cfg: GitConfig):
        self.cfg = cfg
        self.cfg.bundles_dir.mkdir(parents=True, exist_ok=True)

    def package_repos_under(self, root: Path) -> list[Path]:
        """Encontra todos os repos sob `root` e gera/atualiza seus bundles."""
        repos = find_git_repos(root, self.cfg.max_recursion_depth) if self.cfg.recursive_detection \
            else ([root] if is_git_repo(root) else [])

        if not repos:
            log.debug("Nenhum repositório Git encontrado em %s", root)
            return []

        log.info("%d repositório(s) Git detectado(s) em %s", len(repos), root)
        bundles: list[Path] = []
        for repo in repos:
            dest = bundle_path_for(
                repo, root, self.cfg.bundles_dir / root.name, self.cfg.bundle_suffix
            )
            if should_replace_bundle(repo, dest):
                if create_bundle(repo, dest, self.cfg.bundle_all):
                    bundles.append(dest)
            else:
                log.debug("Bundle %s já está atualizado.", dest)
                bundles.append(dest)
        return bundles
