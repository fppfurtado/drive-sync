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
import time
from collections import deque
from pathlib import Path

from .config import AppConfig, FolderConfig
from .exclude_presets import default_excludes_for_code

log = logging.getLogger(__name__)

# Serializa subprocess rclone — race no token refresh do backend protondrive
# zera os tokens cached quando ≥2 instâncias rclone init-ializam em paralelo
# (rclone#7381, ADR-001). Pode ser removido se o backend migrar para lib/oauthutil.
_rclone_lock = asyncio.Lock()

# #61 / SP-T7 (J4): força o campo `2fa` do backend protondrive a vazio em TODA
# invocação rclone. Um `2fa` estático persistido no rclone.conf é inútil no cold
# reauth (o TOTP de ~30s expira muito antes de qualquer cold reauth real) e só
# produz o `8002` enganoso ao ser re-submetido expirado (finding SP-T1). Passar o
# flag explícito-vazio sobrescreve o valor do config SEM que o drive-sync jamais
# escreva o rclone.conf — dissolve a corrida de escrita com o rclone (que reescreve
# tokens no mesmo arquivo), o motivo do defer original de SP-T7. Recovery de 2FA
# genuíno passa a ser reauth interativo (código usado ao vivo, nunca persistido),
# ver ADR-017 (emenda o playbook de recovery do ADR-003).
_FORCE_EMPTY_2FA = ["--protondrive-2fa", ""]

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

# ---------------------------------------------------------------------------
# Janela de flakiness transitória do provedor (SP-T2 · #35 + #46)
# ---------------------------------------------------------------------------
# Casa qualquer `Status=5xx` no stderr do rclone — endpoint-agnóstico (auth
# `/auth/v4` E block-upload `/storage/blocks`), unificando o classificador de
# #35 (auth 8002/500-storm) e #46 (block 502/504) numa só janela.
_5XX_RE = re.compile(r"Status=(5\d\d)\b")

# Estado module-level: `_run` e `_classify_rclone_stderr` são funções
# module-level (não métodos de RcloneEngine), então a janela vive aqui.
# Timestamps monotônicos (alinhado ao dual-clock de ADR-004/007 — imune a
# ajuste de wall-clock/suspend). `_run` é serializado sob `_rclone_lock`
# (ADR-001), então append/leitura não precisam de lock adicional.
_infra_window: deque[float] = deque()


def _record_infra_signals(stderr: str) -> int:
    """Registra na janela um timestamp monotônico por ocorrência `Status=5xx`.

    Chamado por `_run` no caminho rc≠0. Retorna quantos 5xx foram registrados.
    """
    hits = _5XX_RE.findall(stderr)
    now = time.monotonic()
    for _ in hits:
        _infra_window.append(now)
    return len(hits)


def _reset_infra_window() -> None:
    """Zera a janela de 5xx — helper de teste (estado module-level)."""
    _infra_window.clear()


# Config da detecção de storm (setada por RcloneEngine.__init__ a partir de
# RcloneConfig). Defaults calibrados dos 2 incidentes (2026-06-23 ~16× 500 numa
# janela; 2026-08-26 storm ~1.5h) — conservador: exige storm real, não 5xx isolado.
_DEFAULT_INFRA_STORM_THRESHOLD = 5
_DEFAULT_INFRA_WINDOW_SECONDS = 600.0
_INFRA_STORM_THRESHOLD = _DEFAULT_INFRA_STORM_THRESHOLD
_INFRA_WINDOW_SECONDS = _DEFAULT_INFRA_WINDOW_SECONDS


def _configure_infra_detection(threshold: int, window_seconds: float) -> None:
    """Configura o detector de storm a partir de RcloneConfig (SP-T3)."""
    global _INFRA_STORM_THRESHOLD, _INFRA_WINDOW_SECONDS
    _INFRA_STORM_THRESHOLD = threshold
    _INFRA_WINDOW_SECONDS = window_seconds


