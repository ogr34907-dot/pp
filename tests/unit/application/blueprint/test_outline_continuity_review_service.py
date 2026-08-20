from __future__ import annotations

from dataclasses import replace
import json
import sqlite3
from types import SimpleNamespace

import pytest

from application.blueprint.services.outline_continuity_context_assembler import (
    OutlineContinuityContextAssembler,
)
from application.blueprint.services.outline_continuity_review_service import (
    OutlineContinuityReviewService,
)
from domain.ai.value_objects.prompt import Prompt
from domain.structure.outline_contract import OutlineLevel
from domain.structure.outline_plan import (
    OutlinePlanItem,
    OutlinePlanRevision,
    PlanRevisionStatus,
)


class _Assembler:
    def __init__(self, fingerprint: str = "fingerprint-current") -> None:
        self.fingerprint = fingerprint

    def assemble(self, plan_revision_id: str, parent_logical_node_id: str):
        return {
            "novel_id": "novel-1",
            "plan_revision_id": plan_revision_id,
            "plan_digest": "plan-current",
            "canonical_prefix_digest": "canonical-current",
            "context_digest": "context-current",
            "scope_fingerprint": self.fingerprint,
            "scope": {
                "parent_logical_node_id": parent_logical_node_id,
                "level": "volume",
                "parent_version_id": "version-parent",
                "parent_version_digest": "digest-parent",
                "children": [
                    {
                        "logical_node_id": "child-1",
                        "version_id": "version-child-1",
                        "version_digest": "digest-child-1",
                    }
                ],
            },
            "fragments": [
                {"id": "outline:parent:digest-parent", "payload": {"title": "Parent"}},
                {"id": "outline:child-1:digest-child-1", "payload": {"title": "Child"}},
            ],
            "evidence_refs": [
                "outline:parent:digest-parent",
                "outline:child-1:digest-child-1",
            ],
        }


class _LLM:
    def __init__(self, payload=None, error: Exception | None = None) -> None:
        self.payload = payload
        self.error = error
        self.calls = []

    async def generate(self, prompt, config):
        self.calls.append((prompt, config))
        if self.error:
            raise self.error
        return SimpleNamespace(content=json.dumps(self.payload))


class _ReviewRepository:
    def __init__(self, old_record: dict | None = None) -> None:
        self.old_record = old_record
        self.begin_calls = []
        self.current_calls = []
        self.completed = []
        self.failed = []

    def begin(self, **kwargs):
        self.begin_calls.append(kwargs)
        if (
            self.old_record
            and self.old_record["plan_digest"] == kwargs["plan_digest"]
            and self.old_record["scope_fingerprint"] == kwargs["scope_fingerprint"]
        ):
            return self.old_record
        return {
            "id": "review-current",
            "state": "running",
            "decision": "unavailable",
            "plan_digest": kwargs["plan_digest"],
            "scope_fingerprint": kwargs["scope_fingerprint"],
            "report": {},
        }

    def complete(self, review_id, *, report, raw_response=""):
        self.completed.append((review_id, report, raw_response))
        return {
            "id": review_id,
            "state": "succeeded",
            "decision": report.decision.value,
            "plan_digest": "plan-current",
            "scope_fingerprint": report.scope_fingerprint,
            "report": report.to_dict(),
            "raw_response": raw_response,
            "error": "",
        }

    def current(self, **kwargs):
        self.current_calls.append(kwargs)
        if (
            self.old_record
            and self.old_record["scope_fingerprint"] == kwargs["scope_fingerprint"]
        ):
            return self.old_record
        return None

    def fail(self, review_id, *, error, raw_response=""):
        self.failed.append((review_id, error, raw_response))
        return {
            "id": review_id,
            "state": "succeeded",
            "decision": "unavailable",
            "plan_digest": "plan-current",
            "scope_fingerprint": "fingerprint-current",
            "report": {
                "decision": "unavailable",
                "confidence": None,
                "scope_fingerprint": "fingerprint-current",
                "issues": [],
                "suggestions": [],
                "model": "",
                "schema_version": "1",
                "ruleset_version": "1",
                "error": error,
            },
            "raw_response": raw_response,
            "error": error,
        }


