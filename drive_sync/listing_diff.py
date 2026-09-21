"""Classificação de divergência Path1↔Path2 a partir dos listings do rclone (#94).

Responde à pergunta que governa a decisão de recovery de um abort rc=7:
**o `--resync` seria só-adições, ou sobrescreveria conteúdo divergente?**

Por que os listings e não o texto do log (evidência empírica, rclone v1.74.3):

- `--resync` **nunca deleta** — um arquivo removido de um lado é RESTAURADO a
  partir do outro. Gatear por "0 deleções" num resync é sempre-verdadeiro.
- Um arquivo divergente nos dois lados é SOBRESCRITO (o perdedor é destruído), e
  no log isso sai como `Skipped copy as --dry-run is set` — **a mesma assinatura**
  de uma adição pura inofensiva.

Ou seja: a superfície de texto do dry-run **não carrega** a distinção
segura/destrutiva. Os listings carregam — um path presente em apenas um lado é
adição; presente nos dois com metadata divergente é overwrite.

Formato do `.lst` (bisync listing v1):

    # bisync listing v1 from 2026-09-21T11:35:39.728281922+0000
    -   981416 - - 2026-08-26T14:19:31.000000000+0000 "2ºVia IPTU.pdf"
    d       -1 - - 2026-08-16T18:15:33.000000000+0000 "caldav"

Campos: tipo (`-` arquivo, `d` diretório) · size · hash · id · mtime · path
entre aspas. Os campos de hash/id vêm **vazios (`-`) nos dois lados**, inclusive
no protondrive — a comparação é por size + mtime, o mesmo critério que o bisync
já usa por default (sem `--checksum`).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# `<tipo> <size> <hash> <id> <mtime> "<path>"` — path entre aspas (pode conter espaços).
_ENTRY_RE = re.compile(
    r'^(?P<kind>[-d])\s+(?P<size>-?\d+)\s+\S+\s+\S+\s+(?P<mtime>\S+)\s+"(?P<path>.*)"\s*$'
)
_HEADER_PREFIX = "# bisync listing"

# Tolerância default de mtime. O protondrive trunca para segundos
# (`...31.000000000`) enquanto o FS local guarda nanossegundos
# (`...31.188035120`) — comparar mtime cru acusaria divergência em TODO arquivo.
# Espelha o papel do `--modify-window` do rclone.
DEFAULT_MODIFY_WINDOW_SECONDS = 1.0


class ListingParseError(Exception):
    """Listing ausente, ilegível ou de formato não reconhecido (fail-safe)."""


@dataclass(frozen=True)
class Entry:
    kind: str       # "-" arquivo, "d" diretório
    size: int
    mtime: float    # epoch seconds
    path: str

    @property
    def is_dir(self) -> bool:
        return self.kind == "d"


@dataclass
class Divergence:
    """Veredito da comparação de dois listings."""

    only_path1: list[str] = field(default_factory=list)
    only_path2: list[str] = field(default_factory=list)
    overwrites: list[str] = field(default_factory=list)
    identical: int = 0

    @property
    def additive_only(self) -> bool:
        """True se o resync só CRIA — nenhum conteúdo divergente seria sobrescrito.

        Este é o predicado data-safe. Um resync só-adições não pode destruir dado;
        um com overwrites destrói o lado perdedor silenciosamente.
        """
        return not self.overwrites

    @property
    def is_noop(self) -> bool:
        """True se as duas árvores já batem (nada a fazer)."""
        return not self.only_path1 and not self.only_path2 and not self.overwrites


def _parse_mtime(raw: str) -> float:
    # rclone emite RFC3339 com nanossegundos e offset: 2026-08-26T14:19:31.000000000+0000
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+0000"
    # datetime.fromisoformat (< 3.11) não aceita offset sem `:` nem 9 dígitos de
    # fração — normaliza ambos.
    m = re.match(r"^(.*?)(?:\.(\d+))?([+-]\d{2}):?(\d{2})$", text)
    if not m:
        raise ListingParseError(f"mtime não reconhecido: {raw!r}")
    base, frac, oh, om = m.groups()
    micros = (frac or "0")[:6].ljust(6, "0")
    try:
        dt = datetime.fromisoformat(f"{base}.{micros}{oh}:{om}")
    except ValueError as exc:  # pragma: no cover - defensivo
        raise ListingParseError(f"mtime não reconhecido: {raw!r}") from exc
    return dt.timestamp()


def parse_listing(path: Path) -> dict[str, Entry]:
    """Lê um `.lst` do bisync e devolve {path: Entry}.

    Levanta ListingParseError em ausência/ilegibilidade/formato inesperado —
    o chamador trata como "não provado seguro" (fail-safe), nunca como vazio.
    """
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ListingParseError(f"não foi possível ler {path}: {exc}") from exc

    entries: dict[str, Entry] = {}
    saw_header = False
    for lineno, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        if line.startswith(_HEADER_PREFIX):
            saw_header = True
            continue
        if line.startswith("#"):
            continue
        m = _ENTRY_RE.match(line)
        if m is None:
            raise ListingParseError(f"{path}:{lineno}: linha não reconhecida: {line!r}")
        entries[m["path"]] = Entry(
            kind=m["kind"],
            size=int(m["size"]),
            mtime=_parse_mtime(m["mtime"]),
            path=m["path"],
        )

    if not saw_header:
        raise ListingParseError(f"{path}: cabeçalho '{_HEADER_PREFIX}' ausente")
    return entries


def compare_listings(
    path1: dict[str, Entry],
    path2: dict[str, Entry],
    modify_window: float = DEFAULT_MODIFY_WINDOW_SECONDS,
) -> Divergence:
    """Classifica a divergência entre dois listings já parseados.

    Diretórios presentes em um só lado contam como adição (o resync faz mkdir);
    um diretório nunca é "sobrescrito", então pares de diretório são ignorados na
    detecção de overwrite.
    """
    result = Divergence()

    for p in sorted(path1.keys() - path2.keys()):
        result.only_path1.append(p)
    for p in sorted(path2.keys() - path1.keys()):
        result.only_path2.append(p)

    for p in sorted(path1.keys() & path2.keys()):
        e1, e2 = path1[p], path2[p]
        if e1.is_dir or e2.is_dir:
            # Um lado vira diretório e o outro arquivo é anomalia estrutural, não
            # um overwrite de conteúdo — trata como divergência para não passar batido.
            if e1.is_dir != e2.is_dir:
                result.overwrites.append(p)
            else:
                result.identical += 1
            continue
        if e1.size != e2.size or abs(e1.mtime - e2.mtime) > modify_window:
            result.overwrites.append(p)
        else:
            result.identical += 1

    return result


def classify_dry_run(
    workdir: Path,
    modify_window: float = DEFAULT_MODIFY_WINDOW_SECONDS,
) -> Divergence:
    """Localiza os `.lst-dry` que o dry-run escreveu em `workdir` e classifica.

    O nome do arquivo é derivado pelo rclone a partir do par local/remote e varia
    com a versão — por isso localizamos por glob num workdir exclusivo do dry-run,
    em vez de reconstruir o nome (mesma razão pela qual `sync_engine` mantém um
    marker próprio).
    """
    p1 = sorted(workdir.glob("*.path1.lst-dry"))
    p2 = sorted(workdir.glob("*.path2.lst-dry"))
    if len(p1) != 1 or len(p2) != 1:
        raise ListingParseError(
            f"esperado exatamente 1 listing por lado em {workdir}; "
            f"encontrado path1={len(p1)}, path2={len(p2)}"
        )
    return compare_listings(parse_listing(p1[0]), parse_listing(p2[0]), modify_window)