def _infra_storm_active() -> bool:
    """Poda a janela pelo window atual e diz se há um storm de 5xx ativo."""
    cutoff = time.monotonic() - _INFRA_WINDOW_SECONDS
    while _infra_window and _infra_window[0] < cutoff:
        _infra_window.popleft()
    return len(_infra_window) >= _INFRA_STORM_THRESHOLD

# ADR-012: captura completa de stderr per call-site em ~/.local/state/drive-sync/
# substituindo o tail-truncate `err.strip()[-N:]` que ocultava causa-raiz.
_FIRST_ERROR_RE = re.compile(r"^.*ERROR\s*:\s*(.+)$", re.MULTILINE)
_SAFE_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]")


def _stderr_dir() -> Path:
    """Diretório XDG state onde escrevemos last-stderr-*.log."""
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    p = Path(base) / "drive-sync"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _slug(value: str) -> str:
    """Sanitiza folder.name / rel_subpath para uso seguro em filename."""
    return _SAFE_SLUG_RE.sub("_", value)


def _capture_stderr(
    op: str, folder_name: str, stderr: str, *, sub: str | None = None
) -> tuple[str, Path]:
    """Escreve stderr completo em arquivo per call-site e retorna (summary, path).

    `summary` = primeiro match `_FIRST_ERROR_RE.group(0)` se houver, senão
    `stderr.strip()[-500:]` (fallback ao comportamento legado pré-ADR-012).
    """
    folder_slug = _slug(folder_name)
    if sub is not None:
        filename = f"last-stderr-{op}-{folder_slug}-{_slug(sub)}.log"
    else:
        filename = f"last-stderr-{op}-{folder_slug}.log"
    path = _stderr_dir() / filename
    path.write_text(stderr)
    match = _FIRST_ERROR_RE.search(stderr)
    summary = match.group(0) if match else stderr.strip()[-500:]
    return summary, path


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


# Janela de graça entre SIGTERM e SIGKILL ao matar um job estourado (#45): dá ao
# rclone a chance de sair limpo (fechar sockets/state) antes do kill forçado.
_STUCK_JOB_GRACE_SECONDS = 10.0


class StuckJobError(RuntimeError):
    """Levantada por `_run` quando um job rclone ultrapassa seu `max_job_runtime_seconds`.

    #45: um único bisync sem excludes segurou o lock global (ADR-001) por 14h sem
    ação automática. O `_run` mata o subprocess (SIGTERM → grace → SIGKILL) e
    levanta este erro; o daemon o traduz em `[STUCK_JOB]` + folder degradado
    (reusa infra ADR-005), liberando o lock para outros folders. Restart manual
    continua sendo o recovery (consistente com `bisync errors do NOT auto-recover`).
    Carrega o limite estourado para o daemon compor a mensagem.
    """

    def __init__(self, timeout_seconds: float):
        self.timeout_seconds = timeout_seconds
        super().__init__(f"job excedeu max_job_runtime_seconds={timeout_seconds:g}s")


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
    kind = _AUTH_CODES[(code, status)]
    # SP-T3: um par auth co-ocorrendo com um storm de 5xx do provedor é
    # colateral da flakiness transitória — reclassifica para `proton_infra`
    # (recovery=aguardar, sem reauth) em vez de credencial-genuína.
    if _infra_storm_active():
        kind = "proton_infra"
    return AuthDegradedError(
        kind=kind,
        code=code,
        stderr_tail=stderr.strip()[-500:],
    )


# Assinatura stale-listings do bisync (SP-T2 · #47 · ADR-019): rc=7 cujo estado
# `.lst` morreu (ex.: queda de rede / storm 5xx da Proton no meio de um bisync
# longo). O rclone reporta "cannot find prior Path1 or Path2 listings" +
# "Must run --resync to recover". Distinto de OUTRAS causas de rc=7 — ex.: colisão
# case-insensitive (ADR-011), cujo abort NÃO casa estas frases e segue o
# tratamento default de BISYNC_FAIL. Recuperável via --resync data-safe
# (auto-recuperação gated em bisync_folder).
_STALE_LISTINGS_RE = re.compile(
    r"cannot find prior Path1 or Path2 listings|Must run --resync to recover"
)


