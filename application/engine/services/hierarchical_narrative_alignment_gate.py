"""Hierarchy-aware narrative contract checks.

This module is deliberately an application-layer orchestrator.  It does not
persist contracts or maintain a second memory/vector store; callers provide
the existing story nodes and evidence sources.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import asyncio
import math
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Awaitable, Callable, Iterable, Mapping, Optional


def _json_safe(value: Any, _seen: Optional[set[int]] = None) -> Any:
    """Convert arbitrary application values into JSON-compatible values."""
    seen = _seen if _seen is not None else set()
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, Mapping):
        marker = id(value)
        if marker in seen:
            return "<cycle>"
        seen.add(marker)
        try:
            return {str(_json_safe(key, seen)): _json_safe(item, seen) for key, item in value.items()}
        finally:
            seen.discard(marker)
    if isinstance(value, (list, tuple, set, frozenset)):
        marker = id(value)
        if marker in seen:
            return "<cycle>"
        seen.add(marker)
        try:
            return [_json_safe(item, seen) for item in value]
        finally:
            seen.discard(marker)
    if hasattr(value, "value") and not isinstance(value, type):
        return _json_safe(getattr(value, "value"), seen)
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    marker = id(value)
    if marker in seen:
        return "<cycle>"
    seen.add(marker)
    try:
        if hasattr(value, "__dict__"):
            return {str(key): _json_safe(item, seen) for key, item in vars(value).items()}
        return str(value)
    finally:
        seen.discard(marker)


@dataclass(frozen=True)
class OneShotOverride:
    candidate_digest: str
    reason: str


@dataclass(frozen=True)
class AlignmentViolation:
    scope: str
    severity: str
    expected: str
    actual: str = ""
    evidence_refs: tuple[str, ...] = ()
    repair: str = ""


@dataclass(frozen=True)
class CharacterAgency:
    character: str
    goal: str = ""
    choice: str = ""
    opposition: str = ""
    cost: str = ""
    state_delta: str = ""


@dataclass(frozen=True)
class HierarchySnapshot:
    novel_id: str
    chapter_id: str
    chapter_number: int
    ancestry: Mapping[str, Optional[Mapping[str, Any]]]
    digest: str
    memory_state: Mapping[str, Any] = field(default_factory=dict)
    vector_evidence: tuple[Mapping[str, Any], ...] = ()
    evidence_degraded: bool = False
    structural_violations: tuple[AlignmentViolation, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return _json_safe({
            "novel_id": self.novel_id,
            "chapter_id": self.chapter_id,
            "chapter_number": self.chapter_number,
            "ancestry": {
                key: dict(value) if value is not None else None
                for key, value in self.ancestry.items()
            },
            "digest": self.digest,
            "memory_state": dict(self.memory_state),
            "vector_evidence": list(self.vector_evidence),
            "evidence_degraded": self.evidence_degraded,
            "structural_violations": [asdict(item) for item in self.structural_violations],
        })


@dataclass(frozen=True)
class AlignmentReport:
    decision: str
    confidence: float
    candidate_digest: str
    snapshot_digest: str
    served_commitments: tuple[str, ...] = ()
    missing_commitments: tuple[str, ...] = ()
    violations: tuple[AlignmentViolation, ...] = ()
    character_agency: tuple[CharacterAgency, ...] = ()
    repair_plan: tuple[str, ...] = ()
    evidence_degraded: bool = False
    overridden: bool = False
    override_reason: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(asdict(self))


class HierarchicalNarrativeAlignmentGate:
    """Build and evaluate a chapter's part/volume/act contract chain."""

    _EXPECTED = ("chapter", "act", "volume", "part")

    def __init__(
        self,
        *,
        llm_evaluator: Optional[Callable[..., Any]] = None,
        memory_engine: Any = None,
        vector_retriever: Any = None,
        min_confidence: float = 0.6,
    ) -> None:
        self.llm_evaluator = llm_evaluator
        self.memory_engine = memory_engine
        self.vector_retriever = vector_retriever
        self.min_confidence = min_confidence
        self._consumed_overrides: set[str] = set()

    @staticmethod
    def _node_value(node: Any, key: str, default: Any = None) -> Any:
        if isinstance(node, Mapping):
            return node.get(key, default)
        return getattr(node, key, default)

    @classmethod
    def _node_type(cls, node: Any) -> str:
        value = cls._node_value(node, "node_type", "")
        return getattr(value, "value", value)

    @classmethod
    def derive_contract_digest(cls, node: Any) -> str:
        """Return a stable contract digest for legacy nodes that predate the metadata field."""
        metadata = cls._node_value(node, "metadata", {}) or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except (TypeError, ValueError):
                metadata = {}
        if not isinstance(metadata, Mapping):
            metadata = {}
        stored = (
            metadata.get("contract_digest")
            or metadata.get("contract_hash")
            or metadata.get("plan_digest")
        )
        if stored:
            return str(stored)

        contract_metadata = {
            key: metadata[key]
            for key in (
                "narrative_goal",
                "plot_points",
                "key_characters",
                "key_locations",
                "emotional_arc",
                "setup_for",
                "payoff_from",
                "required_threads",
                "out_of_scope",
                "character_agency",
                "handoff_from_previous",
                "handoff_to_next",
            )
            if key in metadata
        }
        payload = {
            "node_type": cls._node_type(node),
            "number": cls._node_value(node, "number"),
            "title": cls._node_value(node, "title", ""),
            "parent_id": cls._node_value(node, "parent_id"),
            "chapter_start": cls._node_value(node, "chapter_start"),
            "chapter_end": cls._node_value(node, "chapter_end"),
            "chapter_count": cls._node_value(node, "chapter_count", 0),
            "suggested_chapter_count": cls._node_value(node, "suggested_chapter_count"),
            "description": cls._node_value(node, "description", ""),
            "outline": cls._node_value(node, "outline", ""),
            "themes": cls._node_value(node, "themes", None) or metadata.get("themes", []),
            "key_events": cls._node_value(node, "key_events", None) or metadata.get("key_events", []),
            "narrative_arc": cls._node_value(node, "narrative_arc", None) or metadata.get("narrative_arc", ""),
            "conflicts": cls._node_value(node, "conflicts", None) or metadata.get("conflicts", []),
            "metadata": contract_metadata,
        }
        encoded = json.dumps(
            _json_safe(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @classmethod
    def _node_record(cls, node: Any) -> dict[str, Any]:
        metadata = cls._node_value(node, "metadata", {}) or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except (TypeError, ValueError):
                metadata = {}
        committed = metadata.get("committed_metadata", {}) or {}
        if isinstance(committed, str):
            try:
                committed = json.loads(committed)
            except (TypeError, ValueError):
                committed = {}
        summary = metadata.get("summary") or committed.get("summary", "")
        record = {
            "id": cls._node_value(node, "id"),
            "novel_id": cls._node_value(node, "novel_id"),
            "node_type": cls._node_type(node),
            "number": cls._node_value(node, "number"),
            "title": cls._node_value(node, "title", ""),
            "parent_id": cls._node_value(node, "parent_id"),
            "order_index": cls._node_value(node, "order_index"),
            "planning_status": getattr(cls._node_value(node, "planning_status", "draft"), "value", cls._node_value(node, "planning_status", "draft")),
            "planning_source": getattr(cls._node_value(node, "planning_source", "manual"), "value", cls._node_value(node, "planning_source", "manual")),
            "chapter_start": cls._node_value(node, "chapter_start"),
            "chapter_end": cls._node_value(node, "chapter_end"),
            "chapter_count": cls._node_value(node, "chapter_count", 0),
            "suggested_chapter_count": cls._node_value(node, "suggested_chapter_count"),
            "description": cls._node_value(node, "description") or summary,
            "content": cls._node_value(node, "content"),
            "word_count": cls._node_value(node, "word_count", 0),
            "status": cls._node_value(node, "status", "draft"),
            "pov_character_id": cls._node_value(node, "pov_character_id"),
            "timeline_start": cls._node_value(node, "timeline_start"),
            "timeline_end": cls._node_value(node, "timeline_end"),
            "created_at": cls._node_value(node, "created_at"),
            "updated_at": cls._node_value(node, "updated_at"),
            "themes": cls._node_value(node, "themes", None) or metadata.get("themes", []),
            "key_events": cls._node_value(node, "key_events", None) or metadata.get("key_events", []),
            "narrative_arc": cls._node_value(node, "narrative_arc", None) or metadata.get("narrative_arc", ""),
            "conflicts": cls._node_value(node, "conflicts", None) or metadata.get("conflicts", []),
            "metadata": metadata,
            "contract_digest": cls.derive_contract_digest(node),
        }
        # Keep all existing planning fields available to consumers without
        # coupling this gate to a particular schema revision.
        for key, value in metadata.items():
            record.setdefault(key, value)
        for key in (
            "outline", "narrative_goal", "plot_points", "key_characters", "key_locations",
            "emotional_arc", "setup_for", "payoff_from", "required_threads", "out_of_scope",
            "character_agency", "handoff_from_previous", "handoff_to_next",
        ):
            value = cls._node_value(node, key, None)
            if value is not None:
                record[key] = value
        return record

    @staticmethod
    def _repair_plan(violations: Iterable[AlignmentViolation]) -> tuple[str, ...]:
        repairs: list[str] = []
        for violation in violations:
            repair = str(violation.repair or f"{violation.scope}: satisfy {violation.expected}")
            if repair and repair not in repairs:
                repairs.append(repair)
        return tuple(repairs)

    @classmethod
    def _report(cls, *, violations: Iterable[AlignmentViolation], **kwargs: Any) -> AlignmentReport:
        violations_tuple = tuple(violations)
        kwargs.setdefault("repair_plan", cls._repair_plan(violations_tuple))
        return AlignmentReport(violations=violations_tuple, **kwargs)

    def _retrieve_vectors(self, novel_id: str, chapter_id: str) -> tuple[list[Any], bool]:
        retriever = self.vector_retriever
        if retriever is None:
            return [], False
        operation = retriever
        for name in ("retrieve", "search", "get_evidence"):
            if hasattr(retriever, name):
                operation = getattr(retriever, name)
                break
        if inspect.iscoroutinefunction(operation):
            return [{"degraded": True, "error": "async vector retriever requires async evaluation"}], True
        try:
            try:
                result = operation(novel_id, chapter_id)
            except TypeError:
                result = operation(chapter_id)
            if inspect.isawaitable(result):
                try:
                    result = asyncio.run(result)
                except RuntimeError:
                    if hasattr(result, "close"):
                        result.close()
                    return [{"degraded": True, "error": "async vector retriever requires async evaluation"}], True
            if isinstance(result, Mapping):
                degraded = bool(result.get("degraded") or result.get("error"))
                items = result.get("items") or result.get("evidence") or []
                if degraded and not items:
                    return [dict(result)], True
                result = items
                return list(result), degraded
            return list(result or []), False
        except Exception as exc:
            return [{"degraded": True, "error": str(exc)}], True

    def build_snapshot(
        self,
        chapter_id: str,
        nodes: Iterable[Any],
        *,
        memory_state: Optional[Mapping[str, Any]] = None,
        vector_evidence: Any = None,
        novel_id: Optional[str] = None,
    ) -> HierarchySnapshot:
        records = {str(self._node_value(n, "id")): self._node_record(n) for n in nodes}
        chapter = records.get(str(chapter_id))
        structural: list[AlignmentViolation] = []
        if chapter is None:
            structural.append(AlignmentViolation("chapter", "blocking", "existing chapter node", str(chapter_id)))
            chapter = {"id": chapter_id, "novel_id": novel_id or "", "node_type": "chapter", "number": 0, "parent_id": None}

        ancestry: dict[str, Optional[Mapping[str, Any]]] = {name: None for name in self._EXPECTED}
        current: Optional[Mapping[str, Any]] = chapter
        chapter_novel_id = str(chapter.get("novel_id") or novel_id or "")
        expected_parent = {"chapter": "act", "act": "volume", "volume": "part"}
        for level in self._EXPECTED:
            if current is None:
                break
            current_novel_id = str(current.get("novel_id") or "")
            if chapter_novel_id and current_novel_id and current_novel_id != chapter_novel_id:
                structural.append(
                    AlignmentViolation(
                        level,
                        "blocking",
                        f"novel {chapter_novel_id}",
                        current_novel_id,
                        (str(current.get("id", "")),),
                        "将祖先节点限制在当前小说内",
                    )
                )
                break
            current_type = str(current.get("node_type", ""))
            if current_type != level:
                structural.append(AlignmentViolation(level, "blocking", level, current_type, (str(current.get("id", "")),)))
                break
            ancestry[level] = current
            if level == "part":
                if current.get("parent_id") is not None:
                    structural.append(AlignmentViolation("part", "blocking", "root part", str(current.get("parent_id"))))
                break
            parent_id = current.get("parent_id")
            current = records.get(str(parent_id)) if parent_id is not None else None
            if current is None:
                structural.append(AlignmentViolation(expected_parent[level], "blocking", f"parent of {level}", str(parent_id)))

        chapter_number = int(chapter.get("number") or 0)
        novel = novel_id or str(chapter.get("novel_id") or "")
        for level in ("part", "volume", "act"):
            ancestor = ancestry[level]
            if not ancestor:
                continue
            start, end = ancestor.get("chapter_start"), ancestor.get("chapter_end")
            if start is not None and chapter_number < int(start) or end is not None and chapter_number > int(end):
                structural.append(AlignmentViolation(level, "blocking", f"chapter in {start}-{end}", str(chapter_number), (str(ancestor.get("id")),)))

        mem = dict(memory_state or {})
        if not mem and self.memory_engine is not None:
            try:
                summary = self.memory_engine.get_state_summary(novel)
                if isinstance(summary, Mapping):
                    mem = dict(summary)
            except Exception as exc:  # evidence remains explicit to caller
                mem = {"error": str(exc), "unavailable": True}

        degraded = False
        if isinstance(vector_evidence, Mapping):
            degraded = bool(vector_evidence.get("degraded") or vector_evidence.get("error"))
            vectors = (vector_evidence.get("items") or vector_evidence.get("evidence") or [])
        elif vector_evidence is None:
            vectors, degraded = self._retrieve_vectors(novel_id or str(chapter.get("novel_id") or ""), str(chapter_id))
        else:
            vectors = list(vector_evidence)
        try:
            serial = _json_safe({"novel_id": novel, "chapter_id": chapter_id, "ancestry": ancestry})
            digest = hashlib.sha256(json.dumps(serial, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        except Exception:
            digest = hashlib.sha256(str(serial).encode()).hexdigest()
        return HierarchySnapshot(novel, str(chapter_id), chapter_number, ancestry, digest, mem, tuple(vectors), degraded, tuple(structural))

    @staticmethod
    def _candidate_digest(candidate: Mapping[str, Any]) -> str:
        payload = json.dumps(_json_safe(candidate), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _as_list(value: Any) -> list[Any]:
        if value is None:
            return []
        if isinstance(value, (list, tuple, set)):
            return list(value)
        return [value]

    def check(self, snapshot: HierarchySnapshot, candidate: Mapping[str, Any]) -> AlignmentReport:
        violations = list(snapshot.structural_violations)
        served: list[str] = []
        missing: list[str] = []
        contracts = candidate.get("contract_digests") or candidate.get("plan_contract_digests") or {}
        for level in ("chapter", "act", "volume"):
            ancestor = snapshot.ancestry.get(level)
            expected = (ancestor or {}).get("contract_digest") if ancestor else None
            if ancestor and not expected:
                ancestor_metadata = ancestor.get("metadata", {}) or {}
                expected = ancestor_metadata.get("contract_digest") or ancestor_metadata.get("contract_hash") or ancestor_metadata.get("plan_digest")
            actual = contracts.get(level) if isinstance(contracts, Mapping) else None
            if not expected or not actual or str(actual) != str(expected):
                violations.append(AlignmentViolation(level, "blocking", str(expected or "contract digest"), str(actual or "missing"), (str((ancestor or {}).get("id", "")),)))

        for level in ("part", "volume", "act"):
            ancestor = snapshot.ancestry.get(level) or {}
            commitments = []
            commitments.extend(self._as_list(ancestor.get("metadata", {}).get("narrative_goal")))
            commitments.extend(self._as_list(ancestor.get("key_events")))
            candidate_values = self._as_list(candidate.get(f"serves_{level}_commitments"))
            for commitment in commitments:
                if not commitment:
                    continue
                if any(str(commitment) == str(value) or str(commitment) in str(value) for value in candidate_values):
                    served.append(str(commitment))
                else:
                    missing.append(str(commitment))
                    violations.append(AlignmentViolation(level, "blocking", str(commitment), ", ".join(map(str, candidate_values)), (str(ancestor.get("id", "")),), "补充该层级承诺的具体服务动作"))

        references = self._as_list(candidate.get("references") or candidate.get("candidate_references"))
        known: set[str] = set()
        for ancestor in snapshot.ancestry.values():
            if ancestor:
                meta = ancestor.get("metadata", {})
                for key in ("known_references", "key_characters", "key_locations", "storylines"):
                    known.update(map(str, self._as_list(meta.get(key))))
        if references:
            for ref in references:
                if str(ref) not in known:
                    violations.append(AlignmentViolation("chapter", "blocking", "known candidate reference", str(ref), (snapshot.chapter_id,), "将候选引用绑定到已知角色、地点或故事线"))

        active = self._as_list((snapshot.ancestry.get("chapter") or {}).get("metadata", {}).get("key_characters"))
        agency_input = self._as_list(candidate.get("character_agency"))
        agency: list[CharacterAgency] = []
        by_character = {}
        for item in agency_input:
            if isinstance(item, Mapping):
                record = CharacterAgency(str(item.get("character") or item.get("character_id") or ""), str(item.get("goal") or ""), str(item.get("choice") or ""), str(item.get("opposition") or ""), str(item.get("cost") or ""), str(item.get("state_delta") or ""))
                agency.append(record)
                by_character[record.character] = record
        for character in active:
            name = str(character)
            item = by_character.get(name)
            if item is None or not item.goal or not item.choice or not item.cost:
                violations.append(AlignmentViolation("character", "blocking", f"{name}: goal, choice, cost", str(item or "missing"), (snapshot.chapter_id,), "为该角色补充主动目标、选择和代价"))

        decision = "block" if any(v.severity == "blocking" for v in violations) else "pass"
        if snapshot.evidence_degraded and decision == "pass":
            decision = "review"
        return self._report(violations=violations, decision=decision, confidence=1.0 if decision == "pass" else 0.0, candidate_digest=self._candidate_digest(candidate), snapshot_digest=snapshot.digest, served_commitments=tuple(served), missing_commitments=tuple(missing), character_agency=tuple(agency), evidence_degraded=snapshot.evidence_degraded)

    async def evaluate(self, snapshot: HierarchySnapshot, candidate: Mapping[str, Any]) -> AlignmentReport:
        retry_async_vectors = any(
            isinstance(item, Mapping)
            and item.get("error") == "async vector retriever requires async evaluation"
            for item in snapshot.vector_evidence
        )
        if (not snapshot.vector_evidence or retry_async_vectors) and self.vector_retriever is not None:
            vectors, degraded = await self._retrieve_vectors_async(snapshot.novel_id, snapshot.chapter_id)
            evidence_degraded = degraded if retry_async_vectors else snapshot.evidence_degraded or degraded
            snapshot = replace(snapshot, vector_evidence=tuple(vectors), evidence_degraded=evidence_degraded)
        report = self.check(snapshot, candidate)
        if report.decision == "block" or self.llm_evaluator is None:
            return report
        try:
            result = self.llm_evaluator(snapshot, candidate)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            violation = AlignmentViolation("chapter", "warning", "LLM alignment evidence", str(exc), (snapshot.chapter_id,))
            violations = report.violations + (violation,)
            return replace(report, decision="review", confidence=0.0, violations=violations, repair_plan=self._repair_plan(violations))
        if not isinstance(result, Mapping):
            return replace(report, decision="review", confidence=0.0)
        llm_decision = str(result.get("decision", "review")).lower().strip()
        try:
            confidence = float(result.get("confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        if not math.isfinite(confidence):
            confidence = 0.0
        if llm_decision not in {"pass", "review", "block"}:
            return replace(report, decision="review", confidence=confidence)
        if llm_decision in {"block", "review"} or confidence < self.min_confidence:
            return replace(report, decision="review" if llm_decision != "block" else "block", confidence=confidence)
        return replace(report, confidence=confidence)

    async def _retrieve_vectors_async(self, novel_id: str, chapter_id: str) -> tuple[list[Any], bool]:
        retriever = self.vector_retriever
        operation = retriever
        for name in ("retrieve", "search", "get_evidence"):
            if hasattr(retriever, name):
                operation = getattr(retriever, name)
                break
        try:
            try:
                result = operation(novel_id, chapter_id)
            except TypeError:
                result = operation(chapter_id)
            if inspect.isawaitable(result):
                result = await result
            if isinstance(result, Mapping):
                degraded = bool(result.get("degraded") or result.get("error"))
                items = result.get("items") or result.get("evidence") or []
                if degraded and not items:
                    return [dict(result)], True
                result = items
                return list(result), degraded
            return list(result or []), False
        except Exception as exc:
            return [{"degraded": True, "error": str(exc)}], True

    async def acheck(self, snapshot: HierarchySnapshot, candidate: Mapping[str, Any]) -> AlignmentReport:
        return await self.evaluate(snapshot, candidate)

    # Explicit aliases make the seam easy to discover from existing services
    # that use either ``build_hierarchy_snapshot`` or ``check_alignment``.
    build_hierarchy_snapshot = build_snapshot
    check_alignment = check

    def apply_override(self, report: AlignmentReport, override: OneShotOverride) -> AlignmentReport:
        if not override.candidate_digest or not str(override.reason or "").strip():
            raise ValueError("override candidate_digest and non-empty reason are required")
        if report.candidate_digest != override.candidate_digest:
            raise ValueError("override candidate digest does not match report")
        if report.candidate_digest in self._consumed_overrides or report.overridden:
            raise ValueError("override has already been consumed")
        self._consumed_overrides.add(report.candidate_digest)
        return replace(report, decision="pass", overridden=True, override_reason=str(override.reason).strip())


__all__ = [
    "AlignmentReport",
    "AlignmentViolation",
    "CharacterAgency",
    "HierarchySnapshot",
    "HierarchicalNarrativeAlignmentGate",
    "OneShotOverride",
]
