"""Recognizing ITS barcode evidence on source observations.

Only observations with ITS evidence are imported. On iNaturalist that means the
"DNA Barcode ITS" observation field holds a nucleotide sequence (FASTA headers
and line breaks are fine) or points to the sequenced observation, e.g. "see
https://www.inaturalist.org/observations/101058580". Placeholders such as
"pending" or "yes" do not count. On Mushroom Observer it means a sequence
record whose locus names ITS and that has bases or a repository accession.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

INAT_ITS_FIELD_ID = 2330
INAT_ITS_FIELD_NAME = "DNA Barcode ITS"
MIN_BASES = 50

# A whitespace-separated token made only of nucleotide letters and gap marks.
_NUCLEOTIDE_TOKEN = re.compile(r"[ACGTURYSWKMBDHVN\-.*]+", re.IGNORECASE)
_OBSERVATION_POINTER = re.compile(
    r"inaturalist\.org/observations/\d+|\bobs(?:ervation)?\.?\s*(?:#|no\.?|number)?\s*\d{5,}",
    re.IGNORECASE,
)
_ITS_LOCUS = re.compile(r"(?:^|[^a-z])(?:nr|g)?its(?:\d|[^a-z]|$)", re.IGNORECASE)


def sequence_length(value: str | None) -> int:
    """Bases in the longest nucleotide stretch of `value`; 0 if shorter than MIN_BASES.

    Consecutive nucleotide tokens are joined (sequences are often wrapped or
    split into blocks), position numbers are skipped, and any other word
    (FASTA headers, accession numbers, prose) ends a stretch.
    """
    best = current = 0
    for token in (value or "").split():
        if _NUCLEOTIDE_TOKEN.fullmatch(token):
            current += sum(char.isalpha() for char in token)
        elif not token.isdigit():
            best, current = max(best, current), 0
    best = max(best, current)
    return best if best >= MIN_BASES else 0


@dataclass(frozen=True)
class ItsEvidence:
    present: bool
    length: int | None  # None when the field points to another observation


def inat_its_evidence(observation: dict) -> ItsEvidence:
    best: ItsEvidence = ItsEvidence(False, None)
    for field in observation.get("ofvs") or []:
        if not isinstance(field, dict):
            continue
        if field.get("field_id") != INAT_ITS_FIELD_ID and field.get("name") != INAT_ITS_FIELD_NAME:
            continue
        value = str(field.get("value") or "")
        length = sequence_length(value)
        if length:
            if not best.length or length > best.length:
                best = ItsEvidence(True, length)
        elif not best.present and _OBSERVATION_POINTER.search(value):
            best = ItsEvidence(True, None)
    return best


def is_its_locus(locus: str | None) -> bool:
    return bool(locus and _ITS_LOCUS.search(locus))