def _is_stale_listings(stderr: str) -> bool:
    """True se o stderr do rclone indica o abort stale-listings (baseline `.lst` morto).

    Reconhece a assinatura específica do caso recuperável por `--resync`; outras
    causas de rc=7 (ex.: case-duplicates, ADR-011) retornam False e seguem o
    tratamento default de `[BISYNC_FAIL]`.
    """
    return _STALE_LISTINGS_RE.search(stderr) is not None


# Assinatura too-many-deletes do bisync (#52): safety abort do rclone quando o scan
# atual de Path1 tem >50% menos itens que o baseline `.lst` (uma mudança em massa
# legítima removeu conteúdo). A ÚNICA dica que o rclone exibe é `Run with --force if
# desired` — PERIGOSA: `--force` propaga as deleções e causa perda de dados se o
# conteúdo não estiver salvo em outro lugar. Distinto do rc=7 stale-listings (benigno,
# auto-recuperável): aqui há divergência REAL, então NÃO auto-recupera — só enriquece
# o log com advice safe apontando pro playbook rc=1 (invariante `bisync errors do NOT
# auto-recover` preservado — este braço é sinalização, não recuperação).
_TOO_MANY_DELETES_RE = re.compile(r"too many deletes")


def _is_too_many_deletes(stderr: str) -> bool:
    """True se o stderr do bisync casa o safety abort `too many deletes` (rc=1).

    Reconhece a assinatura específica do abort perigoso (deleção em massa); o advice
    enriquecido substitui a dica `--force` cega do rclone por um ponteiro pro branch
    rc=1 do playbook de recuperação.
    """
    return _TOO_MANY_DELETES_RE.search(stderr) is not None


# Parse do `bisync --resync --dry-run` (SP-T1 · SP-T4 · ADR-019): prova união
# no-op. Como `--resync` é união (nunca deleta — F2), cada operação que o resync
# real faria aparece no dry-run como uma linha `... as --dry-run is set`; o bloco
# de stats fecha em `Transferred: 0 B / 0 B` no caso no-op. rc do dry-run é sempre
# 0 (inútil como sinal). Amostras reais em docs/spikes/SP-T1-autoresync-dryrun-parse.md.
_DRYRUN_WOULD_MUTATE_RE = re.compile(r"as --dry-run is set")
_DRYRUN_ZERO_BYTES_RE = re.compile(r"Transferred:\s+0 B / 0 B")


def _dryrun_resync_is_noop(output: str) -> bool:
    """True se a saída de `bisync --resync --dry-run` prova união no-op (data-safe).

    Requer AMBOS os sinais (SP-T1): NENHUMA linha `as --dry-run is set` (cada uma
    seria uma cópia que o resync real faria) E o bloco `Transferred: 0 B / 0 B`.
    Fail-safe por construção (C2/S2): ausência de qualquer sinal — output vazio,
    formato inesperado, ou ≥1 would-be-mutation — retorna False → NÃO auto-recupera.
    """
    if _DRYRUN_WOULD_MUTATE_RE.search(output) is not None:
        return False
    return _DRYRUN_ZERO_BYTES_RE.search(output) is not None


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


