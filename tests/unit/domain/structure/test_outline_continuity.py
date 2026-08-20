"""Contracts for persisted outline continuity review metadata."""

import json

from domain.structure.outline_continuity import (
    ContinuityDecision,
    ContinuityIssue,
    ContinuityIssueSeverity,
    ContinuityReviewReport,
    ContinuityReviewScope,
    ContinuityReviewState,
    build_scope_fingerprint,
)
from domain.structure.outline_plan import OutlinePlanRevision, PlanRevisionStatus


def _scope(*, child_digest: str = "child-digest") -> ContinuityReviewScope:
    return ContinuityReviewScope(
        parent_logical_node_id="part-1",
        level="volume",
        parent_version_id="version-part-1",
        parent_version_digest="parent-digest",
        children=(
            {
                "logical_node_id": "volume-1",
                "version_id": "version-volume-1",
                "version_digest": child_digest,
            },
            {
                "logical_node_id": "volume-2",
                "version_id": "version-volume-2",
                "version_digest": "second-child-digest",
            },
        ),
    )


def test_scope_fingerprint_is_stable_and_binds_ordered_scope_and_metadata():
    scope = _scope()
    kwargs = {
        "ancestor_versions": (
            {
                "logical_node_id": "outline-1",
                "version_digest": "outline-digest",
            },
        ),
        "canonical_prefix_digest": "canonical-digest",
        "context_digest": "context-digest",
        "prompt_node_version_id": "prompt-v1",
        "prompt_hash": "prompt-hash",
        "schema_version": "schema-v1",
        "ruleset_version": "rules-v1",
        "model": "model-a",
        "context_metadata": {"memory": ["m-1"], "bible": "b-1"},
    }

    expected = build_scope_fingerprint(scope, **kwargs)

    assert build_scope_fingerprint(scope, **kwargs) == expected
    assert build_scope_fingerprint(_scope(child_digest="new-child-digest"), **kwargs) != expected
    assert build_scope_fingerprint(
        scope,
        **{**kwargs, "context_metadata": {"bible": "b-1", "memory": ["m-1"]}},
    ) == expected
    reversed_scope = ContinuityReviewScope(
        parent_logical_node_id=scope.parent_logical_node_id,
        level=scope.level,
        parent_version_id=scope.parent_version_id,
        parent_version_digest=scope.parent_version_digest,
        children=tuple(reversed(scope.children)),
    )
    assert build_scope_fingerprint(reversed_scope, **kwargs) != expected


def test_continuity_types_produce_json_safe_dictionaries_and_plan_defaults():
    issue = ContinuityIssue(
        id="issue-1",
        code="handoff.missing",
        severity=ContinuityIssueSeverity.WARNING,
        scope={"node": "volume-1"},
        evidence_refs=("outline:part-1:parent-digest",),
        message="The handoff needs review.",
        suggested_patch={"creative_goal": "Bridge the transition."},
    )
    report = ContinuityReviewReport(
        decision=ContinuityDecision.REVIEW,
        confidence=0.72,
        scope_fingerprint="fingerprint-1",
        issues=(issue,),
        suggestions=({"id": "suggestion-1"},),
        model="model-a",
        schema_version="schema-v1",
        ruleset_version="rules-v1",
    )
    plan = OutlinePlanRevision(
        id="plan-1",
        novel_id="novel-1",
        revision=1,
        status=PlanRevisionStatus.DRAFT,
        digest="plan-digest",
        canonical_prefix_digest="canonical-digest",
        items=(),
    )

    assert json.loads(json.dumps(report.to_dict())) == {
        "decision": "review",
        "confidence": 0.72,
        "scope_fingerprint": "fingerprint-1",
        "issues": [
            {
                "id": "issue-1",
                "code": "handoff.missing",
                "severity": "warning",
                "scope": {"node": "volume-1"},
                "from_ref": "",
                "to_ref": "",
                "evidence_refs": ["outline:part-1:parent-digest"],
                "message": "The handoff needs review.",
                "suggestion": "",
                "suggested_patch": {"creative_goal": "Bridge the transition."},
            }
        ],
        "suggestions": [{"id": "suggestion-1"}],
        "model": "model-a",
        "schema_version": "schema-v1",
        "ruleset_version": "rules-v1",
        "error": "",
    }
    assert plan.narrative_review_state is ContinuityReviewState.NOT_REQUIRED
    assert plan.narrative_review_receipt == {}
