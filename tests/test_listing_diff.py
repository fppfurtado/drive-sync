"""Testes da classificação de divergência por listings (#94).

As amostras reproduzem o formato real capturado do rclone v1.74.3 — inclusive a
assimetria de precisão de mtime entre protondrive (segundos) e FS local
(nanossegundos), que é o caso que um comparador ingênuo erraria.
"""
from __future__ import annotations

import pytest

from drive_sync.listing_diff import (
    ListingParseError,
    compare_listings,
    parse_listing,
)

HEADER = "# bisync listing v1 from 2026-09-21T11:35:39.728281922+0000\n"


def _write(tmp_path, name, body):
    p = tmp_path / name
    p.write_text(HEADER + body, encoding="utf-8")
    return p


def _entries(tmp_path, name, body):
    return parse_listing(_write(tmp_path, name, body))


# --- parsing ---------------------------------------------------------------

def test_parse_reads_files_and_dirs(tmp_path):
    e = _entries(
        tmp_path, "a.lst",
        '-   981416 - - 2026-08-26T14:19:31.000000000+0000 "2ºVia IPTU.pdf"\n'
        'd       -1 - - 2026-08-16T18:15:33.000000000+0000 "caldav"\n',
    )
    assert set(e) == {"2ºVia IPTU.pdf", "caldav"}
    assert e["2ºVia IPTU.pdf"].size == 981416
    assert e["2ºVia IPTU.pdf"].is_dir is False
    assert e["caldav"].is_dir is True


def test_parse_handles_paths_with_spaces_and_slashes(tmp_path):
    e = _entries(
        tmp_path, "a.lst",
        '-  6 - - 2026-08-26T14:19:31.000000000+0000 "learning/erros e logs/nota.md"\n',
    )
    assert "learning/erros e logs/nota.md" in e


def test_parse_rejects_missing_header(tmp_path):
    p = tmp_path / "a.lst"
    p.write_text('-  6 - - 2026-08-26T14:19:31.000000000+0000 "x"\n', encoding="utf-8")
    with pytest.raises(ListingParseError, match="cabeçalho"):
        parse_listing(p)


def test_parse_rejects_unknown_line(tmp_path):
    with pytest.raises(ListingParseError, match="não reconhecida"):
        _entries(tmp_path, "a.lst", "isto não é uma entrada\n")


def test_parse_rejects_missing_file(tmp_path):
    with pytest.raises(ListingParseError, match="não foi possível ler"):
        parse_listing(tmp_path / "inexistente.lst")


# --- classificação ---------------------------------------------------------

def test_identical_trees_are_noop(tmp_path):
    body = '-  6 - - 2026-08-26T14:19:31.000000000+0000 "comum.txt"\n'
    div = compare_listings(_entries(tmp_path, "1.lst", body),
                           _entries(tmp_path, "2.lst", body))
    assert div.is_noop
    assert div.additive_only
    assert div.identical == 1


def test_file_only_on_one_side_is_additive(tmp_path):
    """O caso do incidente: 1 git object só no local, 0 no remote."""
    p1 = _entries(
        tmp_path, "1.lst",
        '-  6 - - 2026-08-26T14:19:31.000000000+0000 "comum.txt"\n'
        '-  42 - - 2026-09-18T12:40:00.000000000+0000 "caldav/.git/objects/86/ff59c"\n',
    )
    p2 = _entries(tmp_path, "2.lst",
                  '-  6 - - 2026-08-26T14:19:31.000000000+0000 "comum.txt"\n')
    div = compare_listings(p1, p2)
    assert div.additive_only is True
    assert div.is_noop is False
    assert div.only_path1 == ["caldav/.git/objects/86/ff59c"]
    assert div.only_path2 == []
    assert div.overwrites == []


def test_divergent_size_is_overwrite(tmp_path):
    """O caso perigoso: mesmo path nos dois lados, conteúdo diferente."""
    p1 = _entries(tmp_path, "1.lst",
                  '-  41 - - 2026-08-26T14:19:31.000000000+0000 "conflito.txt"\n')
    p2 = _entries(tmp_path, "2.lst",
                  '-   3 - - 2026-08-26T14:19:31.000000000+0000 "conflito.txt"\n')
    div = compare_listings(p1, p2)
    assert div.additive_only is False
    assert div.overwrites == ["conflito.txt"]


def test_divergent_mtime_beyond_window_is_overwrite(tmp_path):
    p1 = _entries(tmp_path, "1.lst",
                  '-  10 - - 2026-08-26T14:19:31.000000000+0000 "x.txt"\n')
    p2 = _entries(tmp_path, "2.lst",
                  '-  10 - - 2026-08-26T19:45:02.000000000+0000 "x.txt"\n')
    assert compare_listings(p1, p2).overwrites == ["x.txt"]


def test_mtime_precision_mismatch_is_not_overwrite(tmp_path):
    """protondrive trunca mtime para segundos; o FS local guarda nanossegundos.

    Comparar mtime cru acusaria divergência em TODO arquivo — regressão que
    faria o veredito inverter para 'tem overwrite' no repositório inteiro.
    """
    p1 = _entries(tmp_path, "1.lst",
                  '-  981416 - - 2026-08-26T14:19:31.188035120+0000 "IPTU.pdf"\n')
    p2 = _entries(tmp_path, "2.lst",
                  '-  981416 - - 2026-08-26T14:19:31.000000000+0000 "IPTU.pdf"\n')
    div = compare_listings(p1, p2)
    assert div.additive_only is True
    assert div.overwrites == []
    assert div.identical == 1


def test_file_vs_dir_same_path_is_flagged(tmp_path):
    """Anomalia estrutural não pode passar como benigna."""
    p1 = _entries(tmp_path, "1.lst",
                  '-  10 - - 2026-08-26T14:19:31.000000000+0000 "alvo"\n')
    p2 = _entries(tmp_path, "2.lst",
                  'd  -1 - - 2026-08-26T14:19:31.000000000+0000 "alvo"\n')
    assert compare_listings(p1, p2).additive_only is False


def test_dir_only_on_one_side_is_additive(tmp_path):
    p1 = _entries(tmp_path, "1.lst",
                  'd  -1 - - 2026-08-26T14:19:31.000000000+0000 "novo-dir"\n')
    p2 = _entries(tmp_path, "2.lst", "")
    div = compare_listings(p1, p2)
    assert div.additive_only is True
    assert div.only_path1 == ["novo-dir"]


def test_both_directions_additive(tmp_path):
    p1 = _entries(tmp_path, "1.lst",
                  '-  1 - - 2026-08-26T14:19:31.000000000+0000 "so-local"\n')
    p2 = _entries(tmp_path, "2.lst",
                  '-  1 - - 2026-08-26T14:19:31.000000000+0000 "so-remote"\n')
    div = compare_listings(p1, p2)
    assert div.additive_only is True
    assert div.only_path1 == ["so-local"]
    assert div.only_path2 == ["so-remote"]