def _issue(*, evidence_refs, severity="conflict"):
    return {
        "id": "issue-1",
        "code": "sibling_handoff",
        "severity": severity,
        "scope": {"parent_logical_node_id": "parent"},
        "from_ref": "outline:parent:digest-parent",
        "to_ref": "outline:child-1:digest-child-1",
        "evidence_refs": evidence_refs,
        "message": "The child contradicts the parent exit state.",
        "suggestion": "Bridge the state transition.",
        "suggested_patch": None,
    }


def _service(monkeypatch, llm, review_repository, assembler=None):
    monkeypatch.setattr(
        "application.blueprint.services.outline_continuity_review_service.render_required_prompt",
        lambda key, variables: Prompt(system="Review only.", user=json.dumps(variables)),
    )
    return OutlineContinuityReviewService(
        repository=SimpleNamespace(),
        review_repository=review_repository,
        llm_service=llm,
        db=SimpleNamespace(),
        assembler=assembler or _Assembler(),
    )


@pytest.mark.asyncio
async def test_evidence_backed_conflict_is_persisted_as_conflict(monkeypatch):
    llm = _LLM(
        {
            "decision": "conflict",
            "confidence": 0.91,
            "issues": [_issue(evidence_refs=["outline:child-1:digest-child-1"])],
            "suggestions": [],
        }
    )
    reviews = _ReviewRepository()

    result = await _service(monkeypatch, llm, reviews).review(
        "plan-1", "parent", "plan-current"
    )

    assert result["decision"] == "conflict"
    assert result["report"]["issues"][0]["severity"] == "conflict"
    assert result["report"]["issues"][0]["evidence_refs"] == [
        "outline:child-1:digest-child-1"
    ]
    assert reviews.completed[0][0] == "review-current"
    assert llm.calls[0][1].response_format == {"type": "json_object"}


@pytest.mark.asyncio
async def test_conflict_without_supplied_evidence_is_downgraded_to_review(monkeypatch):
    llm = _LLM(
        {
            "decision": "conflict",
            "confidence": 0.93,
            "issues": [_issue(evidence_refs=["outline:invented:digest"])],
            "suggestions": [],
        }
    )

    result = await _service(monkeypatch, llm, _ReviewRepository()).review(
        "plan-1", "parent", "plan-current"
    )

    assert result["decision"] == "review"
    assert result["report"]["issues"][0]["severity"] == "warning"
    assert result["report"]["issues"][0]["evidence_refs"] == []


@pytest.mark.asyncio
async def test_provider_failure_is_persisted_and_returned_as_unavailable(monkeypatch):
    reviews = _ReviewRepository()
    llm = _LLM(error=RuntimeError("provider offline"))

    result = await _service(monkeypatch, llm, reviews).review(
        "plan-1", "parent", "plan-current"
    )

    assert result["state"] == "succeeded"
    assert result["decision"] == "unavailable"
    assert result["report"]["error"] == "provider offline"
    assert reviews.failed == [("review-current", "provider offline", "")]


@pytest.mark.asyncio
async def test_old_scope_fingerprint_is_history_not_a_reusable_current_result(monkeypatch):
    old = {
        "id": "review-old",
        "state": "succeeded",
        "decision": "pass",
        "plan_digest": "plan-current",
        "scope_fingerprint": "fingerprint-old",
        "report": {"decision": "pass", "scope_fingerprint": "fingerprint-old"},
    }
    reviews = _ReviewRepository(old_record=old)
    llm = _LLM(
        {"decision": "pass", "confidence": 0.88, "issues": [], "suggestions": []}
    )

    result = await _service(monkeypatch, llm, reviews).review(
        "plan-1", "parent", "plan-current"
    )

    assert result["id"] == "review-current"
    assert result["scope_fingerprint"] != "fingerprint-old"
    assert len(llm.calls) == 1
    assert reviews.begin_calls[0]["scope_fingerprint"] == result["scope_fingerprint"]


@pytest.mark.asyncio
async def test_current_recomputes_scope_identity_before_reusing_a_report(monkeypatch):
    llm = _LLM(
        {"decision": "pass", "confidence": 0.88, "issues": [], "suggestions": []}
    )
    reviews = _ReviewRepository()
    assembler = _Assembler()
    service = _service(monkeypatch, llm, reviews, assembler)

    completed = await service.review("plan-1", "parent", "plan-current")
    reviews.old_record = completed

    assert service.current("plan-1", "parent", "plan-current")["id"] == completed["id"]

    assembler.fingerprint = "fingerprint-after-scope-edit"

    assert service.current("plan-1", "parent", "plan-current") is None


