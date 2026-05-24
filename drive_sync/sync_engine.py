"""Wrapper para `rclone bisync` (sincronização bidirecional verdadeira).

Por que `bisync` e não `sync`?
- `rclone sync A B` é unidirecional: torna B idêntico a A (apaga em B o que
  não está em A). Não atende ao requisito de "ponta faltante recebe a versão,
  e em conflito vence o mais recente em ambas as pontas".
- `rclone bisync` mantém estado em ~/.cache/rclone/bisync e resolve conflitos
  por timestamp + hash quando habilitado (--conflict-resolve newer).

A primeira execução exige `--resync` para construir o estado inicial.
Detectamos isso checando a presença do arquivo de estado.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
from pathlib import Path

from .config import AppConfig, FolderConfig
from .exclude_presets import default_excludes_for_code

log = logging.getLogger(__name__)

# Serializa subprocess rclone — race no token refresh do backend protondrive
# zera os tokens cached quando ≥2 instâncias rclone init-ializam em paralelo
# (rclone#7381, ADR-001). Pode ser removido se o backend migrar para lib/oauthutil.
_rclone_lock = asyncio.Lock()

# Tabela canônica dos pares (code, status) reconhecidos pelo classificador.
# Origem empírica de cada par:
# - (8002, 422): incidente 2026-05-11 — Code=8002 em /api/auth/v4/2fa ("Incorrect login credentials").
# - (9001, 422): cobertura preventiva — CAPTCHA na auth original; ainda não observado em produção.
# - (10013, 400): incidente 2026-05-24 16:58 — /auth/v4/refresh ("Invalid refresh token").
# - (2028, 422): incidente 2026-05-24 17:01 — /auth/v4 (CAPTCHA gate / suspect activity).
_AUTH_CODES: dict[tuple[int, int], str] = {
    (8002, 422): "invalid_credentials",
    (9001, 422): "captcha_required",
    (10013, 400): "refresh_token_invalid",
    (2028, 422): "rate_limited",
}

# Regex composto: casa apenas pares (code, status) presentes em _AUTH_CODES.
# Construção em alternation (em vez de regex genérico + filtro pós-match) preserva
# a invariante de robustez a pares ruidosos no stderr — ex.: `(Code=9999, Status=500)`
# de erro de socket precedendo o erro auth deixaria de ser silenciado.
_AUTH_CODE_RE = re.compile(
    r"\(Code=(?:(?:8002|9001|2028),\s*Status=422|10013,\s*Status=400)\)"
)

# Endpoint de auth aceita ambas as formas observadas em produção:
# `/api/auth/v4` (histórico, 2026-05-11) e `/auth/v4` (atual, 2026-05-24 — Proton
# mudou o path sem aviso). Aceita falso-positivo teórico `/v4/auth/v4` (não emitido).
_AUTH_ENDPOINT_RE = re.compile(r"(?:/api)?/auth/v4")

# Re-extrai (code, status) do match já validado, para lookup do kind em _AUTH_CODES.
_AUTH_PAIR_RE = re.compile(r"Code=(\d+),\s*Status=(\d+)")


class AuthDegradedError(RuntimeError):
    """Levantada por `_run` quando o stderr do rclone indica falha de auth conhecida.

    Carrega `kind` e `code` parseados para callers superiores sinalizarem
    estado degraded sem re-parsear o stderr.
    """

    def __init__(self, kind: str, code: int, stderr_tail: str):
        self.kind = kind
        self.code = code
        self.stderr_tail = stderr_tail
        super().__init__(f"{kind} (Code={code})")


def _classify_rclone_stderr(stderr: str) -> AuthDegradedError | None:
    """Detecta falha de auth conhecida do backend protondrive no stderr do rclone.

    Cobre 4 pares (Code, Status) — ver `_AUTH_CODES`. Endpoint aceito: `/auth/v4`
    com ou sem prefixo `/api/`. Retorna None quando não casa.
    """
    if _AUTH_ENDPOINT_RE.search(stderr) is None:
        return None
    match = _AUTH_CODE_RE.search(stderr)
    if match is None:
        return None
    pair = _AUTH_PAIR_RE.search(match.group(0))
    code, status = int(pair.group(1)), int(pair.group(2))
    return AuthDegradedError(
        kind=_AUTH_CODES[(code, status)],
        code=code,
        stderr_tail=stderr.strip()[-500:],
    )


def _bisync_state_dir() -> Path:
    """Diretório onde o rclone guarda o estado das bisync (XDG)."""
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    p = Path(base) / "rclone" / "bisync"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _state_marker_for(local: Path, remote: str) -> Path:
    """Arquivo único que marca 'bisync já inicializado para este par'.

    O rclone já cria seus próprios listings — usamos um marker próprio
    porque o nome dos arquivos do rclone depende da versão.
    """
    key = hashlib.sha1(f"{local}|{remote}".encode()).hexdigest()[:16]
    return _bisync_state_dir() / f"drive-sync.{key}.initialized"


def remote_uri_for(folder: FolderConfig, app: AppConfig, sub: str | None = None) -> str:
    """Monta a URI rclone: `<remote>:<root>/<subpath>[/<extra>]`."""
    parts = [app.rclone.remote_root, folder.remote_subpath]
    if sub:
        parts.append(sub)
    path = "/".join(p.strip("/") for p in parts if p)
    return f"{app.rclone.remote_name}:{path}"


async def _run(cmd: list[str]) -> tuple[int, str, str]:
    """Roda processo de forma assíncrona e devolve (rc, stdout, stderr).

    Levanta `AuthDegradedError` quando o rclone retorna não-zero com stderr
    casando padrão de falha de auth conhecida (ver `_AUTH_CODES`). Demais erros
    retornam normalmente — caller é quem decide o que fazer.
    """
    log.debug("Executando: %s", " ".join(cmd))
    async with _rclone_lock:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, stderr_b = await proc.communicate()
        rc = proc.returncode or 0
        stderr = stderr_b.decode("utf-8", errors="replace")
        if rc != 0:
            auth_err = _classify_rclone_stderr(stderr)
            if auth_err is not None:
                raise auth_err
        return rc, stdout_b.decode("utf-8", errors="replace"), stderr


class RcloneEngine:
    def __init__(self, app: AppConfig):
        self.app = app

    def _base_cmd(self) -> list[str]:
        return [self.app.rclone.binary, *self.app.rclone.global_flags]

    async def auth_probe(self) -> None:
        """Probe leve do backend para detectar falha de auth antes de um job real.

        Levanta AuthDegradedError quando o backend reporta auth quebrada.
        Outros erros (rede, rate-limit transitório) são silenciados — probe não
        deve degradar o daemon por falha não-auth.
        """
        # `about` exercita o endpoint de auth sem listar conteúdo (payload
        # mínimo) — força exatamente o caminho que falha com os códigos
        # classificados em `_AUTH_CODES`.
        remote = f"{self.app.rclone.remote_name}:"
        cmd = self._base_cmd() + ["about", remote]
        try:
            await _run(cmd)
        except AuthDegradedError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.debug("auth_probe falhou (não-auth): %s", exc)

    # -----------------------------------------------------------------
    # Sincronização bidirecional de uma pasta "comum" (não-Git)
    # -----------------------------------------------------------------
    async def _ensure_remote_dir(self, remote: str, name: str) -> bool:
        """Cria o diretório remoto se não existir. É idempotente."""
        cmd = [self.app.rclone.binary, "mkdir", remote]
        rc, _out, err = await _run(cmd)
        if rc != 0:
            log.error("[%s] Falha ao criar diretório remoto %s: %s", name, remote, err.strip()[-300:])
            return False
        log.debug("[%s] Diretório remoto garantido: %s", name, remote)
        return True

    async def bisync_folder(self, folder: FolderConfig, local_override: Path | None = None) -> bool:
        """Executa bisync para uma tarefa. Retorna True em sucesso."""
        local = local_override or folder.local_path
        remote = remote_uri_for(folder, self.app)
        marker = _state_marker_for(local, remote)

        local.mkdir(parents=True, exist_ok=True)

        if not await self._ensure_remote_dir(remote, folder.name):
            return False

        cmd = self._base_cmd() + ["bisync", str(local), remote]
        # Resolução de conflitos: vence quem foi modificado por último.
        cmd += ["--conflict-resolve", "newer", "--conflict-loser", "delete"]
        # Cria diretórios vazios também.
        cmd += ["--create-empty-src-dirs"]

        # Mescla excludes: usuário + presets automáticos (quando aplicável).
        # `bundle` não passa por aqui, então só checamos auto_exclude.
        excludes: list[str] = list(folder.exclude)
        if folder.auto_exclude:
            # Mantém ordem (usuário primeiro, depois presets) e remove duplicatas.
            seen: set[str] = set(excludes)
            for pat in default_excludes_for_code():
                if pat not in seen:
                    excludes.append(pat)
                    seen.add(pat)
        for pat in excludes:
            cmd += ["--exclude", pat]

        # Primeira execução: precisa de --resync.
        if not marker.exists():
            log.info("[%s] Primeira sincronização — executando --resync.", folder.name)
            cmd += ["--resync"]

        rc, _out, err = await _run(cmd)
        if rc != 0:
            log.error("[%s] bisync falhou (rc=%d): %s", folder.name, rc, err.strip()[-500:])
            # Em alguns erros, o rclone sugere `--resync` para recuperar.
            # Não invocamos automaticamente para não causar perda de dados;
            # apenas registramos com nível ERROR.
            return False

        marker.touch()
        log.info("[%s] bisync concluído com sucesso.", folder.name)
        return True

    # -----------------------------------------------------------------
    # Operações específicas para bundles Git
    # -----------------------------------------------------------------
    async def upload_bundle(self, bundle: Path, folder: FolderConfig, rel_subpath: str) -> bool:
        """Sobe um bundle para `<remote>/<subpath>/<rel_subpath>`.

        Usa `rclone copyto` (1:1) com checagem de tamanho/mtime — só transfere
        se for diferente.
        """
        remote = remote_uri_for(folder, self.app, rel_subpath)
        cmd = self._base_cmd() + ["copyto", str(bundle), remote, "--update"]
        rc, _o, err = await _run(cmd)
        if rc != 0:
            log.error("Falha ao subir bundle %s → %s: %s", bundle, remote, err.strip()[-300:])
            return False
        log.info("Bundle sincronizado para nuvem: %s", remote)
        return True

    async def download_bundle_if_newer(
        self, folder: FolderConfig, rel_subpath: str, dest: Path
    ) -> bool:
        """Baixa o bundle do remote se ele for mais novo que o local.

        Implementação: usa `rclone copyto --update` com origem remota — se o
        local for mais novo, o rclone não sobrescreve.
        """
        remote = remote_uri_for(folder, self.app, rel_subpath)
        dest.parent.mkdir(parents=True, exist_ok=True)
        cmd = self._base_cmd() + ["copyto", remote, str(dest), "--update"]
        rc, _o, err = await _run(cmd)
        if rc != 0:
            # "directory not found" / "object not found" significa que o
            # bundle ainda não existe na nuvem — não é erro.
            if "not found" in err.lower():
                log.debug("Bundle remoto inexistente em %s — nada a baixar.", remote)
                return True
            log.error("Falha ao baixar bundle %s: %s", remote, err.strip()[-300:])
            return False
        return True