async def _run(cmd: list[str], timeout: float | None = None) -> tuple[int, str, str]:
    """Roda processo de forma assíncrona e devolve (rc, stdout, stderr).

    Levanta `AuthDegradedError` quando o rclone retorna não-zero com stderr
    casando padrão de falha de auth conhecida (ver `_AUTH_CODES`). Demais erros
    retornam normalmente — caller é quem decide o que fazer.

    `timeout` (#45): se > 0 e o processo não terminar dentro dele, mata o
    subprocess (SIGTERM → grace de `_STUCK_JOB_GRACE_SECONDS` → SIGKILL) e levanta
    `StuckJobError`. Matar o processo aqui (não cancelar a corrotina) é essencial:
    o processo mora neste escopo e é ele quem segura o lock serializado (ADR-001) —
    uma corrotina cancelada deixaria o subprocess órfão segurando o lock. `None`/0
    = sem limite (comportamento histórico).
    """
    log.debug("Executando: %s", " ".join(cmd))
    async with _rclone_lock:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            await _kill_stuck_proc(proc)
            raise StuckJobError(timeout) from None
        rc = proc.returncode or 0
        stderr = stderr_b.decode("utf-8", errors="replace")
        if rc != 0:
            _record_infra_signals(stderr)
            auth_err = _classify_rclone_stderr(stderr)
            if auth_err is not None:
                raise auth_err
        return rc, stdout_b.decode("utf-8", errors="replace"), stderr


async def _kill_stuck_proc(proc: asyncio.subprocess.Process) -> None:
    """Encerra um subprocess estourado: SIGTERM, aguarda a graça, então SIGKILL.

    Sempre faz o reap (`proc.wait()`) para não deixar zumbi. Best-effort: se o
    processo já morreu entre o timeout e o terminate, `ProcessLookupError` é benigno.
    """
    try:
        proc.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), _STUCK_JOB_GRACE_SECONDS)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()


