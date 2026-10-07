"""Normalized image license codes, labels and deed links."""

from __future__ import annotations

import re

from images_app.config import settings

LICENSES = {
    "cc0": ("CC0", "https://creativecommons.org/publicdomain/zero/1.0/"),
    "pd": ("Public domain", "https://creativecommons.org/publicdomain/mark/1.0/"),
    "cc-by": ("CC BY", "https://creativecommons.org/licenses/by/4.0/"),
    "cc-by-sa": ("CC BY-SA", "https://creativecommons.org/licenses/by-sa/4.0/"),
    "cc-by-nc": ("CC BY-NC", "https://creativecommons.org/licenses/by-nc/4.0/"),
    "cc-by-nc-sa": ("CC BY-NC-SA", "https://creativecommons.org/licenses/by-nc-sa/4.0/"),
    "cc-by-nd": ("CC BY-ND", "https://creativecommons.org/licenses/by-nd/4.0/"),
    "cc-by-nc-nd": ("CC BY-NC-ND", "https://creativecommons.org/licenses/by-nc-nd/4.0/"),
    "arr": ("All rights reserved", None),
}

# Mushroom Observer reports licenses by name.
_MO_LICENSES = {
    "creative commons wikipedia compatible v3.0": "cc-by-sa",
    "creative commons non-commercial v3.0": "cc-by-nc-sa",
    "creative commons non commercial v3.0": "cc-by-nc-sa",
    "public domain": "pd",
}


def normalize_inat(code: str | None) -> str:
    code = (code or "").strip().lower()
    if code == "cc-pdm":
        return "pd"
    return code if code in LICENSES else "arr"


def normalize_mo(name: str | None) -> str:
    text = re.sub(r"\s+", " ", (name or "").strip().lower())
    if text in _MO_LICENSES:
        return _MO_LICENSES[text]
    if text.startswith("creative commons") and "attribution" in text:
        parts = ["cc-by"]
        if "non" in text and "commercial" in text:
            parts.append("nc")
        if "no derivative" in text or "noderiv" in text:
            parts.append("nd")
        elif "share" in text:
            parts.append("sa")
        code = "-".join(parts)
        return code if code in LICENSES else "arr"
    return "arr"


def displayable(code: str) -> bool:
    return code in settings.display_licenses


def label(code: str) -> str:
    return LICENSES.get(code, (code.upper(), None))[0]


def deed_url(code: str) -> str | None:
    return LICENSES.get(code, (None, None))[1]
