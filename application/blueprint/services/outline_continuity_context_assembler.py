"""Deterministic evidence context for one direct-child outline cohort."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from domain.structure.outline_continuity import (
    ContinuityReviewScope,
    build_scope_fingerprint,
)


class OutlineContinuityContextAssembler:
    """Read an immutable plan snapshot plus bounded Canonical/Bible evidence."""

    def __init__(self, repository: Any, db: Any) -> None:
        self.repository = repository
        self.db = db

    def assemble(
        self, plan_revision_id: str, parent_logical_node_id: str
    ) -> dict[str, Any]:
        plan = self.repository.get_plan_revision(plan_revision_id)
        by_id = {item.logical_node_id: item for item in plan.items}
        parent = by_id.get(parent_logical_node_id)
        if parent is None:
            raise ValueError(f"continuity review parent not found: {parent_logical_node_id}")
        child_level = parent.level.child_level
        if child_level is None:
            raise ValueError("continuity review parent has no direct-child level")

        direct_children = sorted(
            (
                item
                for item in plan.items
                if item.parent_logical_node_id == parent_logical_node_id
            ),
            key=lambda item: (item.sibling_index, item.logical_node_id),
        )
        if any(item.level != child_level for item in direct_children):
            raise ValueError("continuity review scope contains an invalid direct-child level")

        ancestors = self._ancestors(parent, by_id)
        conn = self.db.get_connection()
        outline_items = (*ancestors, parent, *direct_children)
        outline_fragments = [
            self._outline_fragment(conn, item) for item in outline_items
        ]
        bible_fragments = self._bible_fragments(conn, plan.novel_id)
        canonical_fragments = self._canonical_fragments(conn, plan.novel_id)
        fragments = [*outline_fragments, *bible_fragments, *canonical_fragments]

        scope = ContinuityReviewScope(
            parent_logical_node_id=parent.logical_node_id,
            level=child_level.value,
            parent_version_id=parent.version_id,
            parent_version_digest=parent.version_digest,
            children=tuple(
                {
                    "logical_node_id": item.logical_node_id,
                    "version_id": item.version_id,
                    "version_digest": item.version_digest,
                    "sibling_index": item.sibling_index,
                }
                for item in direct_children
            ),
        )
        ancestor_versions = tuple(
            {
                "logical_node_id": item.logical_node_id,
                "version_id": item.version_id,
                "version_digest": item.version_digest,
                "level": item.level.value,
            }
            for item in ancestors
        )
        context_payload = {
            "scope": scope.to_dict(),
            "ancestor_versions": ancestor_versions,
            "fragments": fragments,
            "canonical_boundary": dict(plan.canonical_boundary or {}),
        }
        context_digest = self._digest(context_payload)
        scope_fingerprint = build_scope_fingerprint(
            scope,
            ancestor_versions=ancestor_versions,
            canonical_prefix_digest=plan.canonical_prefix_digest,
            context_digest=context_digest,
        )
        context = {
            "novel_id": plan.novel_id,
            "plan_revision_id": plan.id,
            "plan_digest": plan.digest,
            "canonical_prefix_digest": plan.canonical_prefix_digest,
            "canonical_boundary": dict(plan.canonical_boundary or {}),
            "scope": scope.to_dict(),
            "ancestor_versions": list(ancestor_versions),
            "ancestors": outline_fragments[: len(ancestors)],
            "parent": outline_fragments[len(ancestors)],
            "children": outline_fragments[len(ancestors) + 1 :],
            "bible_evidence": bible_fragments,
            "canonical_evidence": canonical_fragments,
            "fragments": fragments,
            "evidence_refs": [fragment["id"] for fragment in fragments],
            "context_digest": context_digest,
            "scope_fingerprint": scope_fingerprint,
        }
        child_fragments = context["children"]
        child_scopes = list(scope.children)
        starts = [0]
        while starts[-1] + 8 < len(child_fragments):
            starts.append(starts[-1] + 7)
        child_windows = [
            (child_fragments[start : start + 8], child_scopes[start : start + 8])
            for start in starts
        ]
        chunk_count = len(child_windows)
        shared_fragments = [
            *context["ancestors"],
            context["parent"],
            *bible_fragments,
            *canonical_fragments,
        ]
        context["review_chunks"] = [
            {
                "chunk_index": chunk_index,
                "chunk_count": chunk_count,
                "scope": {
                    **scope.to_dict(),
                    "children": window_scopes,
                },
                "ancestor_versions": list(ancestor_versions),
                "canonical_boundary": context["canonical_boundary"],
                "ancestors": context["ancestors"],
                "parent": context["parent"],
                "children": window_fragments,
                "bible_evidence": bible_fragments,
                "canonical_evidence": canonical_fragments,
                "fragments": [*shared_fragments, *window_fragments],
                "evidence_refs": [
                    fragment["id"] for fragment in (*shared_fragments, *window_fragments)
                ],
                "context_digest": context_digest,
                "scope_fingerprint": scope_fingerprint,
            }
            for chunk_index, (window_fragments, window_scopes) in enumerate(
                child_windows
            )
        ]
        return context

    @staticmethod
    def _ancestors(parent: Any, by_id: Mapping[str, Any]) -> tuple[Any, ...]:
        ancestors = []
        seen = {parent.logical_node_id}
        current = parent
        while current.parent_logical_node_id:
            ancestor = by_id.get(current.parent_logical_node_id)
            if ancestor is None:
                raise ValueError("continuity review ancestor is missing from the plan")
            if ancestor.logical_node_id in seen:
                raise ValueError("continuity review ancestor chain contains a cycle")
            seen.add(ancestor.logical_node_id)
            ancestors.append(ancestor)
            current = ancestor
        ancestors.reverse()
        return tuple(ancestors)

    @classmethod
    def _outline_fragment(cls, conn: Any, item: Any) -> dict[str, Any]:
        row = conn.execute(
            "SELECT payload_json FROM outline_contract_versions WHERE id = ?",
            (item.version_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"outline version payload not found: {item.version_id}")
        try:
            payload = json.loads(str(row["payload_json"] or "{}"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"outline version payload is invalid: {item.version_id}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"outline version payload is invalid: {item.version_id}")
        payload = dict(payload)
        if "narrative_text" in payload:
            payload["narrative_text"] = str(payload["narrative_text"] or "")[:4000]
        return {
            "id": f"outline:{item.logical_node_id}:{item.version_digest}",
            "kind": "outline",
            "logical_node_id": item.logical_node_id,
            "level": item.level.value,
            "parent_logical_node_id": item.parent_logical_node_id,
            "sibling_index": item.sibling_index,
            "version_id": item.version_id,
            "version_digest": item.version_digest,
            "payload": payload,
        }

    @classmethod
    def _bible_fragments(cls, conn: Any, novel_id: str) -> list[dict[str, Any]]:
        fragments: list[dict[str, Any]] = []
        for kind, table in (
            ("world", "bible_world_settings"),
            ("character", "unified_characters"),
            ("location", "bible_locations"),
        ):
            if not cls._table_exists(conn, table):
                continue
            rows = conn.execute(
                f"SELECT id, name, description, updated_at FROM {table} "
                "WHERE novel_id = ? ORDER BY id LIMIT 12",
                (novel_id,),
            ).fetchall()
            for row in rows:
                payload = {
                    "name": str(row["name"] or ""),
                    "description": str(row["description"] or "")[:1200],
                }
                digest = cls._digest(
                    {**payload, "updated_at": str(row["updated_at"] or "")}
                )
                fragments.append(
                    {
                        "id": f"bible:{kind}:{row['id']}:{digest}",
                        "kind": f"bible_{kind}",
                        "payload": payload,
                    }
                )
        return fragments

    @classmethod
    def _canonical_fragments(cls, conn: Any, novel_id: str) -> list[dict[str, Any]]:
        if not cls._table_exists(conn, "knowledge") or not cls._table_exists(
            conn, "chapter_summaries"
        ):
            return []
        rows = conn.execute(
            """
            SELECT summary.chapter_number, summary.summary,
                   summary.canonical_payload_sha256
            FROM chapter_summaries AS summary
            JOIN knowledge ON knowledge.id = summary.knowledge_id
            WHERE knowledge.novel_id = ? AND summary.sync_status = 'committed'
              AND TRIM(COALESCE(summary.summary, '')) <> ''
            ORDER BY summary.chapter_number DESC LIMIT 8
            """,
            (novel_id,),
        ).fetchall()
        fragments = []
        for row in reversed(rows):
            summary = str(row["summary"] or "")[:2000]
            digest = str(row["canonical_payload_sha256"] or "") or cls._digest(
                {"chapter_number": int(row["chapter_number"]), "summary": summary}
            )
            fragments.append(
                {
                    "id": f"canonical:{int(row['chapter_number'])}:{digest}",
                    "kind": "canonical_summary",
                    "payload": {
                        "chapter_number": int(row["chapter_number"]),
                        "summary": summary,
                    },
                }
            )
        return fragments

    @staticmethod
    def _table_exists(conn: Any, table: str) -> bool:
        return (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone()
            is not None
        )

    @staticmethod
    def _digest(value: Any) -> str:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
