"""Sequential, parent-gated AI draft generation for the five-level outline."""

from __future__ import annotations

import json
import re
from typing import Any, AsyncIterator, Protocol

from domain.ai.services.llm_service import GenerationConfig
from domain.ai.value_objects.prompt import Prompt
from domain.structure.outline_contract import OutlinePayload, OutlineSource, OutlineStatus
from infrastructure.persistence.database.outline_contract_repository import (
    OutlineContractRepository,
    OutlineContractSlot,
)


class OutlineDraftGenerationError(ValueError):
    """A generation result cannot safely become an outline draft."""


class OutlineDraftLLM(Protocol):
    async def generate(self, prompt: Prompt, config: GenerationConfig) -> Any: ...

    async def stream_generate(
        self, prompt: Prompt, config: GenerationConfig
    ) -> AsyncIterator[str]: ...


class OutlineDraftGenerationService:
    """Generate one level only; children never call an unconfirmed parent."""

    def __init__(self, repository: OutlineContractRepository, llm_service: OutlineDraftLLM, db: Any) -> None:
        self.repository = repository
        self.llm_service = llm_service
        self.db = db

    async def generate_draft(self, contract_id: str) -> OutlineContractSlot:
        slot = self.repository.get_slot(contract_id)
        prompt = self._build_prompt(slot)
        result = await self.llm_service.generate(prompt, GenerationConfig(temperature=0.7))
        content = str(getattr(result, "content", result) or "")
        return self._save_generated(slot, content)

    async def stream_generate_draft(self, contract_id: str) -> AsyncIterator[dict[str, Any]]:
        """SSE-friendly token stream; draft persistence happens only at done."""

        slot = self.repository.get_slot(contract_id)
        prompt = self._build_prompt(slot)
        yield {"type": "phase", "phase": "outline_generation", "level": slot.level.value}
        chunks: list[str] = []
        async for chunk in self.llm_service.stream_generate(prompt, GenerationConfig(temperature=0.7)):
            text = str(chunk or "")
            if text:
                chunks.append(text)
                yield {"type": "chunk", "text": text, "level": slot.level.value}
        drafted = self._save_generated(slot, "".join(chunks))
        yield {
            "type": "done",
            "contract_id": drafted.id,
            "level": drafted.level.value,
            "revision": drafted.draft.revision if drafted.draft else None,
            "payload": drafted.draft.payload.canonical_dict() if drafted.draft else {},
        }

    def _build_prompt(self, slot: OutlineContractSlot) -> Prompt:
        parent_context = self._published_parent_context(slot)
        novel = self.db.get_connection().execute(
            "SELECT title, premise, target_chapters FROM novels WHERE id = ?", (slot.novel_id,)
        ).fetchone()
        if novel is None:
            raise KeyError(f"novel not found: {slot.novel_id}")
        level_label = {
            "outline": "总纲",
            "part": "部纲",
            "volume": "卷纲",
            "act": "幕纲",
            "chapter": "章纲",
        }[slot.level.value]
        system = (
            "你是长篇小说规划助手。只输出一个 JSON 对象，不要 Markdown、解释或代码围栏。"
            "不得编造已发生事实；下级计划必须满足父级已发布契约。"
        )
        user = (
            f"小说：{novel['title']}\n"
            f"创意：{novel['premise'] or ''}\n"
            f"目标章节数：{novel['target_chapters'] or 0}\n"
            f"现在只生成：{level_label}\n"
            f"已发布父级契约：{json.dumps(parent_context, ensure_ascii=False, sort_keys=True)}\n\n"
            "JSON 至少包含 title、narrative_text、creative_goal、entry_state、exit_state、"
            "required_events、forbidden_events、state_changes、foreshadowing、chapter_start、"
            "chapter_end、word_budget、handoff_conditions；章纲额外给 pov、scenes、beats、conflicts、ending_hook。"
        )
        return Prompt(system=system, user=user)

    def _published_parent_context(self, slot: OutlineContractSlot) -> list[dict[str, Any]]:
        """Walk only active synced ancestors, rejecting stale/draft parent access."""

        chain: list[dict[str, Any]] = []
        parent_id = slot.parent_contract_id
        while parent_id:
            parent = self.repository.get_slot(parent_id)
            if parent.active is None or parent.active.status != OutlineStatus.SYNCED:
                state = parent.active.status.value if parent.active else "missing"
                raise OutlineDraftGenerationError(
                    f"parent outline must be synced before generation: {parent.level.value}:{state}"
                )
            chain.append(
                {
                    "level": parent.level.value,
                    "contract_id": parent.id,
                    "revision": parent.active.revision,
                    "digest": parent.active.published_digest or parent.active.digest,
                    "payload": parent.active.payload.canonical_dict(),
                }
            )
            parent_id = parent.parent_contract_id
        return list(reversed(chain))

    def _save_generated(self, slot: OutlineContractSlot, raw: str) -> OutlineContractSlot:
        payload = OutlinePayload.from_dict(self._parse_json_object(raw))
        return self.repository.save_draft(slot.id, payload, source=OutlineSource.AI)

    @staticmethod
    def _parse_json_object(raw: str) -> dict[str, Any]:
        text = str(raw or "").strip()
        fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.IGNORECASE | re.DOTALL)
        if fenced:
            text = fenced.group(1).strip()
        decoder = json.JSONDecoder()
        starts = [index for index, char in enumerate(text) if char == "{"]
        for start in starts:
            try:
                value, _ = decoder.raw_decode(text[start:])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
        raise OutlineDraftGenerationError("outline_generation_requires_json_object")