class RcloneEngine:
    def __init__(self, app: AppConfig):
        self.app = app
        _configure_infra_detection(
            app.rclone.infra_storm_threshold,
            app.rclone.infra_window_seconds,
        )
        # Guard 1-tentativa-por-episódio da auto-recuperação de rc=7 (ADR-019, #47):
        # markers de pares com auto-resync já tentado no episódio corrente. Evita
        # re-disparar o dry-run a cada ciclo num folder divergente preso. Limpo em
        # QUALQUER sucesso de bisync; reseta no restart (novo processo → set vazio).
        self._autoresync_attempted: set[Path] = set()

    def _base_cmd(self) -> list[str]:
        return [
            self.app.rclone.binary,
            *self.app.rclone.global_flags,
            *_FORCE_EMPTY_2FA,
        ]

    def _job_timeout(self, folder: FolderConfig) -> float | None:
        """Resolve o max-runtime efetivo do folder (#45): override per-folder > global.

        `None` no folder = herda `rclone.max_job_runtime_seconds`; 0 em qualquer
        nível = desligado (sem limite). Retorna `None` quando desligado.
        """
        raw = folder.max_job_runtime_seconds
        if raw is None:
            raw = self.app.rclone.max_job_runtime_seconds
        return float(raw) if raw and raw > 0 else None

    async def auth_probe(self) -> bool:
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
            rc, _out, _err = await _run(cmd)
            # rc==0 é sucesso real; rc≠0 sem AuthDegradedError (ex.: 5xx do
            # provedor) NÃO é sucesso — SP-T5 depende disso para não retomar
            # prematuramente enquanto o provedor ainda está em storm.
            return rc == 0
        except AuthDegradedError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.debug("auth_probe falhou (não-auth): %s", exc)
            return False

    # -----------------------------------------------------------------
    # Sincronização bidirecional de uma pasta "comum" (não-Git)
    # -----------------------------------------------------------------
    async def _ensure_remote_dir(
        self, remote: str, name: str, timeout: float | None = None
    ) -> bool:
        """Cria o diretório remoto se não existir. É idempotente."""
        # via _base_cmd() para herdar global_flags + o `--protondrive-2fa ""`
        # forçado (#61): mkdir também toca o backend e faz cold reauth.
        cmd = self._base_cmd() + ["mkdir", remote]
        rc, _out, err = await _run(cmd, timeout=timeout)
        if rc != 0:
            summary, path = _capture_stderr("mkdir", name, err)
            log.error(
                "[%s] [MKDIR_FAIL] %s: %s (full stderr: %s)",
                name, remote, summary, path,
            )
            return False
        log.debug("[%s] Diretório remoto garantido: %s", name, remote)
        return True

    async def _attempt_gated_autoresync(
        self, folder: FolderConfig, base_cmd: list[str], timeout: float | None
    ) -> bool:
        """Auto-recuperação gated de rc=7 stale-listings (ADR-019, #47).

        Prova, via `--resync --dry-run`, que o resync seria união no-op (data-safe,
        C2) ANTES de reconstruir o baseline com o `--resync` real. Divergência ou
        dry-run ambíguo → NÃO age (fail-safe: permanece degradado). `base_cmd` é o
        cmd de bisync SEM `--resync` (flags/excludes live, git_handling-aware).
        Retorna True só quando o `--resync` real reconstruiu o baseline (rc==0);
        NÃO toca o marker (o tail de sucesso do caller faz). O guard de 1-tentativa
        e a limpeza do marker são responsabilidade do caller.
        """
        log.info("[%s] [BISYNC_AUTORESYNC] attempted (rc=7 stale-listings)", folder.name)
        _rc, out, err = await _run(base_cmd + ["--resync", "--dry-run"], timeout=timeout)
        if not _dryrun_resync_is_noop(out + err):
            log.warning(
                "[%s] [BISYNC_AUTORESYNC] skipped (divergent: dry-run não provou "
                "união no-op) — permanece degradado, recovery manual (playbook)",
                folder.name,
            )
            return False
        rc, _o, rerr = await _run(base_cmd + ["--resync"], timeout=timeout)
        if rc != 0:
            summary, path = _capture_stderr("bisync-autoresync", folder.name, rerr)
            log.error(
                "[%s] [BISYNC_AUTORESYNC] skipped (resync real falhou rc=%d): %s "
                "(full stderr: %s)",
                folder.name, rc, summary, path,
            )
            return False
        log.info(
            "[%s] [BISYNC_AUTORESYNC] recovered (baseline reconstruído via --resync)",
            folder.name,
        )
        return True

    async def bisync_folder(
        self,
        folder: FolderConfig,
        local_override: Path | None = None,
        extra_excludes: list[str] | None = None,
    ) -> bool:
        """Executa bisync para uma tarefa. Retorna True em sucesso.

        extra_excludes (ADR-008): padrões adicionais injetados pelo daemon em
        modo `auto` — paths de repos descobertos viram excludes para que o
        bisync cubra só conteúdo não-repo (skip excluído do sync; bundle
        sincronizado em flow separado).
        """
        local = local_override or folder.local_path
        remote = remote_uri_for(folder, self.app)
        marker = _state_marker_for(local, remote)
        timeout = self._job_timeout(folder)

        local.mkdir(parents=True, exist_ok=True)

        if not await self._ensure_remote_dir(remote, folder.name, timeout=timeout):
            return False

        cmd = self._base_cmd() + ["bisync", str(local), remote]
        # Resolução de conflitos: vence quem foi modificado por último.
        cmd += ["--conflict-resolve", "newer", "--conflict-loser", "delete"]
        # Cria diretórios vazios também.
        cmd += ["--create-empty-src-dirs"]

        # Mescla excludes: usuário + presets automáticos (quando aplicável) + extras do daemon.
        # `bundle` não passa por aqui; `auto` injeta extras via extra_excludes quando há
        # repos descobertos (ADR-008).
        excludes: list[str] = list(folder.exclude)
        if folder.auto_exclude:
            # Mantém ordem (usuário primeiro, depois presets) e remove duplicatas.
            seen: set[str] = set(excludes)
            for pat in default_excludes_for_code():
                if pat not in seen:
                    excludes.append(pat)
                    seen.add(pat)
        if extra_excludes:
            seen = set(excludes)
            for pat in extra_excludes:
                if pat not in seen:
                    excludes.append(pat)
                    seen.add(pat)
        for pat in excludes:
            cmd += ["--exclude", pat]

        # Primeira execução: precisa de --resync.
        if not marker.exists():
            log.info("[%s] Primeira sincronização — executando --resync.", folder.name)
            cmd += ["--resync"]

        rc, _out, err = await _run(cmd, timeout=timeout)
        if rc != 0:
            summary, path = _capture_stderr("bisync", folder.name, err)
            log.error(
                "[%s] [BISYNC_FAIL] rc=%d: %s (full stderr: %s)",
                folder.name, rc, summary, path,
            )
            # Advice safe de too-many-deletes (#52): o `[BISYNC_FAIL]` acima ecoa a
            # dica `--force` do rclone (é a linha ERROR verbatim — contrato ADR-012).
            # Essa dica é PERIGOSA: `--force` propaga as deleções → perda de dados.
            # Emite um contra-advice greppável apontando pro branch rc=1 do playbook,
            # sem auto-agir (divergência real → invariante `bisync errors do NOT
            # auto-recover` vale; recuperação é decisão consciente do operador).
            if _is_too_many_deletes(err):
                log.warning(
                    "[%s] [BISYNC_SAFETY_ABORT] too-many-deletes: uma mudança em "
                    "massa removeu >50%% dos itens da visão do bisync. NÃO rode "
                    "`--force` às cegas — propaga as deleções e causa PERDA DE DADOS "
                    "se o conteúdo não estiver salvo em outro lugar. Decida a "
                    "intenção primeiro: veja o branch rc=1 em "
                    "docs/operations/playbook-bisync-recovery.md",
                    folder.name,
                )
            # Auto-recuperação gated de rc=7 stale-listings (ADR-019, #47): quando o
            # abort é stale-listings E um dry-run prova que o `--resync` seria união
            # no-op, reconstrói o baseline em vez de ficar degradado indefinidamente.
            # Exceção RESTRITA ao invariante "bisync errors do NOT auto-recover":
            # divergência real ou dry-run ambíguo → NÃO age (data-safety C2). Guard
            # de 1-tentativa-por-episódio evita re-disparar o dry-run a cada ciclo.
            if (
                self.app.rclone.auto_resync_stale_listings
                and _is_stale_listings(err)
                and marker not in self._autoresync_attempted
            ):
                self._autoresync_attempted.add(marker)
                try:
                    recovered = await self._attempt_gated_autoresync(folder, cmd, timeout)
                except Exception:
                    # Attempt abortado por exceção (ex.: AuthDegradedError de storm
                    # remanescente — o mesmo storm que causou o rc=7; ou StuckJobError):
                    # NENHUM veredito no-op/divergente foi alcançado, então NÃO consumiu
                    # a tentativa do episódio. Libera o guard para re-tentar no próximo
                    # ciclo e re-levanta (o daemon trata auth/stuck). Sem este discard,
                    # um blip transitório barraria a auto-recuperação até o restart.
                    self._autoresync_attempted.discard(marker)
                    raise
                if not recovered:
                    return False
                # Recuperado — cai no tail de sucesso abaixo.
            else:
                return False

        marker.touch()
        self._autoresync_attempted.discard(marker)
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
        rc, _o, err = await _run(cmd, timeout=self._job_timeout(folder))
        if rc != 0:
            summary, path = _capture_stderr(
                "upload-bundle", folder.name, err, sub=rel_subpath
            )
            log.error(
                "[%s] [BUNDLE_UPLOAD_FAIL] %s → %s: %s (full stderr: %s)",
                folder.name, bundle, remote, summary, path,
            )
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
        rc, _o, err = await _run(cmd, timeout=self._job_timeout(folder))
        if rc != 0:
            # "directory not found" / "object not found" significa que o
            # bundle ainda não existe na nuvem — não é erro.
            if "not found" in err.lower():
                log.debug("Bundle remoto inexistente em %s — nada a baixar.", remote)
                return True
            summary, path = _capture_stderr(
                "download-bundle", folder.name, err, sub=rel_subpath
            )
            log.error(
                "[%s] [BUNDLE_DOWNLOAD_FAIL] %s: %s (full stderr: %s)",
                folder.name, remote, summary, path,
            )
            return False
        return True
