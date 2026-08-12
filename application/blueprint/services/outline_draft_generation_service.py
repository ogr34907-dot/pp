"""Sequential, parent-gated AI draft generation for the five-level outline."""

from __future__ import annotations

import asyncio
import json
import re
from hashlib import sha256
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

    async def stream_generate_draft(
        self, contract_id: str, *, retry_attempt_id: str | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """Persist each stream event so reconnecting authors see the real attempt."""

        slot = self.repository.get_slot(contract_id)
        prompt = self._build_prompt(slot)
        snapshot = {"system": prompt.system, "user": prompt.user}
        context_digest = sha256(
            json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        attempt = self.repository.start_generation_attempt(
            slot.id,
            prompt_snapshot=snapshot,
            context_digest=context_digest,
            retry_of_attempt_id=retry_attempt_id,
        )
        attempt_id = attempt["id"]
        terminal = False
        try:
            yield {
                "type": "started",
                "attempt_id": attempt_id,
                "retry_of_attempt_id": attempt["retry_of_attempt_id"],
                "contract_id": slot.id,
                "level": slot.level.value,
            }
            async for chunk in self.llm_service.stream_generate(prompt, GenerationConfig(temperature=0.7)):
                text = str(chunk or "")
                if text:
                    self.repository.append_generation_attempt_delta(attempt_id, text)
                    yield {"type": "delta", "attempt_id": attempt_id, "text": text, "level": slot.level.value}
            persisted = self.repository.get_generation_attempt(attempt_id)
            drafted = self._save_generated(slot, persisted["accumulated_text"])
            completed = self.repository.complete_generation_attempt(
                attempt_id, draft_revision=drafted.draft.revision if drafted.draft else 0
            )
            terminal = True
            yield {
                "type": "completed",
                "attempt_id": attempt_id,
                "contract_id": drafted.id,
                "level": drafted.level.value,
                "revision": completed["draft_revision"],
                "payload": drafted.draft.payload.canonical_dict() if drafted.draft else {},
            }
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            if isinstance(exc, (asyncio.CancelledError, GeneratorExit)):
                self.repository.cancel_generation_attempt(attempt_id)
                terminal = True
                raise
            self.repository.fail_generation_attempt(attempt_id, str(exc))
            terminal = True
            yield {"type": "error", "attempt_id": attempt_id, "message": str(exc), "level": slot.level.value}
        finally:
            if not terminal:
                self.repository.cancel_generation_attempt(attempt_id)

    def _build_prompt(self, slot: OutlineContractSlot) -> Prompt:
        parent_context = self._published_parent_context(slot)
        sibling_context = self._previous_sibling_context(slot)
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
            f"上一同级已发布交接：{json.dumps(sibling_context, ensure_ascii=False, sort_keys=True)}\n\n"
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

    def _previous_sibling_context(self, slot: OutlineContractSlot) -> dict[str, Any]:
        previous = self.repository.previous_synced_sibling(slot.id)
        if previous is None:
            return {}
        return {
            "level": previous.level.value,
            "contract_id": previous.id,
            "revision": previous.revision,
            "digest": previous.published_digest or previous.digest,
            "chapter_end": previous.payload.chapter_end,
            "exit_state": previous.payload.exit_state,
            "state_changes": previous.payload.state_changes,
            "handoff_conditions": previous.payload.handoff_conditions,
            "ending_hook": previous.payload.ending_hook,
        }

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
