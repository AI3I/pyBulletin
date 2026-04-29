from __future__ import annotations

import re
from pathlib import Path


STATIC = Path("src/pybulletin/web/static")


def test_sysop_static_get_element_ids_exist():
    js = (STATIC / "sysop.js").read_text(encoding="utf-8")
    html = (STATIC / "sysop.html").read_text(encoding="utf-8")

    html_ids = set(re.findall(r'id="([^"]+)"', html))
    js_static_refs = set(re.findall(r'getElementById\("([^"]+)"\)', js))

    assert js_static_refs <= html_ids
