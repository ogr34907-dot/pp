from application.engine.services.hierarchical_narrative_alignment_gate import (
    HierarchicalNarrativeAlignmentGate,
)
from application.governance.service import NarrativeGovernanceService


class Repo:
    def __init__(self):
        self.events = []
        self.reports = []
        self.contract = None

    def get_contract(self, _id): return self.contract
    def save_contract(self, contract): self.contract = contract
    def list_storylines(self, _id): return []
    def list_open_debts(self, _id, limit=50): return []
    def latest_report(self, _id): return self.reports[-1] if self.reports else None
    def append_event(self, *args): self.events.append(args)


def _nodes():
    return [
        {"id": "part", "novel_id": "n1", "node_type": "part", "parent_id": None,
         "metadata": {"contract_digest": "p", "narrative_goal": "大目标"}},
        {"id": "volume", "novel_id": "n1", "node_type": "volume", "parent_id": "part",
         "metadata": {"contract_digest": "v", "narrative_goal": "卷目标"}},
        {"id": "act", "novel_id": "n1", "node_type": "act", "parent_id": "volume",
         "metadata": {"contract_digest": "a", "narrative_goal": "幕目标"}},
        {"id": "chapter", "novel_id": "n1", "node_type": "chapter", "number": 1,
         "parent_id": "act", "metadata": {"contract_digest": "c"}},
    ]


def _candidate():
    return {"contract_digests": {"chapter": "c", "act": "a", "volume": "v"},
            "serves_part_commitments": ["大目标"], "serves_volume_commitments": ["卷目标"],
            "serves_act_commitments": ["幕目标"]}


def test_alignment_preview_persists_report_event_and_replan_is_no_write():
    repo = Repo()
    service = NarrativeGovernanceService(repo, hierarchy_gate=HierarchicalNarrativeAlignmentGate(),
                                         story_node_repository=type("Nodes", (), {"get_by_novel_sync": lambda *_: _nodes()})())
    result = service.preview_hierarchy_alignment("n1", "chapter", _candidate())
    assert result["decision"] == "pass"
    assert result["snapshot_digest"]
    assert result["candidate_digest"]
    assert any(event[2] == "HierarchyAlignmentEvaluated" for event in repo.events)
    preview = service.preview_hierarchy_replan("n1", "chapter", _candidate())
    assert preview["would_write"] is False
    assert any(event[2] == "HierarchyReplanPreviewed" for event in repo.events)


def test_override_requires_exact_digest_reason_and_is_single_use():
    repo = Repo()
    service = NarrativeGovernanceService(repo, hierarchy_gate=HierarchicalNarrativeAlignmentGate(),
                                         story_node_repository=type("Nodes", (), {"get_by_novel_sync": lambda *_: _nodes()})())
    blocked = dict(_candidate(), contract_digests={"chapter": "wrong"})
    report = service.preview_hierarchy_alignment("n1", "chapter", blocked)
    try:
        service.override_hierarchy_alignment("n1", report["candidate_digest"], "")
        assert False, "empty reason must fail"
    except ValueError:
        pass
    overridden = service.override_hierarchy_alignment("n1", report["candidate_digest"], "作者确认")
    assert overridden["decision"] == "overridden"
    try:
        service.override_hierarchy_alignment("n1", report["candidate_digest"], "再次确认")
        assert False, "override must be single use"
    except ValueError:
        pass
    assert any(event[2] == "HierarchyAlignmentOverrideApplied" for event in repo.events)
