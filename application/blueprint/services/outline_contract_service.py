"""Application service for the logical five-level outline tree.

The physical ``story_nodes`` rows remain the source for Part → Volume → Act →
Chapter identity.  This service adds the total-outline root and makes the
published contract projection the only plan data a prose prompt can use.
"""

from __future__ import annotations

from typing import Any, Optional

import json
from collections.abc import Mapping

from domain.structure.outline_contract import (
    OutlineChain,
    OutlineContract,
    OutlineLevel,
    OutlinePayload,
    OutlineSource,
    OutlineStatus,
)
from domain.structure.story_node import NodeType, StoryNode
from domain.structure.outline_plan import PlanningAuthorityMode
from domain.structure.outline_plan import (
    CanonicalPrefix,
    OutlineExpansionRequired,
    PlanReconciliationReport,
    PlanReconciliationStatus,
    canonical_history_digest,
)
from infrastructure.persistence.database.outline_contract_repository import (
    OutlineContractRepository,
    OutlineContractSlot,
)


_NODE_LEVELS = {
    NodeType.PART: OutlineLevel.PART,
    NodeType.VOLUME: OutlineLevel.VOLUME,
    NodeType.ACT: OutlineLevel.ACT,
    NodeType.CHAPTER: OutlineLevel.CHAPTER,
}


