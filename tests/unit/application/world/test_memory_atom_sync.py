from application.world.services.chapter_narrative_sync import persist_bundle_memory_atoms


class FakeMemoryService:
    def __init__(self):
        self.entities = []
        self.atoms = []

    def ensure_entity(self, novel_id, entity_id, *, entity_type="character", canonical_name="", aliases=None):
        self.entities.append(
            {
                "novel_id": novel_id,
                "entity_id": entity_id,
                "canonical_name": canonical_name,
            }
        )

    def remember(
        self,
        novel_id,
        entity_id,
        memory_type,
        payload,
        *,
        entity_type="character",
        scope="global",
        source="manual",
        status="candidate",
        chapter_number=None,
        text_span="",
        confidence=0.5,
    ):
        self.atoms.append(
            {
                "novel_id": novel_id,
                "entity_id": entity_id,
                "memory_type": memory_type,
                "payload": payload,
                "source": source,
                "status": status,
                "chapter_number": chapter_number,
                "text_span": text_span,
                "confidence": confidence,
            }
        )


def test_persist_bundle_memory_atoms_uses_injected_memory_service():
    memory = FakeMemoryService()
    content = (
        "阿止怀疑师父隐瞒真相。阿止开始警惕宗门。"
        "阿止说：我会守住城门。阿止怀疑师父。旧案线索引出新债。"
    )
    bundle = {
        "character_states": [
            {
                "character_name": "阿止",
                "mental_state": "怀疑师父隐瞒真相",
                "evidence_text": "阿止怀疑师父隐瞒真相",
            },
        ],
        "character_mutations": [
            {
                "data": {
                    "character_name": "阿止",
                    "mutation_type": "scar",
                    "impact": "开始警惕宗门",
                    "evidence_text": "阿止开始警惕宗门",
                }
            },
        ],
        "dialogues": [
            {
                "speaker": "阿止",
                "content": "我会守住城门。",
                "evidence_text": "阿止说：我会守住城门。",
            },
        ],
        "relation_triples": [
            {
                "data": {
                    "subject": "阿止",
                    "predicate": "怀疑",
                    "object": "师父",
                    "evidence_text": "阿止怀疑师父",
                }
            },
        ],
        "causal_edges": [
            {
                "data": {
                    "involved_characters": ["阿止"],
                    "description": "旧案线索引出新债",
                    "evidence_text": "旧案线索引出新债",
                }
            },
        ],
    }

    saved = persist_bundle_memory_atoms(
        "novel-1",
        7,
        bundle,
        memory_service=memory,
        content=content,
    )

    assert saved == 5
    assert {atom["memory_type"] for atom in memory.atoms} == {
        "state",
        "scar",
        "voice",
        "relationship",
        "debt",
    }
    assert all(atom["chapter_number"] == 7 for atom in memory.atoms)
    assert all(atom["status"] == "candidate" for atom in memory.atoms)


def test_persist_bundle_memory_atoms_rejects_negated_evidence():
    memory = FakeMemoryService()
    content = "林澈没有杀死反派甲，仍把刀收回鞘中。"
    bundle = {
        "character_states": [
            {
                "character_name": "林澈",
                "mental_state": "杀死反派甲后的轻松",
                "evidence_text": "林澈没有杀死反派甲",
            }
        ],
        "character_mutations": [
            {
                "data": {
                    "character_name": "林澈",
                    "mutation_type": "motivation",
                    "source_event": "杀死反派甲",
                    "impact_or_description": "杀死反派甲后的轻松",
                    "evidence_text": "林澈没有杀死反派甲",
                }
            }
        ],
        "dialogues": [],
        "relation_triples": [
            {
                "data": {
                    "subject": "林澈",
                    "predicate": "杀死",
                    "object": "反派甲",
                    "evidence_text": "林澈没有杀死反派甲",
                }
            }
        ],
        "causal_edges": [
            {
                "data": {
                    "involved_characters": ["林澈"],
                    "source_event": "杀死反派甲",
                    "target_event": "复仇完成",
                    "state_change": "杀死反派甲后的轻松",
                    "evidence_text": "林澈没有杀死反派甲",
                }
            }
        ],
    }

    assert (
        persist_bundle_memory_atoms(
            "novel-1", 7, bundle, memory_service=memory, content=content
        )
        == 0
    )
    assert memory.atoms == []
