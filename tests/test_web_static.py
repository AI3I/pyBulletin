from __future__ import annotations

import re
from pathlib import Path


STATIC = Path("src/pybulletin/web/static")


def _ids(path: Path) -> set[str]:
    return set(re.findall(r'id="([^"]+)"', path.read_text(encoding="utf-8")))


def _static_get_element_refs(path: Path) -> set[str]:
    js = path.read_text(encoding="utf-8")
    return set(re.findall(r'getElementById\("([^"]+)"\)', js))


def _asset_refs(path: Path) -> set[str]:
    html = path.read_text(encoding="utf-8")
    return set(re.findall(r'(?:src|href)="([^"]+)"', html))


def test_public_static_get_element_ids_exist():
    assert _static_get_element_refs(STATIC / "app.js") <= _ids(STATIC / "index.html")


def test_sysop_static_get_element_ids_exist():
    assert _static_get_element_refs(STATIC / "sysop.js") <= _ids(STATIC / "sysop.html")


def test_html_static_assets_exist():
    for page in ("index.html", "sysop.html"):
        for ref in _asset_refs(STATIC / page):
            if "://" in ref or ref.startswith(("/", "#")):
                continue
            assert (STATIC / ref).is_file(), f"{page} references missing asset {ref}"
