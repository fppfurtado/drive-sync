"""Diagnóstico `--dry-run-resync <folder>` (#94).

Responde, ANTES de qualquer ação, à pergunta que governa o recovery de um abort
rc=7: **o `--resync` deste folder seria só-adições (seguro) ou sobrescreveria
conteúdo divergente (destrutivo)?**

Existe porque o playbook manual pedia ao operador (a) um pré-check que só compara
o top-level — cego para a divergência que de fato decide — e (b) transcrever ~45
flags/excludes à mão para rodar o dry-run, com o risco de o veredito não
corresponder ao que o daemon faria. Aqui os flags vêm de
`RcloneEngine.build_bisync_cmd`, a mesma fonte que o daemon usa.

Não muta nada: roda com `--dry-run` e num `--workdir` temporário próprio, de modo
que o cache de bisync real (`~/.cache/rclone/bisync`) fica intocado — o daemon
pode seguir rodando sem ver estado estranho.
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from .config import AppConfig, FolderConfig
from .listing_diff import Divergence, ListingParseError, classify_dry_run
from .sync_engine import RcloneEngine, _run, remote_uri_for

# Limite de paths listados por seção no relatório (o resto vira "… e mais N").
_MAX_SHOWN = 20


class DryRunResyncError(Exception):
    """Falha ao produzir um veredito (folder inválido, rclone falhou, parse)."""


def _format_section(title: str, paths: list[str]) -> list[str]:
    if not paths:
        return []
    out = [f"{title} ({len(paths)}):"]
    for p in paths[:_MAX_SHOWN]:
        out.append(f"  {p}")
    if len(paths) > _MAX_SHOWN:
        out.append(f"  … e mais {len(paths) - _MAX_SHOWN}")
    return out


def render_report(folder_name: str, remote: str, div: Divergence) -> str:
    """Relatório legível, com o veredito data-safe em destaque."""
    lines = [f"folder: {folder_name}", f"remote: {remote}", ""]

    if div.is_noop:
        lines.append("VEREDITO: união NO-OP — as duas árvores já batem.")
        lines.append("")
        lines.append("O `--resync` reconstruiria o baseline sem transferir nada.")
        lines.append(f"({div.identical} paths idênticos nos dois lados)")
        return "\n".join(lines)

    lines.extend(_format_section("Só no Path1 (local) — seriam ENVIADOS", div.only_path1))
    if div.only_path1:
        lines.append("")
    lines.extend(_format_section("Só no Path2 (remote) — seriam BAIXADOS", div.only_path2))
    if div.only_path2:
        lines.append("")
    lines.extend(
        _format_section(
            "Divergentes nos DOIS lados — seriam SOBRESCRITOS (perda do lado perdedor)",
            div.overwrites,
        )
    )
    if div.overwrites:
        lines.append("")

    if div.additive_only:
        lines.append("VEREDITO: SÓ-ADIÇÕES — data-safe.")
        lines.append("")
        lines.append(
            "Nenhum arquivo existe nos dois lados com conteúdo divergente, então o\n"
            "`--resync` apenas cria — não há como destruir dado. Recovery pelo\n"
            "playbook rc=7 (deletar o marker do par) é seguro."
        )
    else:
        lines.append("VEREDITO: TEM OVERWRITE — NÃO é data-safe.")
        lines.append("")
        lines.append(
            "Os paths acima existem nos DOIS lados com size/mtime divergente. Um\n"
            "`--resync` sobrescreveria o lado perdedor, destruindo aquela versão —\n"
            "e no log isso apareceria como uma inócua `Skipped copy`.\n"
            "Decida a direção conscientemente antes de agir (branch rc=1 do playbook)."
        )
    lines.append("")
    lines.append(f"({div.identical} paths idênticos nos dois lados)")
    return "\n".join(lines)


async def _classify(
    cfg: AppConfig, folder: FolderConfig, workdir: Path
) -> tuple[str, Divergence]:
    engine = RcloneEngine(cfg)
    remote = remote_uri_for(folder, cfg)
    cmd = engine.build_bisync_cmd(folder, folder.local_path, remote)
    cmd += ["--resync", "--dry-run", "--workdir", str(workdir)]

    rc, _out, err = await _run(cmd)
    try:
        div = classify_dry_run(workdir)
    except ListingParseError as exc:
        # rc≠0 é a causa provável do listing faltante — reporta a causa, não o sintoma.
        if rc != 0:
            raise DryRunResyncError(
                f"rclone falhou (rc={rc}) e não produziu listings utilizáveis.\n"
                f"{err.strip()[-800:]}"
            ) from exc
        raise DryRunResyncError(str(exc)) from exc
    return remote, div


def run_dry_run_resync(cfg: AppConfig, folder_name: str) -> int:
    """Executa o diagnóstico e imprime o relatório.

    Exit code: 0 = seguro (no-op ou só-adições) · 1 = tem overwrite · 2 = erro.
    O código é o contrato para scripting; o relatório é para o operador.
    """
    folder = next((f for f in cfg.folders if f.name == folder_name), None)
    if folder is None:
        known = ", ".join(f.name for f in cfg.folders) or "(nenhum)"
        print(f"erro: folder {folder_name!r} não existe no config. Conhecidos: {known}")
        return 2

    with tempfile.TemporaryDirectory(prefix="drive-sync-dryrun-") as tmp:
        try:
            remote, div = asyncio.run(_classify(cfg, folder, Path(tmp)))
        except DryRunResyncError as exc:
            print(f"erro: {exc}")
            return 2

    print(render_report(folder.name, remote, div))
    return 0 if div.additive_only else 1
