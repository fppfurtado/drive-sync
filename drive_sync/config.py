"""Carregamento e validação do arquivo de configuração."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

_VALID_GIT_HANDLING = ("auto", "skip", "bundle", "plain")
_VALID_REPO_OVERRIDE_MODES = ("skip", "bundle")
_VALID_SUBPATH_OVERRIDE_HANDLING = ("skip", "bundle", "plain")
_LEGACY_GIT_MODE_VALUES = ("bisync", "bundle", "off")
_PLAYBOOK_PATH = "docs/operations/playbook-flip-git-handling.md"
# ADR-010: markers de build artifacts canônicos (Python/JS/Rust) — disparam
# erro fatal no --check quando auto_exclude: false E scan detecta no local_path.
# Match exato por Path.name (não substring): .venv-backup/ ou node_modules_old/
# NÃO disparam. .git/ é fora do escopo (ADR-008 cobre estruturalmente).
_AUTO_EXCLUDE_CODE_MARKERS = (".venv", "node_modules", "target")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _expand(p: str) -> Path:
    """Expande ~ e variáveis de ambiente, retornando Path absoluto."""
    return Path(os.path.expandvars(os.path.expanduser(p))).resolve()


def _normalize_subpath(parent_name: str, raw_subpath: str, field_label: str) -> str:
    """Valida shape de subpath; retorna versão normalizada (sem leading/trailing /)."""
    subpath = raw_subpath.strip("/")
    if not subpath:
        raise ValueError(
            f"{field_label} em {parent_name!r}: subpath vazio"
        )
    if raw_subpath.startswith("/"):
        raise ValueError(
            f"{field_label} em {parent_name!r}: subpath {raw_subpath!r} "
            f"não pode ser absoluto"
        )
    if ".." in subpath.split("/"):
        raise ValueError(
            f"{field_label} em {parent_name!r}: subpath {raw_subpath!r} "
            f"não pode conter '..'"
        )
    return subpath


def _parse_subpath_overrides(
    parent_name: str, raw: list[dict[str, Any]]
) -> list["SubpathOverride"]:
    """Valida e converte raw YAML de subpath_overrides (ADR-006 + ADR-008)."""
    overrides: list[SubpathOverride] = []
    seen_subpaths: set[str] = set()
    for item in raw:
        if "git_mode" in item:
            legacy_value = item["git_mode"]
            raise ValueError(
                f"subpath_overrides em {parent_name!r}: chave 'git_mode' removida "
                f"(ADR-008) — use 'git_handling' "
                f"(valores: {', '.join(repr(v) for v in _VALID_SUBPATH_OVERRIDE_HANDLING)}). "
                f"Valor legado {legacy_value!r} mapeia para "
                f"{_legacy_subpath_migration_hint(legacy_value)!r}. "
                f"Veja {_PLAYBOOK_PATH}."
            )
        raw_subpath = item.get("subpath") or ""
        subpath = _normalize_subpath(parent_name, raw_subpath, "subpath_overrides")
        git_handling = item.get("git_handling")
        if git_handling not in _VALID_SUBPATH_OVERRIDE_HANDLING:
            raise ValueError(
                f"subpath_overrides em {parent_name!r}: git_handling {git_handling!r} "
                f"inválido para subpath {raw_subpath!r} "
                f"(use {', '.join(repr(v) for v in _VALID_SUBPATH_OVERRIDE_HANDLING)})"
            )
        if subpath in seen_subpaths:
            raise ValueError(
                f"subpath_overrides em {parent_name!r}: subpath {raw_subpath!r} "
                f"duplicado"
            )
        for prior in seen_subpaths:
            if subpath.startswith(prior + "/") or prior.startswith(subpath + "/"):
                raise ValueError(
                    f"subpath_overrides em {parent_name!r}: subpath {raw_subpath!r} "
                    f"está aninhado com {prior!r} — sobreposição não permitida"
                )
        seen_subpaths.add(subpath)
        overrides.append(SubpathOverride(subpath=subpath, git_handling=git_handling))
    return overrides


def _parse_repo_overrides(
    parent_name: str, raw: list[dict[str, Any]]
) -> list["RepoOverride"]:
    """Valida e converte raw YAML de repo_overrides (ADR-008)."""
    overrides: list[RepoOverride] = []
    seen_subpaths: set[str] = set()
    for item in raw:
        raw_subpath = item.get("repo_subpath") or ""
        subpath = _normalize_subpath(parent_name, raw_subpath, "repo_overrides")
        mode = item.get("mode")
        if mode not in _VALID_REPO_OVERRIDE_MODES:
            raise ValueError(
                f"repo_overrides em {parent_name!r}: mode {mode!r} "
                f"inválido para repo_subpath {raw_subpath!r} "
                f"(use {', '.join(repr(v) for v in _VALID_REPO_OVERRIDE_MODES)})"
            )
        if subpath in seen_subpaths:
            raise ValueError(
                f"repo_overrides em {parent_name!r}: repo_subpath {raw_subpath!r} "
                f"duplicado"
            )
        seen_subpaths.add(subpath)
        overrides.append(RepoOverride(repo_subpath=subpath, mode=mode))
    return overrides


def _validate_auto_exclude_against_code(folder: "FolderConfig", max_depth: int) -> None:
    """ADR-010: rejeita auto_exclude: false quando scan detecta markers de código.

    Walk recursivo até max_depth (reusa git.max_recursion_depth, default 6) procurando
    dirs cujo Path.name casa exato um marker em _AUTO_EXCLUDE_CODE_MARKERS. Skip
    silente quando auto_exclude is True (caso comum) OU quando local_path não existe
    (paridade com find_git_repos). .git/ inteiro fica fora do escopo do scan (ADR-008
    endereça repos git via git_handling estruturalmente).
    """
    if folder.auto_exclude:
        return
    # Modos `bundle` e `skip` não passam por bisync — auto_exclude não se aplica
    # (ADR-008 dispatch). Apenas `auto` (bisync do não-repo com extra_excludes) e
    # `plain` (bisync puro) usam auto_exclude meaningfully.
    if folder.git_handling in ("bundle", "skip"):
        return
    if not folder.local_path.exists():
        return

    hits: list[Path] = []
    root = folder.local_path
    for dirpath, dirnames, _filenames in os.walk(root):
        rel_parts = Path(dirpath).relative_to(root).parts
        # ADR-010 §Decisão (3): .git/ fora do escopo — ADR-008 cobre.
        if ".git" in rel_parts:
            dirnames.clear()
            continue
        if len(rel_parts) >= max_depth:
            dirnames.clear()
            continue
        for d in dirnames:
            if d in _AUTO_EXCLUDE_CODE_MARKERS:
                hits.append(Path(dirpath) / d)

    if not hits:
        return

    paths_listing = "\n".join(f"  - {p}/" for p in hits)
    raise ValueError(
        f"auto_exclude: false em {folder.name!r} E scan de {folder.local_path} "
        f"detectou paths de código:\n"
        f"{paths_listing}\n\n"
        f"Defina `auto_exclude: true` (recomendado: cobre todos os build artifacts "
        f"conhecidos sem listagem manual).\n\n"
        f"Se precisa manter `auto_exclude: false` por razão específica, adicione globs "
        f"em `exclude:` que cubram os paths listados acima e re-execute `--check`."
    )


def _validate_repo_subpath_overlap(
    parent_name: str,
    repo_overrides: list["RepoOverride"],
    subpath_overrides: list["SubpathOverride"],
) -> None:
    """ADR-008: rejeita sobreposição entre repo_overrides e subpath_overrides."""
    repo_paths = {o.repo_subpath for o in repo_overrides}
    for sp in subpath_overrides:
        if sp.subpath in repo_paths:
            raise ValueError(
                f"{parent_name!r}: subpath {sp.subpath!r} aparece em ambos "
                f"repo_overrides e subpath_overrides — sobreposição não permitida "
                f"(ADR-008 §Coexistência). Mantenha em apenas um."
            )


def _expand_synthetic_folder(
    parent: "FolderConfig", override: "SubpathOverride"
) -> "FolderConfig":
    """Cria FolderConfig synthetic a partir de parent + override (ADR-006)."""
    synthetic_name = f"{parent.name}/{override.subpath}"
    return FolderConfig(
        name=synthetic_name,
        local_path=parent.local_path / override.subpath,
        remote_subpath=f"{parent.remote_subpath}/{override.subpath}".strip("/"),
        enabled=parent.enabled,
        git_handling=override.git_handling,
        exclude=list(parent.exclude),
        auto_exclude=parent.auto_exclude,
        debounce_seconds=parent.debounce_seconds,
        cooldown_seconds=parent.cooldown_seconds,
        subpath_overrides=[],
        repo_overrides=[],
        fs_key=synthetic_name.replace("/", "-"),
    )


# ---------------------------------------------------------------------------
# Dataclasses tipadas — facilitam autocomplete e centralizam defaults
# ---------------------------------------------------------------------------
@dataclass
class SubpathOverride:
    """Override de git_handling para um subpath dentro de um folder (ADR-006 + ADR-008).

    git_handling aceita apenas 'skip', 'bundle' ou 'plain' — 'auto' não aplica em
    subpath (auto é varredura do folder; o operador já apontou o caminho exato).
    """

    subpath: str
    git_handling: str


@dataclass
class RepoOverride:
    """Override por repo descoberto (ADR-008). repo_subpath casa repo escaneado.

    repo_subpath é relativo ao folder.local_path e nunca vazio — override do
    repo na raiz do folder (quando folder.local_path é ele próprio um repo git)
    não passa por aqui; use folder.git_handling diretamente.
    """

    repo_subpath: str
    mode: str


@dataclass
class FolderConfig:
    name: str
    local_path: Path
    remote_subpath: str
    enabled: bool = True
    # "auto"   → scan + git remote -v decide (vazio → bundle; com remote → skip)
    # "skip"   → folder inteiro fora do sync
    # "bundle" → empacota com `git bundle` (só histórico commitado)
    # "plain"  → bisync puro, sem tratamento git (folders não-git)
    git_handling: str = "auto"
    exclude: list[str] = field(default_factory=list)
    # Se True (e git_handling != "bundle"), aplica os excludes do
    # exclude_presets.default_excludes_for_code() em adição aos do usuário.
    auto_exclude: bool = True
    debounce_seconds: int = 5
    cooldown_seconds: int = 0
    subpath_overrides: list[SubpathOverride] = field(default_factory=list)
    repo_overrides: list[RepoOverride] = field(default_factory=list)
    fs_key: str = ""  # slug filesystem-safe; default vira `name` no __post_init__ — ADR-006

    def __post_init__(self) -> None:
        if not self.fs_key:
            self.fs_key = self.name


@dataclass
class RcloneConfig:
    remote_name: str = "proton"
    remote_root: str = "Sync"
    binary: str = "rclone"
    global_flags: list[str] = field(default_factory=list)


@dataclass
class GitConfig:
    bundles_dir: Path = field(default_factory=lambda: _expand("~/.cache/drive-sync/bundles"))
    bundle_suffix: str = ".gitbundle"
    bundle_all: bool = True
    recursive_detection: bool = True
    max_recursion_depth: int = 6


@dataclass
class WatcherConfig:
    queue_size: int = 1000
    max_concurrent_jobs: int = 3
    periodic_full_sync_seconds: int = 1800
    startup_delay_seconds: int = 15
    folder_staleness_threshold_seconds: int = 43200


@dataclass
class DedupeConfig:
    skip_subpaths_of_configured_folders: bool = True


@dataclass
class HealthCheckConfig:
    enabled: bool = True
    interval_seconds: int = 3600


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: Path = field(default_factory=lambda: _expand("~/.local/state/drive-sync/drive-sync.log"))
    max_bytes: int = 5 * 1024 * 1024
    backup_count: int = 5
    console: bool = True


@dataclass
class AppConfig:
    rclone: RcloneConfig
    folders: list[FolderConfig]
    git: GitConfig
    watcher: WatcherConfig
    dedupe: DedupeConfig
    health_check: HealthCheckConfig
    logging: LoggingConfig
    source_path: Path  # de onde o arquivo foi carregado (debug)


# ---------------------------------------------------------------------------
# Resolução do caminho do config
# ---------------------------------------------------------------------------
def default_config_path() -> Path:
    """Segue XDG: $XDG_CONFIG_HOME/drive-sync/config.yaml."""
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return _expand(f"{base}/drive-sync/config.yaml")


# ---------------------------------------------------------------------------
# Loader principal
# ---------------------------------------------------------------------------
def load_config(path: Path | None = None) -> AppConfig:
    """Lê o YAML, valida, e devolve um AppConfig pronto para uso."""
    cfg_path = path or default_config_path()
    if not cfg_path.exists():
        raise FileNotFoundError(
            f"Arquivo de configuração não encontrado: {cfg_path}\n"
            f"Copie config.example.yaml para esse local e edite-o."
        )

    raw: dict[str, Any] = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}

    # ---- folders (obrigatório, ao menos uma entrada) ----
    folders_raw = raw.get("folders") or []
    if not folders_raw:
        raise ValueError("Configuração inválida: a chave 'folders' não pode estar vazia.")

    # Pré-parse de git.max_recursion_depth para reuso na validação de auto_exclude
    # (ADR-010 §Decisão (2) — uniformidade com infra existente).
    git_max_recursion_depth = int((raw.get("git") or {}).get("max_recursion_depth", 6))

    folders: list[FolderConfig] = []
    seen_names: set[str] = set()
    for entry in folders_raw:
        name = entry["name"]
        if name in seen_names:
            raise ValueError(f"Nome de pasta duplicado em 'folders': {name!r}")
        seen_names.add(name)

        if "git_mode" in entry:
            legacy_value = entry["git_mode"]
            raise ValueError(
                f"git_mode em {name!r}: chave removida (ADR-008) — use 'git_handling' "
                f"(valores: {', '.join(repr(v) for v in _VALID_GIT_HANDLING)}). "
                f"Valor legado {legacy_value!r} mapeia para "
                f"{_legacy_migration_hint(legacy_value)!r}. Veja {_PLAYBOOK_PATH}."
            )

        git_handling = entry.get("git_handling", "auto")
        if git_handling not in _VALID_GIT_HANDLING:
            raise ValueError(
                f"git_handling inválido em {name!r}: {git_handling!r} "
                f"(use {', '.join(repr(v) for v in _VALID_GIT_HANDLING)})"
            )

        cooldown_seconds = int(entry.get("cooldown_seconds", 0))
        if cooldown_seconds < 0:
            raise ValueError(
                f"cooldown_seconds inválido em {name!r}: {cooldown_seconds} "
                f"(deve ser >= 0; use 0 para desligar)"
            )

        overrides_raw = entry.get("subpath_overrides") or []
        overrides = _parse_subpath_overrides(name, overrides_raw)

        repo_overrides_raw = entry.get("repo_overrides") or []
        repo_overrides = _parse_repo_overrides(name, repo_overrides_raw)
        _validate_repo_subpath_overlap(name, repo_overrides, overrides)

        parent = FolderConfig(
            name=name,
            local_path=_expand(entry["local_path"]),
            remote_subpath=entry["remote_subpath"].strip("/"),
            enabled=bool(entry.get("enabled", True)),
            git_handling=git_handling,
            exclude=list(entry.get("exclude", [])),
            auto_exclude=bool(entry.get("auto_exclude", True)),
            debounce_seconds=int(entry.get("debounce_seconds", 5)),
            cooldown_seconds=cooldown_seconds,
            subpath_overrides=overrides,
            repo_overrides=repo_overrides,
        )
        folders.append(parent)
        _validate_auto_exclude_against_code(parent, git_max_recursion_depth)

        # Ordem load-bearing: clone do exclude (em _expand_synthetic_folder)
        # acontece ANTES do append do glob no parent — synthetic não exclui a si mesmo.
        for override in overrides:
            synthetic = _expand_synthetic_folder(parent, override)
            if synthetic.name in seen_names:
                raise ValueError(
                    f"subpath_overrides em {name!r}: nome synthetic "
                    f"{synthetic.name!r} colide com folder já declarado"
                )
            seen_names.add(synthetic.name)
            folders.append(synthetic)
            glob = f"{override.subpath}/**"
            if glob in parent.exclude:
                log.warning(
                    "[%s] exclude redundante de %r — injetado automaticamente "
                    "por subpath_overrides (ADR-006)",
                    parent.name,
                    glob,
                )
            else:
                parent.exclude.append(glob)

    # ---- demais seções (todas opcionais — usam defaults do dataclass) ----
    rclone_raw = raw.get("rclone", {}) or {}
    rclone = RcloneConfig(
        remote_name=rclone_raw.get("remote_name", "drive"),
        remote_root=rclone_raw.get("remote_root", "Sync").strip("/"),
        binary=rclone_raw.get("binary", "rclone"),
        global_flags=list(rclone_raw.get("global_flags", [])),
    )

    git_raw = raw.get("git", {}) or {}
    git = GitConfig(
        bundles_dir=_expand(git_raw.get("bundles_dir", "~/.cache/drive-sync/bundles")),
        bundle_suffix=git_raw.get("bundle_suffix", ".gitbundle"),
        bundle_all=bool(git_raw.get("bundle_all", True)),
        recursive_detection=bool(git_raw.get("recursive_detection", True)),
        max_recursion_depth=int(git_raw.get("max_recursion_depth", 6)),
    )

    w_raw = raw.get("watcher", {}) or {}
    folder_staleness_threshold_seconds = int(
        w_raw.get("folder_staleness_threshold_seconds", 43200)
    )
    periodic_full_sync_seconds = int(w_raw.get("periodic_full_sync_seconds", 1800))
    if folder_staleness_threshold_seconds < 0:
        raise ValueError(
            f"folder_staleness_threshold_seconds inválido: "
            f"{folder_staleness_threshold_seconds} (deve ser >= 0; use 0 para desligar)"
        )
    if folder_staleness_threshold_seconds > 0 and periodic_full_sync_seconds <= 0:
        raise ValueError(
            "folder_staleness_threshold_seconds > 0 requer "
            "watcher.periodic_full_sync_seconds > 0 (detecção piggyback no loop "
            "periódico — ADR-005). Defina folder_staleness_threshold_seconds=0 "
            "para opt-out, ou habilite periodic full-sync."
        )
    watcher = WatcherConfig(
        queue_size=int(w_raw.get("queue_size", 1000)),
        max_concurrent_jobs=int(w_raw.get("max_concurrent_jobs", 3)),
        periodic_full_sync_seconds=periodic_full_sync_seconds,
        startup_delay_seconds=int(w_raw.get("startup_delay_seconds", 15)),
        folder_staleness_threshold_seconds=folder_staleness_threshold_seconds,
    )

    d_raw = raw.get("dedupe", {}) or {}
    dedupe = DedupeConfig(
        skip_subpaths_of_configured_folders=bool(
            d_raw.get("skip_subpaths_of_configured_folders", True)
        ),
    )

    h_raw = raw.get("health_check", {}) or {}
    health_check = HealthCheckConfig(
        enabled=bool(h_raw.get("enabled", True)),
        interval_seconds=int(h_raw.get("interval_seconds", 3600)),
    )

    l_raw = raw.get("logging", {}) or {}
    logging_cfg = LoggingConfig(
        level=str(l_raw.get("level", "INFO")).upper(),
        file=_expand(l_raw.get("file", "~/.local/state/drive-sync/drive-sync.log")),
        max_bytes=int(l_raw.get("max_bytes", 5 * 1024 * 1024)),
        backup_count=int(l_raw.get("backup_count", 5)),
        console=bool(l_raw.get("console", True)),
    )

    return AppConfig(
        rclone=rclone,
        folders=folders,
        git=git,
        watcher=watcher,
        dedupe=dedupe,
        health_check=health_check,
        logging=logging_cfg,
        source_path=cfg_path,
    )


def _legacy_migration_hint(legacy_value: Any) -> str:
    """Retorna o git_handling equivalente para o git_mode legado, para mensagem de migração."""
    mapping = {"bisync": "auto", "bundle": "bundle", "off": "plain"}
    return mapping.get(legacy_value, "auto")


def _legacy_subpath_migration_hint(legacy_value: Any) -> str:
    """Mapping legado para subpath_overrides — 'bisync' não tem equivalente direto."""
    mapping = {"bundle": "bundle", "off": "plain"}
    return mapping.get(legacy_value, "<sem equivalente; bisync no subpath não é suportado>")
