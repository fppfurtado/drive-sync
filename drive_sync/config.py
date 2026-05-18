"""Carregamento e validação do arquivo de configuração."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _expand(p: str) -> Path:
    """Expande ~ e variáveis de ambiente, retornando Path absoluto."""
    return Path(os.path.expandvars(os.path.expanduser(p))).resolve()


# ---------------------------------------------------------------------------
# Dataclasses tipadas — facilitam autocomplete e centralizam defaults
# ---------------------------------------------------------------------------
@dataclass
class FolderConfig:
    name: str
    local_path: Path
    remote_subpath: str
    enabled: bool = True
    # "off"     → bisync puro, sem nenhum tratamento especial
    # "bisync"  → bisync com excludes automáticos de artefatos de build
    # "bundle"  → empacota com `git bundle` (só histórico commitado)
    git_mode: str = "bisync"
    exclude: list[str] = field(default_factory=list)
    # Se True (e git_mode != "bundle"), aplica os excludes do
    # exclude_presets.default_excludes_for_code() em adição aos do usuário.
    auto_exclude: bool = True
    debounce_seconds: int = 5
    cooldown_seconds: int = 0


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

    folders: list[FolderConfig] = []
    seen_names: set[str] = set()
    for entry in folders_raw:
        name = entry["name"]
        if name in seen_names:
            raise ValueError(f"Nome de pasta duplicado em 'folders': {name!r}")
        seen_names.add(name)

        git_mode = entry.get("git_mode", "bisync")
        if git_mode not in ("off", "bisync", "bundle"):
            raise ValueError(
                f"git_mode inválido em {name!r}: {git_mode!r} "
                f"(use 'off', 'bisync' ou 'bundle')"
            )

        cooldown_seconds = int(entry.get("cooldown_seconds", 0))
        if cooldown_seconds < 0:
            raise ValueError(
                f"cooldown_seconds inválido em {name!r}: {cooldown_seconds} "
                f"(deve ser >= 0; use 0 para desligar)"
            )

        folders.append(
            FolderConfig(
                name=name,
                local_path=_expand(entry["local_path"]),
                remote_subpath=entry["remote_subpath"].strip("/"),
                enabled=bool(entry.get("enabled", True)),
                git_mode=git_mode,
                exclude=list(entry.get("exclude", [])),
                auto_exclude=bool(entry.get("auto_exclude", True)),
                debounce_seconds=int(entry.get("debounce_seconds", 5)),
                cooldown_seconds=cooldown_seconds,
            )
        )

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