class OutlineContractService:
    """Bridge legacy structure nodes to versioned plan contracts."""

    def __init__(
        self,
        *,
        contract_repository: OutlineContractRepository,
        story_node_repository: Any,
    ) -> None:
        self.contract_repository = contract_repository
        self.story_node_repository = story_node_repository

    @staticmethod
    def _node_type(node: StoryNode) -> NodeType:
        value = node.node_type
        return value if isinstance(value, NodeType) else NodeType(str(value))

    @staticmethod
    def _node_dict(node: StoryNode, slot: Optional[OutlineContractSlot]) -> dict[str, Any]:
        payload = node.to_dict()
        active = slot.active if slot else None
        draft = slot.draft if slot else None
        payload["outline_contract"] = {
            "contract_id": slot.id if slot else None,
            "level": slot.level.value if slot else None,
            "status": active.status.value if active else ("draft" if draft else "missing"),
            "active_revision": active.revision if active else None,
            "draft_revision": draft.revision if draft else None,
            "author_locked": bool(slot.author_locked) if slot else False,
            "has_author_edits": bool(slot.has_author_edits) if slot else False,
        }
        return payload

    def _manifest_rows(self, novel_id: str) -> list[dict[str, Any]] | None:
        """Return the active logical manifest when this book has cut over.

        A missing planning Head means the book is still on the legacy
        compatibility path. Once a Head says ``manifest``, failure to read a
        complete sealed item set propagates as a planning gate instead of
        falling back to mutable StoryNode parents.
        """

        get_head = getattr(self.contract_repository, "get_planning_head", None)
        read_manifest = getattr(
            self.contract_repository, "active_plan_items_with_payload", None
        )
        if not callable(get_head) or not callable(read_manifest):
            return None
        try:
            head = get_head(novel_id)
        except KeyError:
            return None
        mode = getattr(head, "authority_mode", None)
        if mode != PlanningAuthorityMode.MANIFEST and str(mode) != PlanningAuthorityMode.MANIFEST.value:
            return None
        return read_manifest(novel_id)

    @staticmethod
    def _manifest_contract(row: Mapping[str, Any]) -> OutlineContract:
        payload = OutlinePayload.from_dict(
            json.loads(str(row.get("payload_json") or "{}"))
        )
        source = row.get("version_source") or row.get("source") or OutlineSource.AI.value
        return OutlineContract(
            id=str(row["logical_node_id"]),
            novel_id=str(row["novel_id"]),
            level=OutlineLevel(str(row["level"])),
            revision=int(row.get("version_revision") or 0),
            payload=payload,
            status=OutlineStatus.SYNCED,
            parent_id=row.get("parent_logical_node_id"),
            story_node_id=row.get("story_node_id"),
            source=OutlineSource(str(source)),
            author_locked=bool(row.get("author_locked")),
            published_digest=str(row.get("version_digest") or ""),
            previous_sibling_digest=str(
                row.get("validated_previous_sibling_digest") or ""
            ),
        )

    def _manifest_chain_for_chapter(
        self, novel_id: str, chapter_node_id: str, rows: list[dict[str, Any]]
    ) -> OutlineChain:
        by_logical = {str(row["logical_node_id"]): row for row in rows}
        chapter_row = next(
            (
                row
                for row in rows
                if str(row.get("story_node_id") or "") == str(chapter_node_id)
                or str(row.get("logical_node_id") or "") == str(chapter_node_id)
            ),
            None,
        )
        if chapter_row is None or str(chapter_row.get("level")) != OutlineLevel.CHAPTER.value:
            raise KeyError(f"chapter outline is not in the active manifest: {chapter_node_id}")

        contracts: list[OutlineContract] = []
        current = chapter_row
        visited: set[str] = set()
        while current is not None:
            logical_id = str(current["logical_node_id"])
            if logical_id in visited:
                raise ValueError("active manifest contains a logical parent cycle")
            visited.add(logical_id)
            contracts.append(self._manifest_contract(current))
            parent_id = current.get("parent_logical_node_id")
            current = by_logical.get(str(parent_id)) if parent_id else None

        chain = OutlineChain(reversed(contracts))
        if tuple(chain._contracts) != OutlineLevel.ordered():
            missing = [
                level.value
                for level in OutlineLevel.ordered()
                if level not in chain._contracts
            ]
            raise ValueError("active manifest chain is incomplete: " + ", ".join(missing))
        return chain

    def logical_tree(self, novel_id: str) -> dict[str, Any]:
        """Return a root-first tree without rewriting legacy parent IDs."""

        root = self.contract_repository.ensure_root(novel_id)
        nodes = self.story_node_repository.get_by_novel_sync(novel_id)
        nodes_by_parent: dict[Optional[str], list[StoryNode]] = {}
        for node in nodes:
            if self._node_type(node) == NodeType.OUTLINE:
                continue
            nodes_by_parent.setdefault(node.parent_id, []).append(node)
        for child_nodes in nodes_by_parent.values():
            child_nodes.sort(key=lambda item: (item.order_index, item.number, item.id))

        def render(node: StoryNode) -> dict[str, Any]:
            slot = self.contract_repository.get_slot_by_story_node(novel_id, node.id)
            result = self._node_dict(node, slot)
            result["children"] = [render(child) for child in nodes_by_parent.get(node.id, [])]
            return result

        active = getattr(root, "active", None)
        draft = getattr(root, "draft", None)
        return {
            "id": root.id,
            "novel_id": novel_id,
            "node_type": OutlineLevel.OUTLINE.value,
            "number": 1,
            "order_index": 0,
            "title": active.payload.title if active else (draft.payload.title if draft else "总纲"),
            "description": active.payload.narrative_text if active else (draft.payload.narrative_text if draft else ""),
            "outline_contract": {
                "contract_id": root.id,
                "level": OutlineLevel.OUTLINE.value,
                "status": active.status.value if active else ("draft" if draft else "missing"),
                "active_revision": active.revision if active else None,
                "draft_revision": draft.revision if draft else None,
                "author_locked": bool(getattr(root, "author_locked", False)),
                "has_author_edits": bool(getattr(root, "has_author_edits", False)),
            },
            "children": [render(node) for node in nodes_by_parent.get(None, [])],
        }

    def ensure_contract_for_story_node(
        self, novel_id: str, story_node_id: str
    ) -> OutlineContractSlot:
        """Bind an existing physical node after its parent is published/synced."""

        existing = self.contract_repository.get_slot_by_story_node(novel_id, story_node_id)
        if existing is not None:
            return existing
        nodes = {node.id: node for node in self.story_node_repository.get_by_novel_sync(novel_id)}
        node = nodes.get(story_node_id)
        if node is None:
            raise KeyError(f"story node not found: {story_node_id}")
        level = _NODE_LEVELS.get(self._node_type(node))
        if level is None:
            raise ValueError(f"unsupported physical outline node: {self._node_type(node).value}")
        if level == OutlineLevel.PART:
            parent_contract_id = self.contract_repository.ensure_root(novel_id).id
        else:
            if not node.parent_id:
                raise ValueError(f"{level.value} node must have a physical parent")
            parent_contract_id = self.ensure_contract_for_story_node(novel_id, node.parent_id).id
        return self.contract_repository.create_contract(
            novel_id=novel_id,
            level=level,
            parent_contract_id=parent_contract_id,
            story_node_id=node.id,
        )

    def active_chain_for_chapter(self, novel_id: str, chapter_node_id: str) -> OutlineChain:
        """Assemble exactly the current root → part → volume → act → chapter chain."""

        manifest_rows = self._manifest_rows(novel_id)
        if manifest_rows is not None:
            return self._manifest_chain_for_chapter(novel_id, chapter_node_id, manifest_rows)

        nodes = {node.id: node for node in self.story_node_repository.get_by_novel_sync(novel_id)}
        chapter = nodes.get(chapter_node_id)
        if chapter is None:
            raise KeyError(f"story node not found: {chapter_node_id}")
        if self._node_type(chapter) != NodeType.CHAPTER:
            raise ValueError("prose context requires a chapter outline node")

        contracts = []
        root = self.contract_repository.ensure_root(novel_id)
        if root.active is not None:
            contracts.append(root.active)
        current: Optional[StoryNode] = chapter
        expected = (OutlineLevel.CHAPTER, OutlineLevel.ACT, OutlineLevel.VOLUME, OutlineLevel.PART)
        for level in expected:
            if current is None or _NODE_LEVELS.get(self._node_type(current)) != level:
                break
            slot = self.contract_repository.get_slot_by_story_node(novel_id, current.id)
            if slot is not None and slot.active is not None:
                contracts.append(slot.active)
            current = nodes.get(current.parent_id) if current.parent_id else None
        return OutlineChain(contracts)

    def published_context_for_chapter(self, novel_id: str, chapter_node_id: str) -> dict[str, Any]:
        chain = self.active_chain_for_chapter(novel_id, chapter_node_id)
        if not chain.ready_for_prose:
            raise ValueError(
                "outline chain is not ready for prose generation: " + ", ".join(chain.blockers)
            )
        return chain.to_prompt_context()

    @staticmethod
    def _content_sha256(content: str) -> str:
        import hashlib

        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def compute_canonical_prefix(
        self, novel_id: str, through_chapter: int
    ) -> CanonicalPrefix:
        """Build a stable formal prefix from persisted historical identities.

        Runtime generation epochs are intentionally absent. Legacy rows use
        ``pre_candidate_formal_history``; Candidate-first rows require the
        exact chapter/formal-commit hash and revision, while narrative and
        memory readiness remain independent gates on the returned value.
        """

        requested = int(through_chapter)
        if requested < 0:
            raise ValueError("through_chapter must be non-negative")
        conn = self.contract_repository._connection()
        identities: list[dict[str, Any]] = []
        blockers: list[str] = []
        formal_head = 0
        canonical_ready = True
        memory_ready = True

        for chapter_number in range(1, requested + 1):
            baseline = conn.execute(
                """
                SELECT baseline.chapter_number, baseline.chapter_id,
                       baseline.content_sha256, baseline.content_revision,
                       chapter.number, chapter.content, chapter.content_sha256 AS chapter_sha,
                       chapter.content_revision AS chapter_revision, chapter.status
                FROM pre_candidate_formal_history AS baseline
                JOIN chapters AS chapter ON chapter.id = baseline.chapter_id
                WHERE baseline.novel_id = ? AND baseline.chapter_number = ?
                """,
                (novel_id, chapter_number),
            ).fetchone()
            if baseline is not None:
                content = str(baseline["content"] or "")
                actual_sha = self._content_sha256(content)
                valid = (
                    int(baseline["number"] or 0) == chapter_number
                    and str(baseline["status"] or "") == "completed"
                    and bool(content.strip())
                    and str(baseline["content_sha256"] or "") == actual_sha
                    and int(baseline["content_revision"] or 0)
                    == int(baseline["chapter_revision"] or 0)
                    and (
                        not str(baseline["chapter_sha"] or "")
                        or str(baseline["chapter_sha"]) == actual_sha
                    )
                )
                if not valid:
                    blockers.append(f"formal:chapter:{chapter_number}")
                    break
                identities.append(
                    {
                        "source": "legacy",
                        "chapter_number": chapter_number,
                        "chapter_id": str(baseline["chapter_id"]),
                        "content_sha256": actual_sha,
                        "content_revision": int(baseline["content_revision"] or 0),
                    }
                )
                formal_head = chapter_number
                continue

            formal = conn.execute(
                """
                SELECT chapter.id AS chapter_id, chapter.number,
                       chapter.content, chapter.content_sha256 AS chapter_sha,
                       chapter.content_revision AS chapter_revision, chapter.status,
                       candidate.id AS candidate_id, candidate.status AS candidate_status,
                       formal.content_sha256 AS formal_sha,
                       formal.content_revision AS formal_revision,
                       formal.sync_status
                FROM chapters AS chapter
                JOIN chapter_candidate_formal_commits AS formal
                  ON formal.chapter_id = chapter.id
                 AND formal.novel_id = chapter.novel_id
                 AND formal.chapter_number = chapter.number
                JOIN chapter_candidates AS candidate
                  ON candidate.id = formal.candidate_id
                WHERE chapter.novel_id = ? AND chapter.number = ?
                """,
                (novel_id, chapter_number),
            ).fetchone()
            if formal is None:
                blockers.append(f"formal:chapter:{chapter_number}")
                break
            content = str(formal["content"] or "")
            actual_sha = self._content_sha256(content)
            valid = (
                int(formal["number"] or 0) == chapter_number
                and str(formal["status"] or "") == "completed"
                and str(formal["candidate_status"] or "") == "committed"
                and str(formal["sync_status"] or "") == "ready"
                and bool(content.strip())
                and actual_sha == str(formal["chapter_sha"] or "")
                and actual_sha == str(formal["formal_sha"] or "")
                and int(formal["chapter_revision"] or 0)
                == int(formal["formal_revision"] or 0)
            )
            if not valid:
                blockers.append(f"formal:chapter:{chapter_number}")
                break

            narrative = conn.execute(
                """
                SELECT novel_id, chapter_number, content_sha256,
                       pipeline_version, content_revision, status, memory_status
                FROM chapter_narrative_commits
                WHERE novel_id = ? AND chapter_number = ?
                  AND content_sha256 = ? AND content_revision = ?
                ORDER BY CASE WHEN status = 'committed' THEN 0 ELSE 1 END,
                         updated_at DESC
                LIMIT 1
                """,
                (novel_id, chapter_number, actual_sha, int(formal["formal_revision"] or 0)),
            ).fetchone()
            if narrative is None:
                canonical_ready = False
                memory_ready = False
                blockers.append(f"canonical:chapter:{chapter_number}")
            else:
                if str(narrative["status"] or "") != "committed":
                    canonical_ready = False
                    blockers.append(f"canonical:chapter:{chapter_number}")
                if str(narrative["memory_status"] or "not_required") not in {
                    "committed",
                    "not_required",
                }:
                    memory_ready = False
                    blockers.append(f"memory:chapter:{chapter_number}")
                identities.append(
                    {
                        "source": "candidate",
                        "chapter_number": chapter_number,
                        "chapter_id": str(formal["chapter_id"]),
                        "candidate_id": str(formal["candidate_id"]),
                        "content_sha256": actual_sha,
                        "content_revision": int(formal["formal_revision"] or 0),
                        "pipeline_version": str(narrative["pipeline_version"] or ""),
                    }
                )
            if narrative is None:
                identities.append(
                    {
                        "source": "candidate",
                        "chapter_number": chapter_number,
                        "chapter_id": str(formal["chapter_id"]),
                        "candidate_id": str(formal["candidate_id"]),
                        "content_sha256": actual_sha,
                        "content_revision": int(formal["formal_revision"] or 0),
                    }
                )
            formal_head = chapter_number

        if requested == 0:
            canonical_ready = True
            memory_ready = True
        formal_ready = formal_head >= requested
        if not formal_ready:
            canonical_ready = False
            memory_ready = False
        return CanonicalPrefix(
            novel_id=novel_id,
            requested_through=requested,
            formal_head=formal_head,
            digest=canonical_history_digest(identities),
            identities=tuple(identities),
            formal_ready=formal_ready,
            canonical_ready=canonical_ready,
            memory_ready=memory_ready,
            blockers=tuple(dict.fromkeys(blockers)),
        )

    def reconcile_plan_boundary(
        self, *, novel_id: str, plan_revision_id: str
    ) -> PlanReconciliationReport:
        plan = self.contract_repository.get_plan_revision(plan_revision_id)
        if plan.novel_id != novel_id:
            raise ValueError("outline plan belongs to another novel")
        boundary = dict(plan.canonical_boundary or {})
        expected_head = int(
            boundary.get("formal_head")
            if boundary.get("formal_head") is not None
            else max(0, int(plan.replan_start_chapter or 1) - 1)
        )
        prefix = self.compute_canonical_prefix(novel_id, expected_head)
        expected_digest = str(plan.canonical_prefix_digest or "")
        digest_matches = not expected_digest or expected_digest == prefix.digest
        if prefix.formal_head < expected_head:
            status = PlanReconciliationStatus.REPAIRABLE
        elif not digest_matches:
            status = PlanReconciliationStatus.AUTHOR_DECISION_REQUIRED
        else:
            status = PlanReconciliationStatus.ALIGNED
        return PlanReconciliationReport(
            plan_revision_id=plan_revision_id,
            status=status,
            expected_formal_head=expected_head,
            actual_formal_head=prefix.formal_head,
            expected_prefix_digest=expected_digest,
            actual_prefix_digest=prefix.digest,
            canonical_ready=prefix.canonical_ready,
            memory_ready=prefix.memory_ready,
            blockers=prefix.blockers,
        )

    def next_published_chapter_context(
        self, novel_id: str, *, after_chapter: int
    ) -> tuple[StoryNode, dict[str, Any]]:
        """Find the next physical chapter whose complete active chain is ready.

        This is deliberately stricter than looking at a chapter number alone:
        a draft, stale or conflicting parent blocks the LLM before any prose
        call can occur.
        """

        nodes = sorted(
            (
                node
                for node in self.story_node_repository.get_by_novel_sync(novel_id)
                if self._node_type(node) == NodeType.CHAPTER and node.number > after_chapter
            ),
            key=lambda node: (node.number, node.order_index, node.id),
        )
        blockers: list[str] = []
        for node in nodes:
            try:
                return node, self.published_context_for_chapter(novel_id, node.id)
            except (KeyError, ValueError) as exc:
                blockers.append(f"{node.id}:{exc}")
        if blockers:
            raise ValueError("no prose-ready chapter outline: " + "; ".join(blockers))
        raise OutlineExpansionRequired(
            "no planned chapter exists after the formal chapter cursor; expand outline cohort"
        )
