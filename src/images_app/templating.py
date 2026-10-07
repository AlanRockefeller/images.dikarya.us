"""Jinja environment and small presentation helpers."""

from __future__ import annotations

import hashlib
import re
from functools import cache

from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from images_app import licenses
from images_app.config import PACKAGE_ROOT, settings
from images_app.models import Image, Taxon

templates = Jinja2Templates(directory=str(PACKAGE_ROOT / "templates"))

SOURCE_LABELS = {"inaturalist": "iNaturalist", "mushroom_observer": "Mushroom Observer"}
SOURCE_SHORT = {"inaturalist": "iNat", "mushroom_observer": "MO"}
INFRASPECIFIC = {"subspecies": "subsp.", "variety": "var.", "form": "f."}
# Ranks between genus and species are shown with a prefix, e.g. "sect. Amanita".
INFRAGENERIC = {"subgenus": "subg.", "section": "sect.", "subsection": "subsect.", "series": "ser."}


@cache
def static_url(filename: str) -> str:
    """URL with a content hash so browsers can cache static files indefinitely."""
    digest = hashlib.sha256((PACKAGE_ROOT / "static" / filename).read_bytes()).hexdigest()[:10]
    return f"/static/{filename}?v={digest}"


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:80] or "taxon"


def taxon_url(taxon: Taxon, suffix: str = "") -> str:
    return f"/taxon/{taxon.inat_taxon_id}/{slugify(taxon.name)}{suffix}"


def taxon_name(taxon: Taxon) -> Markup:
    """Scientific name as HTML, italicized from genus down, with infraspecific connectors."""
    name = escape(taxon.name)
    if taxon.rank_level > 20:
        return Markup(f'<span class="sci">{name}</span>')
    words = taxon.name.split()
    if taxon.rank in INFRASPECIFIC and len(words) == 3:
        connector = INFRASPECIFIC[taxon.rank]
        return Markup(
            f'<span class="sci"><i>{escape(" ".join(words[:2]))}</i> {connector} '
            f"<i>{escape(words[2])}</i></span>"
        )
    if taxon.rank in INFRAGENERIC:
        return Markup(f'<span class="sci">{INFRAGENERIC[taxon.rank]} <i>{name}</i></span>')
    if taxon.rank == "complex":
        return Markup(f'<span class="sci"><i>{name}</i> complex</span>')
    return Markup(f'<span class="sci"><i>{name}</i></span>')


def plain_name(taxon: Taxon) -> str:
    words = taxon.name.split()
    if taxon.rank in INFRASPECIFIC and len(words) == 3:
        return f"{words[0]} {words[1]} {INFRASPECIFIC[taxon.rank]} {words[2]}"
    if taxon.rank in INFRAGENERIC:
        return f"{INFRAGENERIC[taxon.rank]} {taxon.name}"
    if taxon.rank == "complex":
        return f"{taxon.name} complex"
    return taxon.name


def number(value: int | None) -> str:
    return f"{value or 0:,}"


def plural(count: int, singular: str, plural_form: str | None = None) -> str:
    word = singular if count == 1 else (plural_form or singular + "s")
    return f"{count:,} {word}"


def image_alt(image: Image, taxon: Taxon | None = None) -> str:
    subject = plain_name(taxon) if taxon else "Photo"
    return f"{subject}, photographed by {image.photographer}"


templates.env.globals.update(
    static_url=static_url,
    taxon_url=taxon_url,
    taxon_name=taxon_name,
    plain_name=plain_name,
    license_label=licenses.label,
    license_url=licenses.deed_url,
    source_label=SOURCE_LABELS.get,
    source_short=SOURCE_SHORT.get,
    image_alt=image_alt,
    site_name="Dikarya Images",
    base_url=settings.base_url,
)
templates.env.filters.update(number=number, plural=plural)
