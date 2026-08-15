"""Deterministic proof checks for facts extracted from accepted prose."""
from __future__ import annotations

import re
from typing import Iterable


_NEGATION = r"(?:并没有|没有|没能|并未|从未|不曾|未曾|未|不(?:能|会|再|肯|愿|敢)?)"
_CLAUSE_BREAK = r"[^，。！？；：、,.!?;:\"'“”‘’（）()【】]{0,4}"


def _compact(value: object) -> str:
    return re.sub(r"[\s，。！？；：、,.!?;:\"'“”‘’（）()【】《》]", "", str(value or ""))


def _supports_term(evidence: str, term: str) -> bool:
    return term in evidence


def _is_negated(evidence: str, terms: Iterable[str]) -> bool:
    for term in terms:
        fragments = [term] if len(term) <= 4 else [term[index : index + 2] for index in range(len(term) - 1)]
        for fragment in fragments:
            if len(fragment) >= 2 and re.search(
                _NEGATION + _CLAUSE_BREAK + re.escape(fragment), evidence
            ):
                return True
    return False


def is_affirmative_final_text_evidence(
    content: object, evidence_text: object, terms: Iterable[object] = ()
) -> bool:
    """Require an accepted-prose quote to support, rather than merely mention, a claim."""
    if not isinstance(content, str) or not content.strip() or not isinstance(evidence_text, str):
        return False
    evidence = evidence_text.strip()
    if not evidence or evidence not in content:
        return False

    claims = [term for value in terms if len(term := _compact(value)) >= 2]
    compact_evidence = _compact(evidence)
    return (
        all(_supports_term(compact_evidence, term) for term in claims)
        and not _is_negated(evidence, claims)
    )
