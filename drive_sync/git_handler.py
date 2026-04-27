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
from pathlib import Path

from .config import GitConfig

log = logging.getLogger(__name__)

# Refs isoladas usadas para o snapshot do worktree.
SNAPSHOT_REF = "refs/drive-sync/snapshot"
HEAD_MARKER_REF = "refs/drive-sync/head-at-snapshot"


def is_git_repo(path: Path) -> bool:
    """Verifica se `path` é a raiz de um repositório Git (tem .git)."""
    return (path / ".git").exists()


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

        # 6. Marca também onde o HEAD estava no momento do snapshot, para
        #    que a restauração saiba para onde voltar.
        if has_head:
            subprocess.run(
                ["git", "-C", str(repo), "update-ref", HEAD_MARKER_REF, head_sha],
                capture_output=True,
            )

        return True
    finally:
        try:
            os.unlink(tmp_index.name)
        except OSError:
            pass


def _delete_snapshot_refs(repo: Path) -> None:
    """Remove as refs locais usadas pelo snapshot.

    Elas existem só durante a janela de geração do bundle; depois disso
    são lixo. Vivem dentro do bundle, que é o que importa.
    """
    for ref in (SNAPSHOT_REF, HEAD_MARKER_REF):
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
    (snapshot do worktree). Repositórios sem nenhum commit E sem arquivos
    são pulados.
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
        refs_args.append(SNAPSHOT_REF)
        refs_args.append(HEAD_MARKER_REF)
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
      HEAD_MARKER_REF (estado do usuário no momento do snapshot) e aplicamos
      a tree do SNAPSHOT_REF por cima do worktree.
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
    # branch padrão do bundle. Vamos posicionar o HEAD onde o usuário
    # estava (HEAD_MARKER_REF) e depois materializar o snapshot.
    if fresh_clone:
        head_marker = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", HEAD_MARKER_REF],
            capture_output=True, text=True,
        )
        if head_marker.returncode == 0:
            subprocess.run(
                ["git", "-C", str(repo), "reset", "--hard", head_marker.stdout.strip()],
                capture_output=True,
            )
        # As refs internas não pertencem ao usuário.
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
