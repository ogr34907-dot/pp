"""Shared chapter hierarchy-gate invocation for prose entry points.

The guard is intentionally stateless apart from a per-digest cache.  StoryNode
and MemoryEngine remain the sources of truth; this module only assembles the
candidate and evidence passed to the injected alignment gate.
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from application.engine.services.hierarchical_narrative_alignment_gate import (
    AlignmentReport,
)


@dataclass
class NarrativeAlignmentGateError(RuntimeError):
    """Raised when a chapter candidate cannot enter prose generation."""

    report: AlignmentReport

    def __str__(self) -> str:
        return f"narrative_alignment_{self.report.decision}"


def _node_value(node: Any, key: str, default: Any = None) -> Any:
    if isinstance(node, Mapping):
        return node.get(key, default)
    return getattr(node, key, default)


def candidate_from_outline(outline: str, chapter_node: Any = None) -> dict[str, Any]:
    """Build a structured candidate while retaining legacy outline text."""
    candidate: dict[str, Any] = {"outline": outline}
    metadata = _node_value(chapter_node, "metadata", {}) or {}
    if isinstance(metadata, Mapping):
        candidate.update(dict(metadata))
    for key in (
        "contract_digests", "plan_contract_digests", "serves_part_commitments",
        "serves_volume_commitments", "serves_act_commitments", "references",
        "character_agency", "key_plot_points", "chapter_characters",
    ):
        value = _node_value(chapter_node, key, None)
        if value is not None:
            candidate[key] = value
    return candidate


async def _nodes_for_repo(repo: Any, novel_id: str) -> tuple[list[Any], str | None]:
    if repo is None:
        return [], None
    errors: list[str] = []
    for name in ("get_by_novel_sync", "get_tree_sync", "get_by_novel", "get_tree"):
        operation = getattr(repo, name, None)
        if not callable(operation):
            continue
        try:
            result = operation(novel_id)
            if inspect.isawaitable(result):
                result = await result
            if hasattr(result, "nodes"):
                result = result.nodes
            return list(result or []), None
        except Exception as exc:
            errors.append(str(exc))
            continue
    return [], "; ".join(errors) if errors else "story node repository unavailable"


async def evaluate_chapter_candidate(
    gate: Any,
    *,
    story_node_repo: Any,
    novel_id: str,
    chapter_number: int,
    candidate: Mapping[str, Any],
    chapter_node: Any = None,
    memory_engine: Any = None,
    context_evidence: Any = None,
) -> Optional[AlignmentReport]:
    """Evaluate one candidate; return ``None`` when no gate was injected."""
    if gate is None:
        return None
    cache = getattr(gate, "_chapter_gate_cache", None)
    if cache is None:
        cache = {}
        try:
            setattr(gate, "_chapter_gate_cache", cache)
        except Exception:
            cache = None
    candidate_digest = None
    cache_key = None
    digest_fn = getattr(gate, "_candidate_digest", None)
    if callable(digest_fn):
        try:
            candidate_digest = str(digest_fn(candidate))
            cache_key = (str(novel_id), int(chapter_number), candidate_digest)
            if cache is not None and cache_key in cache:
                return cache[cache_key]
        except Exception:
            candidate_digest = None
    nodes, node_error = await _nodes_for_repo(story_node_repo, novel_id)
    chapter_id = str(_node_value(chapter_node, "id", "") or "")
    if not chapter_id:
        for node in nodes:
            if str(_node_value(node, "node_type", "")).lower().endswith("chapter") and int(_node_value(node, "number", 0) or 0) == int(chapter_number):
                chapter_node = node
                chapter_id = str(_node_value(node, "id", ""))
                break
    if not chapter_id:
        chapter_id = f"chapter-{novel_id}-{chapter_number}"

    memory_state = None
    if memory_engine is not None:
        try:
            summary = memory_engine.get_state_summary(novel_id)
            if inspect.isawaitable(summary):
                summary = await summary
            if isinstance(summary, Mapping):
                memory_state = dict(summary)
        except Exception as exc:
            memory_state = {"unavailable": True, "error": str(exc)}

    # Existing context/vector evidence is advisory input, never a replacement
    # for StoryNode or MemoryEngine authority.
    if context_evidence is not None and isinstance(candidate, dict):
        candidate = dict(candidate)
        candidate.setdefault("context_evidence", context_evidence)
    vector_evidence = {"degraded": True, "error": node_error} if node_error else None
    snapshot = gate.build_snapshot(
        chapter_id,
        nodes,
        novel_id=novel_id,
        memory_state=memory_state,
        vector_evidence=vector_evidence,
    )
    evaluator = getattr(gate, "evaluate", None) or getattr(gate, "acheck", None)
    if evaluator is None:
        evaluator = getattr(gate, "check_alignment")
    report = evaluator(snapshot, candidate)
    if inspect.isawaitable(report):
        report = await report
    if cache is not None and cache_key:
        cache[cache_key] = report
    return report


async def enforce_chapter_candidate(*args: Any, **kwargs: Any) -> Optional[AlignmentReport]:
    report = await evaluate_chapter_candidate(*args, **kwargs)
    if report is not None and str(report.decision).lower() in {"block", "review"}:
        raise NarrativeAlignmentGateError(report)
    return report


__all__ = [
    "NarrativeAlignmentGateError",
    "candidate_from_outline",
    "evaluate_chapter_candidate",
    "enforce_chapter_candidate",
]