class _Database:
    def __init__(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            CREATE TABLE outline_contract_versions (
                id TEXT PRIMARY KEY, payload_json TEXT NOT NULL
            );
            CREATE TABLE bible_world_settings (
                id TEXT PRIMARY KEY, novel_id TEXT, name TEXT, description TEXT, updated_at TEXT
            );
            CREATE TABLE unified_characters (
                id TEXT PRIMARY KEY, novel_id TEXT, name TEXT, description TEXT, updated_at TEXT
            );
            CREATE TABLE bible_locations (
                id TEXT PRIMARY KEY, novel_id TEXT, name TEXT, description TEXT, updated_at TEXT
            );
            CREATE TABLE knowledge (id TEXT PRIMARY KEY, novel_id TEXT);
            CREATE TABLE chapter_summaries (
                id TEXT PRIMARY KEY, knowledge_id TEXT, chapter_number INTEGER,
                summary TEXT, sync_status TEXT, canonical_payload_sha256 TEXT
            );
            """
        )

    def get_connection(self):
        return self.connection


def _plan():
    items = (
        OutlinePlanItem("root", "v-root", "d-root", OutlineLevel.OUTLINE, 0),
        OutlinePlanItem("parent", "v-parent", "d-parent", OutlineLevel.PART, 0, "root"),
        OutlinePlanItem("child-b", "v-child-b", "d-child-b", OutlineLevel.VOLUME, 1, "parent"),
        OutlinePlanItem("child-a", "v-child-a", "d-child-a", OutlineLevel.VOLUME, 0, "parent"),
    )
    return OutlinePlanRevision(
        id="plan-1",
        novel_id="novel-1",
        revision=1,
        status=PlanRevisionStatus.DRAFT,
        digest="plan-current",
        canonical_prefix_digest="canonical-current",
        canonical_boundary={"formal_head": 0},
        items=items,
    )


def _assembler(plan=None):
    database = _Database()
    for item in (plan or _plan()).items:
        database.connection.execute(
            "INSERT INTO outline_contract_versions (id, payload_json) VALUES (?, ?)",
            (item.version_id, json.dumps({"title": item.logical_node_id})),
        )
    database.connection.commit()
    repository = SimpleNamespace(get_plan_revision=lambda plan_revision_id: plan or _plan())
    return OutlineContinuityContextAssembler(repository, database)


def test_context_assembler_preserves_ordered_direct_children_and_fragment_ids():
    context = _assembler().assemble("plan-1", "parent")

    assert [item["logical_node_id"] for item in context["scope"]["children"]] == [
        "child-a",
        "child-b",
    ]
    assert context["evidence_refs"][:4] == [
        "outline:root:d-root",
        "outline:parent:d-parent",
        "outline:child-a:d-child-a",
        "outline:child-b:d-child-b",
    ]
    assert context["plan_digest"] == "plan-current"
    assert context["canonical_prefix_digest"] == "canonical-current"
    assert context["context_digest"]
    assert context["scope_fingerprint"]


def test_context_assembler_rejects_missing_parent():
    with pytest.raises(ValueError, match="parent"):
        _assembler().assemble("plan-1", "missing")


def test_context_assembler_rejects_invalid_direct_child_level():
    plan = _plan()
    invalid = replace(plan.items[-1], level=OutlineLevel.ACT)
    plan = replace(plan, items=(*plan.items[:-1], invalid))

    with pytest.raises(ValueError, match="direct-child level"):
        _assembler(plan).assemble("plan-1", "parent")


def test_context_assembler_chunks_nine_children_with_one_boundary_overlap():
    base = _plan()
    children = tuple(
        OutlinePlanItem(
            f"child-{index:02d}",
            f"v-child-{index:02d}",
            f"d-child-{index:02d}",
            OutlineLevel.VOLUME,
            index,
            "parent",
        )
        for index in range(9)
    )
    plan = replace(base, items=(base.items[0], base.items[1], *children))
    assembler = _assembler(plan)
    context = assembler.assemble("plan-1", "parent")

    chunks = context["review_chunks"]
    assert [[child["logical_node_id"] for child in chunk["children"]] for chunk in chunks] == [
        [f"child-{index:02d}" for index in range(8)],
        ["child-07", "child-08"],
    ]
    assert chunks[0]["chunk_index"] == 0
    assert chunks[1]["chunk_index"] == 1
    assert all(chunk["chunk_count"] == 2 for chunk in chunks)
    assert len(context["children"]) == 9
    assert set(context["evidence_refs"]) >= {
        f"outline:child-{index:02d}:d-child-{index:02d}" for index in range(9)
    }


def test_context_assembler_omits_redundant_boundary_only_chunk_for_fifteen_children():
    base = _plan()
    children = tuple(
        OutlinePlanItem(
            f"child-{index:02d}",
            f"v-child-{index:02d}",
            f"d-child-{index:02d}",
            OutlineLevel.VOLUME,
            index,
            "parent",
        )
        for index in range(15)
    )
    plan = replace(base, items=(base.items[0], base.items[1], *children))

    chunks = _assembler(plan).assemble("plan-1", "parent")["review_chunks"]

    assert [[child["logical_node_id"] for child in chunk["children"]] for chunk in chunks] == [
        [f"child-{index:02d}" for index in range(8)],
        [f"child-{index:02d}" for index in range(7, 15)],
    ]
    assert [chunk["chunk_index"] for chunk in chunks] == [0, 1]
    assert [chunk["chunk_count"] for chunk in chunks] == [2, 2]


@pytest.mark.asyncio
async def test_chunk_responses_execute_in_order_and_merge_duplicate_conflict(monkeypatch):
    assembler = _Assembler()
    base_context = assembler.assemble("plan-1", "parent")
    assembler.assemble = lambda plan_revision_id, parent_logical_node_id: {
        **base_context,
        "review_chunks": [
            {
                "chunk_index": 0,
                "chunk_count": 2,
                "scope": base_context["scope"],
                "ancestor_versions": [],
                "parent": {"id": "outline:parent:digest-parent"},
                "ancestors": [],
                "children": [{"id": "outline:child-1:digest-child-1"}],
                "fragments": base_context["fragments"],
                "evidence_refs": base_context["evidence_refs"],
            },
            {
                "chunk_index": 1,
                "chunk_count": 2,
                "scope": base_context["scope"],
                "ancestor_versions": [],
                "parent": {"id": "outline:parent:digest-parent"},
                "ancestors": [],
                "children": [{"id": "outline:child-1:digest-child-1"}],
                "fragments": base_context["fragments"],
                "evidence_refs": base_context["evidence_refs"],
            },
        ],
    }
    llm = _LLM()

    async def generate(prompt, config):
        index = len(llm.calls)
        llm.calls.append((prompt, config))
        return SimpleNamespace(content=json.dumps({
            "decision": "review",
            "confidence": [0.81, 0.74][index],
            "issues": [_issue(evidence_refs=["outline:child-1:digest-child-1"], severity=["warning", "conflict"][index])],
            "suggestions": [{"text": "bridge"}],
        }))

    llm.generate = generate
    result = await _service(monkeypatch, llm, _ReviewRepository(), assembler).review(
        "plan-1", "parent", "plan-current"
    )
    assert len(llm.calls) == 2
    rendered_contexts = [
        json.loads(json.loads(call[0].user)["review_context"])
        for call in llm.calls
    ]
    assert [item["chunk_index"] for item in rendered_contexts] == [0, 1]
    assert result["decision"] == "conflict"
    assert result["report"]["issues"][0]["severity"] == "conflict"
    assert result["report"]["confidence"] == 0.74
    assert result["report"]["suggestions"] == [{"text": "bridge"}]


@pytest.mark.asyncio
async def test_generation_profile_failure_is_unavailable_without_llm_call(monkeypatch):
    monkeypatch.setattr(
        "application.blueprint.services.outline_continuity_review_service.generation_config_from_profile",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("profile broken")),
    )
    llm = _LLM({"decision": "pass", "confidence": 1, "issues": [], "suggestions": []})
    reviews = _ReviewRepository()
    result = await _service(monkeypatch, llm, reviews).review("plan-1", "parent", "plan-current")
    assert result["decision"] == "unavailable"
    assert "profile broken" in result["report"]["error"]
    assert not llm.calls


@pytest.mark.asyncio
async def test_effective_model_resolution_error_uses_provider_settings(monkeypatch):
    llm = _LLM({"decision": "pass", "confidence": 0.9, "issues": [], "suggestions": []})
    llm.factory = SimpleNamespace(
        control_service=SimpleNamespace(
            resolve_active_profile=lambda: (_ for _ in ()).throw(RuntimeError("profile unavailable"))
        )
    )
    llm.settings = SimpleNamespace(
        default_model="settings-model",
        default_max_tokens=90,
        default_temperature=0.3,
        timeout_seconds=12,
    )
    result = await _service(monkeypatch, llm, _ReviewRepository()).review(
        "plan-1", "parent", "plan-current"
    )
    assert result["decision"] == "pass"
    assert result["report"]["model"] == "settings-model"


@pytest.mark.asyncio
async def test_second_chunk_failure_stores_received_outputs_as_json_array(monkeypatch):
    assembler = _Assembler()
    base_context = assembler.assemble("plan-1", "parent")
    chunk = {
        "chunk_count": 2,
        "scope": base_context["scope"],
        "ancestor_versions": [],
        "parent": base_context["fragments"][0],
        "ancestors": [],
        "children": base_context["fragments"][1:],
        "fragments": base_context["fragments"],
        "evidence_refs": base_context["evidence_refs"],
    }
    assembler.assemble = lambda *args: {
        **base_context,
        "review_chunks": [
            {**chunk, "chunk_index": 0},
            {**chunk, "chunk_index": 1},
        ],
    }
    llm = _LLM()

    async def generate(prompt, config):
        llm.calls.append((prompt, config))
        if len(llm.calls) == 2:
            raise RuntimeError("second chunk failed")
        return SimpleNamespace(
            content=json.dumps(
                {"decision": "pass", "confidence": 0.9, "issues": [], "suggestions": []}
            )
        )

    llm.generate = generate
    reviews = _ReviewRepository()
    result = await _service(monkeypatch, llm, reviews, assembler).review(
        "plan-1", "parent", "plan-current"
    )
    assert result["decision"] == "unavailable"
    assert isinstance(json.loads(result["raw_response"]), list)
    assert len(json.loads(result["raw_response"])) == 1


@pytest.mark.asyncio
async def test_effective_model_and_key_config_changes_change_scope_fingerprint(monkeypatch):
    llm = _LLM({"decision": "pass", "confidence": 0.9, "issues": [], "suggestions": []})
    reviews = _ReviewRepository()
    service = _service(monkeypatch, llm, reviews)
    config = SimpleNamespace(model="model-a", max_tokens=100, temperature=0.2, response_format={"type": "json_object"}, timeout_seconds=30, reasoning_effort=None, thinking=None)
    monkeypatch.setattr("application.blueprint.services.outline_continuity_review_service.generation_config_from_profile", lambda *a, **k: config)
    first = await service.review("plan-1", "parent", "plan-current", force=True)
    config.model = "model-b"
    model_changed = await service.review("plan-1", "parent", "plan-current", force=True)
    assert first["scope_fingerprint"] != model_changed["scope_fingerprint"]
    config.max_tokens = 101
    config_changed = await service.review("plan-1", "parent", "plan-current", force=True)
    assert model_changed["scope_fingerprint"] != config_changed["scope_fingerprint"]


@pytest.mark.asyncio
async def test_info_only_high_confidence_passes_and_warning_reviews(monkeypatch):
    info = _issue(evidence_refs=["outline:child-1:digest-child-1"], severity="info")
    warning = _issue(evidence_refs=["outline:child-1:digest-child-1"], severity="warning")
    for issue, expected in ((info, "pass"), (warning, "review")):
        llm = _LLM({"decision": "pass", "confidence": 0.9, "issues": [issue], "suggestions": []})
        result = await _service(monkeypatch, llm, _ReviewRepository()).review("plan-1", "parent", "plan-current")
        assert result["decision"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", 123),
        ("id", ""),
        ("code", []),
        ("message", "  "),
        ("from_ref", {}),
        ("to_ref", 2),
        ("suggestion", None),
        ("scope", []),
        ("evidence_refs", [123]),
        ("suggested_patch", []),
    ],
)
async def test_malformed_issue_field_types_are_unavailable(monkeypatch, field, value):
    malformed = _issue(evidence_refs=["outline:child-1:digest-child-1"])
    malformed[field] = value
    llm = _LLM({"decision": "conflict", "confidence": 0.9, "issues": [malformed], "suggestions": []})
    result = await _service(monkeypatch, llm, _ReviewRepository()).review("plan-1", "parent", "plan-current")
    assert result["decision"] == "unavailable"
