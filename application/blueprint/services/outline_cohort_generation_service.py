"""Manifest-native generation of a complete direct-child outline cohort."""

from __future__ import annotations

import json
from typing import Any, Protocol, Sequence

from application.blueprint.services.outline_contract_service import OutlineContractService
from domain.ai.services.llm_service import GenerationConfig
from domain.ai.value_objects.prompt import Prompt
from domain.structure.outline_contract import OutlineLevel, OutlinePayload
from infrastructure.persistence.database.outline_contract_repository import (
    OutlineContractRepository,
)


class OutlineCohortGenerationError(ValueError):
    """A provider response cannot safely become a complete cohort draft."""


class OutlineCohortLLM(Protocol):
    async def generate(self, prompt: Prompt, config: GenerationConfig) -> Any: ...


class OutlineCohortGenerationService:
    """Generate one manifest sibling cohort without changing the active plan."""

    def __init__(
        self,
        repository: OutlineContractRepository,
        contract_service: OutlineContractService,
        llm_service: OutlineCohortLLM,
        db: Any,
    ) -> None:
        self.repository = repository
        self.contract_service = contract_service
        self.llm_service = llm_service
        self.db = db

    async def generate_cohort(
        self,
        *,
        plan_revision_id: str,
        parent_logical_node_id: str,
        level: OutlineLevel,
    ) -> dict[str, Any]:
        plan = self.repository.get_plan_revision(plan_revision_id)
        parent = next(
            (item for item in plan.items if item.logical_node_id == parent_logical_node_id),
            None,
        )
        if parent is None or parent.level.child_level != level:
            raise OutlineCohortGenerationError("parent does not own the requested cohort level")
        prompt, scope, context_digest = self._build_prompt(plan, parent, level)
        attempt = self.repository.start_manifest_cohort_attempt(
            plan_revision_id=plan.id,
            parent_logical_node_id=parent.logical_node_id,
            level=level,
            scope=scope,
            context_digest=context_digest,
            prompt_snapshot={"system": prompt.system, "user": prompt.user},
        )
        try:
            response = await self.llm_service.generate(
                prompt, GenerationConfig(temperature=0.7)
            )
            raw = str(getattr(response, "content", response) or "")
            self.repository.append_manifest_cohort_attempt_delta(attempt["id"], raw)
            payloads = self._parse_payloads(raw)
            updated = self.repository.replace_draft_cohort_payloads(
                plan_revision_id=plan.id,
                parent_logical_node_id=parent.logical_node_id,
                payloads=payloads,
            )
            completed = self.repository.complete_manifest_cohort_attempt(attempt["id"])
            return {"attempt": completed, "plan": updated}
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            self.repository.fail_manifest_cohort_attempt(attempt["id"], str(exc))
            raise

    def _build_prompt(
        self,
        plan: Any,
        parent: Any,
        level: OutlineLevel,
    ) -> tuple[Prompt, dict[str, Any], str]:
        conn = self.db.get_connection()
        novel = conn.execute(
            "SELECT title, premise, target_chapters FROM novels WHERE id = ?",
            (plan.novel_id,),
        ).fetchone()
        if novel is None:
            raise KeyError(f"novel not found: {plan.novel_id}")
        rows = self.repository.active_plan_items_with_payload(plan.novel_id)
        by_logical = {str(row["logical_node_id"]): row for row in rows}
        parent_row = by_logical.get(parent.logical_node_id)
        if parent_row is None:
            raise OutlineCohortGenerationError("active manifest parent is unavailable")
        bible = self._bible_context(plan.novel_id)
        boundary = dict(plan.canonical_boundary or {})
        sibling_rows = [
            row
            for row in rows
            if row.get("parent_logical_node_id") == parent.logical_node_id
        ]
        scope = {
            "plan_digest": plan.digest,
            "parent_logical_node_id": parent.logical_node_id,
            "parent_version_digest": parent.version_digest,
            "level": level.value,
            "target_chapters": int(novel["target_chapters"] or 0),
        }
        context = {
            "author_intent": plan.author_intent,
            "canonical_boundary": boundary,
            "story_bible": bible,
            "current_parent": parent_row,
            "current_sibling_overview": sibling_rows,
            "target_chapters": scope["target_chapters"],
            "target_ending": self._payload_value(rows, OutlineLevel.OUTLINE, "exit_state"),
        }
        encoded_context = json.dumps(context, ensure_ascii=False, sort_keys=True)
        import hashlib

        context_digest = hashlib.sha256(encoded_context.encode("utf-8")).hexdigest()
        label = {
            OutlineLevel.PART: "全部部纲",
            OutlineLevel.VOLUME: "该部全部卷纲",
            OutlineLevel.ACT: "该卷全部幕纲",
            OutlineLevel.CHAPTER: "该幕全部章纲",
        }[level]
        return (
            Prompt(
                system=(
                    "你是长篇小说规划助手。只输出 JSON 数组，数组元素是完整同级规划。"
                    "不得修改 Canonical 已发生事实，不得输出 Markdown。"
                ),
                user=(
                    f"小说：{novel['title']}\n创意：{novel['premise'] or ''}\n"
                    f"现在整体生成：{label}\n"
                    f"规划上下文：{encoded_context}\n"
                    "每项必须包含 title、narrative_text、creative_goal、entry_state、"
                    "exit_state、conflicts、state_changes、handoff_conditions、"
                    "chapter_start、chapter_end。相邻项必须首尾连续，最后一项到达父级 exit_state。"
                ),
            ),
            scope,
            context_digest,
        )

    def _bible_context(self, novel_id: str) -> dict[str, list[dict[str, str]]]:
        conn = self.db.get_connection()
        tables = (
            ("world_settings", "bible_world_settings"),
            ("characters", "unified_characters"),
            ("locations", "bible_locations"),
        )
        result: dict[str, list[dict[str, str]]] = {}
        for key, table in tables:
            rows = conn.execute(
                f"SELECT name, description FROM {table} WHERE novel_id = ? ORDER BY id LIMIT 20",
                (novel_id,),
            ).fetchall()
            if rows:
                result[key] = [
                    {"name": str(row["name"] or ""), "description": str(row["description"] or "")}
                    for row in rows
                ]
        return result

    @staticmethod
    def _payload_value(
        rows: Sequence[dict[str, Any]], level: OutlineLevel, field: str
    ) -> str:
        row = next((item for item in rows if item.get("level") == level.value), None)
        payload = dict(row.get("payload") or {}) if row else {}
        return str(payload.get(field) or "")

    @staticmethod
    def _parse_payloads(raw: str) -> tuple[OutlinePayload, ...]:
        text = str(raw or "").strip()
        if text.startswith("```") and text.endswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        try:
            values = json.loads(text)
        except json.JSONDecodeError as exc:
            raise OutlineCohortGenerationError("cohort generation requires a JSON array") from exc
        if not isinstance(values, list) or not values or not all(isinstance(item, dict) for item in values):
            raise OutlineCohortGenerationError("cohort generation requires a non-empty JSON object array")
        return tuple(OutlinePayload.from_dict(item) for item in values)
