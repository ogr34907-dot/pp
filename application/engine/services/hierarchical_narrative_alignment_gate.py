"""Hierarchy-aware narrative contract checks.

This module is deliberately an application-layer orchestrator.  It does not
persist contracts or maintain a second memory/vector store; callers provide
the existing story nodes and evidence sources.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Awaitable, Callable, Iterable, Mapping, Optional


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
        return {
            "novel_id": self.novel_id,
            "chapter_id": self.chapter_id,
            "chapter_number": self.chapter_number,
            "ancestry": {
                key: dict(value) if value is not None else None
                for key, value in self.ancestry.items()
            },
            "digest": self.digest,
            "memory_state": dict(self.memory_state),
            "vector_evidence": [dict(item) for item in self.vector_evidence],
            "evidence_degraded": self.evidence_degraded,
        }


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
        data = asdict(self)
        data["violations"] = [asdict(item) for item in self.violations]
        data["character_agency"] = [asdict(item) for item in self.character_agency]
        return data


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
            "chapter_start": cls._node_value(node, "chapter_start"),
            "chapter_end": cls._node_value(node, "chapter_end"),
            "description": cls._node_value(node, "description") or summary,
            "themes": cls._node_value(node, "themes", None) or metadata.get("themes", []),
            "key_events": cls._node_value(node, "key_events", None) or metadata.get("key_events", []),
            "narrative_arc": cls._node_value(node, "narrative_arc", None) or metadata.get("narrative_arc", ""),
            "conflicts": cls._node_value(node, "conflicts", None) or metadata.get("conflicts", []),
            "metadata": metadata,
            "contract_digest": metadata.get("contract_digest") or metadata.get("contract_hash") or metadata.get("plan_digest"),
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
        expected_parent = {"chapter": "act", "act": "volume", "volume": "part"}
        for level in self._EXPECTED:
            if current is None:
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
            vectors = []
        else:
            vectors = list(vector_evidence)
        try:
            serial = {"novel_id": novel, "chapter_id": chapter_id, "ancestry": ancestry}
            digest = hashlib.sha256(json.dumps(serial, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()
        except Exception:
            digest = hashlib.sha256(str(serial).encode()).hexdigest()
        return HierarchySnapshot(novel, str(chapter_id), chapter_number, ancestry, digest, mem, tuple(vectors), degraded, tuple(structural))

    @staticmethod
    def _candidate_digest(candidate: Mapping[str, Any]) -> str:
        payload = json.dumps(candidate, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
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
        if known:
            for ref in references:
                if str(ref) not in known:
                    violations.append(AlignmentViolation("chapter", "blocking", "known candidate reference", str(ref), (snapshot.chapter_id,)))

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
        return AlignmentReport(decision, 1.0 if decision == "pass" else 0.0, self._candidate_digest(candidate), snapshot.digest, tuple(served), tuple(missing), tuple(violations), tuple(agency), evidence_degraded=snapshot.evidence_degraded)

    async def evaluate(self, snapshot: HierarchySnapshot, candidate: Mapping[str, Any]) -> AlignmentReport:
        report = self.check(snapshot, candidate)
        if report.decision == "block" or self.llm_evaluator is None:
            return report
        try:
            result = self.llm_evaluator(snapshot, candidate)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            violation = AlignmentViolation("chapter", "warning", "LLM alignment evidence", str(exc), (snapshot.chapter_id,))
            return replace(report, decision="review", confidence=0.0, violations=report.violations + (violation,))
        if not isinstance(result, Mapping):
            return replace(report, decision="review", confidence=0.0)
        llm_decision = str(result.get("decision", "review")).lower()
        confidence = float(result.get("confidence", 0.0) or 0.0)
        if llm_decision in {"block", "review"} or confidence < self.min_confidence:
            return replace(report, decision="review" if llm_decision != "block" else "block", confidence=confidence)
        return replace(report, confidence=confidence)

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
