"""Pure contracts for evidence-based outline continuity review."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from enum import Enum
import hashlib
import json
from typing import Any, Iterable, Mapping


class ContinuityDecision(str, Enum):
    PASS = "pass"
    REVIEW = "review"
    CONFLICT = "conflict"
    UNAVAILABLE = "unavailable"


class ContinuityReviewState(str, Enum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    PASS = "pass"
    ACKNOWLEDGED = "acknowledged"


class ContinuityIssueSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CONFLICT = "conflict"


def _json_safe(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return _json_safe(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    if isinstance(value, set):
        return [_json_safe(item) for item in sorted(value, key=repr)]
    return value


@dataclass(frozen=True)
class ContinuityIssue:
    id: str
    code: str
    severity: ContinuityIssueSeverity = ContinuityIssueSeverity.WARNING
    scope: Mapping[str, Any] = field(default_factory=dict)
    from_ref: str = ""
    to_ref: str = ""
    evidence_refs: tuple[str, ...] = ()
    message: str = ""
    suggestion: str = ""
    suggested_patch: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if isinstance(self.severity, str):
            object.__setattr__(self, "severity", ContinuityIssueSeverity(self.severity))
        object.__setattr__(self, "scope", dict(self.scope))
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        if self.suggested_patch is not None:
            object.__setattr__(self, "suggested_patch", dict(self.suggested_patch))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "code": self.code,
            "severity": self.severity.value,
            "scope": _json_safe(self.scope),
            "from_ref": self.from_ref,
            "to_ref": self.to_ref,
            "evidence_refs": list(self.evidence_refs),
            "message": self.message,
            "suggestion": self.suggestion,
            "suggested_patch": _json_safe(self.suggested_patch),
        }


@dataclass(frozen=True)
class ContinuityReviewReport:
    decision: ContinuityDecision
    confidence: float | None = None
    scope_fingerprint: str = ""
    issues: tuple[ContinuityIssue, ...] = ()
    suggestions: tuple[Mapping[str, Any], ...] = ()
    model: str = ""
    schema_version: str = ""
    ruleset_version: str = ""
    error: str = ""

    def __post_init__(self) -> None:
        if isinstance(self.decision, str):
            object.__setattr__(self, "decision", ContinuityDecision(self.decision))
        object.__setattr__(
            self,
            "issues",
            tuple(
                issue if isinstance(issue, ContinuityIssue) else ContinuityIssue(**issue)
                for issue in self.issues
            ),
        )
        object.__setattr__(
            self, "suggestions", tuple(dict(suggestion) for suggestion in self.suggestions)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.value,
            "confidence": self.confidence,
            "scope_fingerprint": self.scope_fingerprint,
            "issues": [issue.to_dict() for issue in self.issues],
            "suggestions": _json_safe(self.suggestions),
            "model": self.model,
            "schema_version": self.schema_version,
            "ruleset_version": self.ruleset_version,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, values: Mapping[str, Any]) -> "ContinuityReviewReport":
        return cls(
            decision=ContinuityDecision(str(values.get("decision") or "unavailable")),
            confidence=values.get("confidence"),
            scope_fingerprint=str(values.get("scope_fingerprint") or ""),
            issues=tuple(values.get("issues") or ()),
            suggestions=tuple(values.get("suggestions") or ()),
            model=str(values.get("model") or ""),
            schema_version=str(values.get("schema_version") or ""),
            ruleset_version=str(values.get("ruleset_version") or ""),
            error=str(values.get("error") or ""),
        )


@dataclass(frozen=True)
class ContinuityReviewScope:
    parent_logical_node_id: str
    level: str
    parent_version_id: str
    parent_version_digest: str
    children: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        level = self.level.value if isinstance(self.level, Enum) else str(self.level)
        object.__setattr__(self, "level", level)
        object.__setattr__(self, "children", tuple(dict(child) for child in self.children))

    def to_dict(self) -> dict[str, Any]:
        return {
            "parent_logical_node_id": self.parent_logical_node_id,
            "level": self.level,
            "parent_version_id": self.parent_version_id,
            "parent_version_digest": self.parent_version_digest,
            "children": _json_safe(self.children),
        }


def build_scope_fingerprint(
    scope: ContinuityReviewScope,
    *,
    ancestor_versions: Iterable[Mapping[str, Any]] = (),
    canonical_prefix_digest: str = "",
    context_digest: str = "",
    prompt_node_version_id: str = "",
    prompt_hash: str = "",
    schema_version: str = "",
    ruleset_version: str = "",
    model: str = "",
    context_metadata: Mapping[str, Any] | None = None,
    version_metadata: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> str:
    """Hash the exact ordered scope identity plus review-context versions."""

    payload = {
        "scope": scope.to_dict(),
        "ancestor_versions": _json_safe(tuple(ancestor_versions)),
        "canonical_prefix_digest": canonical_prefix_digest,
        "context_digest": context_digest,
        "prompt_node_version_id": prompt_node_version_id,
        "prompt_hash": prompt_hash,
        "schema_version": schema_version,
        "ruleset_version": ruleset_version,
        "model": model,
        "context_metadata": _json_safe(context_metadata or {}),
        "version_metadata": _json_safe(version_metadata or {}),
        "metadata": _json_safe(metadata or {}),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
