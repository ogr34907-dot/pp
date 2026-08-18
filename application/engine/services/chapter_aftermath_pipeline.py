"""章节保存后的统一管线：叙事落库、向量检索、文风、图谱推断与后台抽取。

供 HTTP 保存、托管连写、自动驾驶审计复用，避免：
- 索引用正文截断 vs 叙事层用 LLM 总结 两套逻辑；
- 文风既入队 VOICE_ANALYSIS 又同步 score_chapter 重复计算。

关键路径只保留下一章/审计立即需要的产物：
1. 章间桥段资产；
2. 分章叙事同步 + 张力评分 + 向量/伏笔/三元组/因果边/人物状态/债务；
3. 文风评分（若调用方已在同一正文上评分，则复用结果）。

其余护栏快照、治理提交、演进/世界线快照、道具同步、汇流点检查等
作为串行辅助阶段后台执行，避免写密集尾部工作阻塞下一章。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Dict, List, Optional, TYPE_CHECKING

from domain.ai.services.llm_service import LLMService
from application.ai.structured_json_pipeline import _retry_delay_seconds

if TYPE_CHECKING:
    from application.world.services.knowledge_service import KnowledgeService

logger = logging.getLogger(__name__)


async def _timed_aftermath_stage(
    stage: str,
    novel_id: str,
    chapter_number: int,
    work: Callable[[], Awaitable[Any]],
) -> Any:
    started = time.perf_counter()
    try:
        return await work()
    finally:
        elapsed = time.perf_counter() - started
        logger.info(
            "章后阶段完成 stage=%s novel=%s ch=%s elapsed=%.2fs",
            stage,
            novel_id,
            chapter_number,
            elapsed,
        )


class _SerializedAuxiliaryQueue:
    """Run non-critical chapter aftermath side effects off the critical path.

    Jobs are chained per event loop instead of launched concurrently. This keeps
    SQLite write-heavy auxiliary work from blocking the next chapter while also
    avoiding a thundering herd of background writes.
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tail: asyncio.Task[Any] | None = None

    def enqueue(
        self,
        stage: str,
        novel_id: str,
        chapter_number: int,
        work: Callable[[], Awaitable[Any]],
    ) -> asyncio.Task[Any]:
        loop = asyncio.get_running_loop()
        previous = self._tail if self._loop is loop and self._tail and not self._tail.done() else None
        self._loop = loop
        self._tail = loop.create_task(
            self._run_after(previous, stage, novel_id, chapter_number, work)
        )
        return self._tail

    async def drain(self) -> None:
        tail = self._tail
        if tail:
            await tail

    async def _run_after(
        self,
        previous: asyncio.Task[Any] | None,
        stage: str,
        novel_id: str,
        chapter_number: int,
        work: Callable[[], Awaitable[Any]],
    ) -> None:
        if previous is not None:
            try:
                await previous
            except Exception:
                # Previous jobs log their own failure; keep the chain alive.
                pass
        try:
            await _timed_aftermath_stage(stage, novel_id, chapter_number, work)
        except Exception as e:
            logger.warning(
                "章后辅助阶段失败 stage=%s novel=%s ch=%s: %s",
                stage,
                novel_id,
                chapter_number,
                e,
            )
            raise


_AUXILIARY_QUEUE = _SerializedAuxiliaryQueue()


async def infer_kg_from_chapter(novel_id: str, chapter_number: int) -> None:
    """结构树章节节点 → 知识图谱增量推断（与 HTTP 原 _try_infer_kg_chapter 一致）。"""
    try:
        from application.paths import get_db_path
        from infrastructure.persistence.database.connection import get_database
        from infrastructure.persistence.database.sqlite_knowledge_repository import SqliteKnowledgeRepository
        from infrastructure.persistence.database.triple_repository import TripleRepository
        from infrastructure.persistence.database.chapter_element_repository import ChapterElementRepository
        from infrastructure.persistence.database.story_node_repository import StoryNodeRepository
        from application.world.services.knowledge_graph_service import KnowledgeGraphService

        db_path = get_db_path()
        kr = SqliteKnowledgeRepository(get_database())
        story_node_id = kr.find_story_node_id_for_chapter_number(novel_id, chapter_number)
        if not story_node_id:
            logger.debug("KG 推断跳过：章节 %d 无故事节点 novel=%s", chapter_number, novel_id)
            return

        kg_service = KnowledgeGraphService(
            TripleRepository(),
            ChapterElementRepository(db_path),
            StoryNodeRepository(db_path),
        )
        triples = await kg_service.infer_from_chapter(story_node_id)
        logger.debug("KG 推断完成 novel=%s ch=%d 新三元组=%d", novel_id, chapter_number, len(triples))
    except Exception as e:
        logger.warning("KG 推断失败 novel=%s ch=%d: %s", novel_id, chapter_number, e)


class ChapterAftermathPipeline:
    """章节保存后分析与落库的统一入口。

    V8 Feed-forward 升级：集成因果边提取、人物状态突变评估、叙事债务更新。
    """

    def __init__(
        self,
        knowledge_service: "KnowledgeService",
        chapter_indexing_service: Any,
        llm_service: LLMService,
        voice_drift_service: Any = None,
        triple_repository: Any = None,
        foreshadowing_repository: Any = None,
        storyline_repository: Any = None,
        chapter_repository: Any = None,
        plot_arc_repository: Any = None,
        narrative_event_repository: Any = None,
        # V8 Feed-forward: 新增仓储
        causal_edge_repository: Any = None,
        character_state_repository: Any = None,
        debt_repository: Any = None,
        bible_repository: Any = None,
        unified_checkpoint_service: Any = None,
        prop_lifecycle_syncer: Any = None,
        evolution_snapshot_service: Any = None,
        character_narrative_kernel: Any = None,
        memory_engine: Any = None,
    ) -> None:
        self._knowledge = knowledge_service
        self._indexing = chapter_indexing_service
        self._llm = llm_service
        self._voice = voice_drift_service
        self._triple_repository = triple_repository
        self._foreshadowing_repository = foreshadowing_repository
        self._storyline_repository = storyline_repository
        self._chapter_repository = chapter_repository
        self._plot_arc_repository = plot_arc_repository
        self._narrative_event_repository = narrative_event_repository
        # V8 Feed-forward: 因果图谱 / 人物状态机 / 叙事债务
        self._causal_edge_repository = causal_edge_repository
        self._character_state_repository = character_state_repository
        self._debt_repository = debt_repository
        self._bible_repository = bible_repository
        self._unified_checkpoint = unified_checkpoint_service
        self._prop_syncer = prop_lifecycle_syncer
        self._evolution_snapshot_service = evolution_snapshot_service
        self._character_kernel = character_narrative_kernel
        self._memory_engine = memory_engine
        self._auxiliary_tasks: set[asyncio.Task[Any]] = set()
        if hasattr(self._memory_engine, "llm_service") and getattr(
            self._memory_engine, "llm_service", None
        ) is None:
            self._memory_engine.llm_service = llm_service

    async def run_after_chapter_saved(
        self,
        novel_id: str,
        chapter_number: int,
        content: str,
        chapter_micro_beats: Optional[List[Dict[str, Any]]] = None,
        voice_result: Optional[Dict[str, Any]] = None,
        expected_content_sha256: Optional[str] = None,
        expected_content_revision: Optional[int] = None,
        outline: str = "",
    ) -> Dict[str, Any]:
        """保存正文后执行完整管线。返回文风结果供托管/审计门控使用。

        三元组与伏笔、故事线、张力、对话、因果边、人物状态、债务
        已在 narrative_sync 单次 LLM 中落库。
        """
        out: Dict[str, Any] = {
            "drift_alert": False,
            "similarity_score": None,
            "narrative_sync_ok": False,
            "vector_stored": False,
            "foreshadow_stored": False,
            "triples_extracted": False,
            "causal_edges_stored": False,
            "character_mutations_stored": False,
            "debt_updated": False,
            "bridge_extracted": False,
            "guardrail_passed": None,
            "guardrail_score": None,
            "evolution_snapshot_ok": False,
            "evolution_snapshot_id": None,
            "character_reconcile_ok": False,
            "character_reconcile": None,
            "memory_engine_ok": None,
            "memory_engine_new_beats": 0,
            "memory_engine_new_clues": 0,
        }
        durable_memory_sync = (
            self._memory_engine is not None
            and getattr(self._chapter_repository, "db", None) is not None
        )

        if not self._is_current_content_version(
            novel_id,
            chapter_number,
            content,
            expected_content_sha256=expected_content_sha256,
            expected_content_revision=expected_content_revision,
        ):
            out.update(
                {
                    "discarded_stale": True,
                    "failure_reason": "source_version_mismatch",
                    "content_sha256": expected_content_sha256 or "",
                    "content_revision": expected_content_revision or 0,
                }
            )
            logger.info(
                "discard stale aftermath job novel=%s ch=%s",
                novel_id,
                chapter_number,
            )
            return out

        if not self._is_canonical_chapter(novel_id, chapter_number):
            out.update(
                {
                    "discarded_uncommitted": True,
                    "failure_reason": "candidate_first_required",
                }
            )
            logger.info(
                "discard uncommitted aftermath job novel=%s ch=%s",
                novel_id,
                chapter_number,
            )
            return out

        if not content or not str(content).strip():
            logger.debug("aftermath 跳过：正文为空 novel=%s ch=%s", novel_id, chapter_number)
            return out

        # 1) 叙事 + 向量 + 故事线 + 张力 + 对话 + 因果边 + 人物状态 + 债务
        try:
            from application.world.services.chapter_narrative_sync import (
                sync_chapter_narrative_after_save,
                worldline_rebuild_generation_epoch,
            )

            expected_generation_epoch = worldline_rebuild_generation_epoch(novel_id)

            async def _sync_narrative() -> Dict[str, Any]:
                sync_kwargs: Dict[str, Any] = {}
                if expected_content_sha256 is not None:
                    sync_kwargs["expected_content_sha256"] = expected_content_sha256
                if expected_content_revision is not None:
                    sync_kwargs["expected_content_revision"] = expected_content_revision
                if durable_memory_sync:
                    sync_kwargs["require_memory_sync"] = True
                return await sync_chapter_narrative_after_save(
                    novel_id,
                    chapter_number,
                    content,
                    self._knowledge,
                    self._indexing,
                    self._llm,
                    triple_repository=self._triple_repository,
                    foreshadowing_repo=self._foreshadowing_repository,
                    storyline_repository=self._storyline_repository,
                    chapter_repository=self._chapter_repository,
                    plot_arc_repository=self._plot_arc_repository,
                    narrative_event_repository=self._narrative_event_repository,
                    causal_edge_repository=self._causal_edge_repository,
                    character_state_repository=self._character_state_repository,
                    debt_repository=self._debt_repository,
                    bible_repository=self._bible_repository,
                    chapter_micro_beats=chapter_micro_beats,
                    **sync_kwargs,
                )

            sync_flags = await _timed_aftermath_stage(
                "narrative_sync",
                novel_id,
                chapter_number,
                _sync_narrative,
            )
            out["narrative_sync_ok"] = bool(sync_flags.get("narrative_sync_ok", False))
            for key in (
                "content_sha256",
                "content_hash",
                "content_revision",
                "pipeline_version",
                "commit_status",
                "failure_reason",
                "attempt_count",
                "vector_status",
                "memory_status",
                "memory_failure_reason",
            ):
                if key in sync_flags:
                    out[key] = sync_flags[key]
            out["vector_stored"] = bool(sync_flags.get("vector_stored"))
            out["foreshadow_stored"] = bool(sync_flags.get("foreshadow_stored"))
            out["triples_extracted"] = bool(sync_flags.get("triples_extracted"))
            out["causal_edges_stored"] = bool(sync_flags.get("causal_edges_stored"))
            out["character_mutations_stored"] = bool(sync_flags.get("character_mutations_stored"))
            out["debt_updated"] = bool(sync_flags.get("debt_updated"))
            # 传递多维张力评分（0-100），供审计流程替代旧式 _score_tension
            out["tension_composite"] = sync_flags.get("tension_composite")
        except Exception as e:
            logger.warning(
                "叙事同步/向量失败 novel=%s ch=%s: %s", novel_id, chapter_number, e
            )

        if not out["narrative_sync_ok"]:
            return out

        # MemoryEngine is part of the writing context, so it must consume the
        # same final chapter version only after the canonical sync has committed.
        if out["narrative_sync_ok"] and self._memory_engine is not None:
            memory_content_sha256 = expected_content_sha256 or str(
                out.get("content_sha256") or out.get("content_hash") or ""
            )
            memory_content_revision = (
                expected_content_revision
                if expected_content_revision is not None
                else out.get("content_revision")
            )
            if not self._is_current_content_version(
                novel_id,
                chapter_number,
                content,
                expected_content_sha256=memory_content_sha256 or None,
                expected_content_revision=memory_content_revision,
            ):
                out.update(
                    {
                        "narrative_sync_ok": False,
                        "discarded_stale": True,
                        "failure_reason": "source_version_mismatch",
                    }
                )
                logger.info(
                    "discard stale memory update novel=%s ch=%s",
                    novel_id,
                    chapter_number,
                )
                return out

            if not durable_memory_sync:
                try:
                    memory_delta = await self._memory_engine.update_from_chapter(
                        novel_id,
                        chapter_number,
                        content,
                        outline,
                    )
                    memory_errors = (
                        list(memory_delta.get("errors") or [])
                        if isinstance(memory_delta, dict)
                        else ["MemoryEngine returned an invalid update result"]
                    )
                    if memory_errors:
                        raise RuntimeError("; ".join(str(error) for error in memory_errors))
                    out["memory_engine_ok"] = True
                    out["memory_engine_new_beats"] = int(
                        memory_delta.get("new_beats", 0)
                    )
                    out["memory_engine_new_clues"] = int(
                        memory_delta.get("new_clues", 0)
                    )
                except Exception as exc:
                    out.update(
                        {
                            "memory_engine_ok": False,
                            "narrative_sync_ok": False,
                            "failure_reason": "memory_engine_update_failed",
                            "memory_engine_error": str(exc),
                        }
                    )
                    logger.warning(
                        "MemoryEngine 回写失败 novel=%s ch=%s: %s",
                        novel_id,
                        chapter_number,
                        exc,
                    )
            elif not memory_content_sha256 or not memory_content_revision:
                out.update(
                    {
                        "memory_engine_ok": False,
                        "narrative_sync_ok": False,
                        "failure_reason": "memory_engine_sync_state_unavailable",
                    }
                )
                return out
            else:
                from application.world.services.chapter_narrative_sync import (
                    CHAPTER_NARRATIVE_PIPELINE_VERSION,
                )
                from infrastructure.persistence.database.sqlite_chapter_narrative_commit_repository import (
                    MAX_MEMORY_SYNC_ATTEMPTS,
                    SqliteChapterNarrativeCommitRepository,
                )

                memory_commit_repository = SqliteChapterNarrativeCommitRepository(
                    self._chapter_repository.db
                )
                memory_kwargs = {
                    "novel_id": novel_id,
                    "chapter_number": chapter_number,
                    "content_sha256": str(memory_content_sha256),
                    "pipeline_version": CHAPTER_NARRATIVE_PIPELINE_VERSION,
                    "content_revision": int(memory_content_revision),
                }
                if expected_generation_epoch is not None:
                    memory_kwargs["expected_generation_epoch"] = (
                        expected_generation_epoch
                    )
                for retry_index in range(MAX_MEMORY_SYNC_ATTEMPTS):
                    try:
                        memory_claim = memory_commit_repository.claim_memory_sync(
                            **memory_kwargs
                        )
                    except Exception as exc:
                        logger.warning(
                            "MemoryEngine 声明状态失败 novel=%s ch=%s: %s",
                            novel_id,
                            chapter_number,
                            exc,
                        )
                        out.update(
                            {
                                "memory_engine_ok": False,
                                "narrative_sync_ok": False,
                                "failure_reason": "memory_engine_sync_state_unavailable",
                                "memory_engine_error": str(exc),
                            }
                        )
                        return out

                    if memory_claim == "reused":
                        out["memory_engine_ok"] = True
                        out["memory_status"] = "committed"
                        break
                    if memory_claim == "in_progress":
                        out.update(
                            {
                                "memory_engine_ok": False,
                                "memory_status": "in_progress",
                                "narrative_sync_ok": False,
                                "failure_reason": "memory_engine_sync_in_progress",
                            }
                        )
                        return out
                    if memory_claim == "exhausted":
                        out.update(
                            {
                                "memory_engine_ok": False,
                                "memory_status": "failed",
                                "narrative_sync_ok": False,
                                "failure_reason": "memory_engine_sync_exhausted",
                            }
                        )
                        return out
                    if memory_claim == "generation_epoch_mismatch":
                        out.update(
                            {
                                "memory_engine_ok": False,
                                "narrative_sync_ok": False,
                                "discarded_stale": True,
                                "failure_reason": "generation_epoch_mismatch",
                            }
                        )
                        return out
                    if memory_claim != "claimed":
                        out.update(
                            {
                                "memory_engine_ok": False,
                                "narrative_sync_ok": False,
                                "discarded_stale": memory_claim == "source_version_mismatch",
                                "failure_reason": (
                                    "source_version_mismatch"
                                    if memory_claim == "source_version_mismatch"
                                    else "memory_engine_sync_state_unavailable"
                                ),
                            }
                        )
                        return out

                    try:
                        canonical_update = getattr(
                            self._memory_engine,
                            "update_canonical_version_from_chapter",
                            None,
                        )
                        if callable(canonical_update):
                            memory_delta = await canonical_update(
                                novel_id,
                                chapter_number,
                                content,
                                outline,
                                content_sha256=str(memory_content_sha256),
                                content_revision=int(memory_content_revision),
                                expected_generation_epoch=expected_generation_epoch,
                            )
                        else:
                            memory_delta = await self._memory_engine.update_from_chapter(
                                novel_id,
                                chapter_number,
                                content,
                                outline,
                            )
                        if isinstance(memory_delta, dict) and memory_delta.get(
                            "discarded_stale"
                        ):
                            out.update(
                                {
                                    "memory_engine_ok": False,
                                    "narrative_sync_ok": False,
                                    "discarded_stale": True,
                                    "failure_reason": "source_version_mismatch",
                                }
                            )
                            return out
                        memory_errors = (
                            list(memory_delta.get("errors") or [])
                            if isinstance(memory_delta, dict)
                            else ["MemoryEngine returned an invalid update result"]
                        )
                        if memory_errors:
                            raise RuntimeError("; ".join(str(error) for error in memory_errors))
                        if not memory_commit_repository.finish_memory_sync(**memory_kwargs):
                            if not self._is_current_content_version(
                                novel_id,
                                chapter_number,
                                content,
                                expected_content_sha256=str(memory_content_sha256),
                                expected_content_revision=int(memory_content_revision),
                            ):
                                out.update(
                                    {
                                        "memory_engine_ok": False,
                                        "narrative_sync_ok": False,
                                        "discarded_stale": True,
                                        "failure_reason": "source_version_mismatch",
                                    }
                                )
                                return out
                            raise RuntimeError("memory_engine_sync_state_commit_failed")
                        out["memory_engine_ok"] = True
                        out["memory_status"] = "committed"
                        out["memory_engine_new_beats"] = int(
                            memory_delta.get("new_beats", 0)
                        )
                        out["memory_engine_new_clues"] = int(
                            memory_delta.get("new_clues", 0)
                        )
                        break
                    except Exception as exc:
                        persisted_failure = memory_commit_repository.fail_memory_sync(
                            **memory_kwargs,
                            failure_reason=str(exc),
                        )
                        if not persisted_failure and not self._is_current_content_version(
                            novel_id,
                            chapter_number,
                            content,
                            expected_content_sha256=str(memory_content_sha256),
                            expected_content_revision=int(memory_content_revision),
                        ):
                            out.update(
                                {
                                    "memory_engine_ok": False,
                                    "narrative_sync_ok": False,
                                    "discarded_stale": True,
                                    "failure_reason": "source_version_mismatch",
                                }
                            )
                            return out
                        if (
                            persisted_failure
                            and retry_index < MAX_MEMORY_SYNC_ATTEMPTS - 1
                        ):
                            delay = _retry_delay_seconds(retry_index)
                            logger.info(
                                "MemoryEngine 回写失败，%.1f 秒后重试 novel=%s ch=%s attempt=%s/%s",
                                delay,
                                novel_id,
                                chapter_number,
                                retry_index + 1,
                                MAX_MEMORY_SYNC_ATTEMPTS,
                            )
                            await asyncio.sleep(delay)
                            continue
                        out.update(
                            {
                                "memory_engine_ok": False,
                                "narrative_sync_ok": False,
                                "failure_reason": "memory_engine_update_failed",
                                "memory_engine_error": str(exc),
                                "memory_status": (
                                    "failed" if persisted_failure else "in_progress"
                                ),
                            }
                        )
                        logger.warning(
                            "MemoryEngine 回写失败 novel=%s ch=%s: %s",
                            novel_id,
                            chapter_number,
                            exc,
                        )

        if not out["narrative_sync_ok"]:
            return out

        # Governance's blocking decision is part of the N -> N+1 barrier.
        # Non-blocking enrichment remains in the serialized auxiliary queue.
        try:
            await self._run_blocking_governance(
                novel_id,
                chapter_number,
                content,
                out,
            )
        except Exception as exc:
            out.update(
                {
                    "narrative_sync_ok": False,
                    "failure_reason": "governance_blocking_decision_failed",
                    "governance_error": str(exc),
                }
            )
            logger.warning(
                "阻断治理决策失败，关闭下一章推进 novel=%s ch=%s: %s",
                novel_id,
                chapter_number,
                exc,
            )
            return out

        # 0) 章间衔接锚点。只有 canonical 与 durable Memory 都 ready 后，
        # 才允许写入下一章会读取的前章桥段资产。
        try:
            await _timed_aftermath_stage(
                "bridge_extract",
                novel_id,
                chapter_number,
                lambda: self._extract_chapter_bridge(novel_id, chapter_number, content),
            )
            out["bridge_extracted"] = True
        except Exception as e:
            logger.warning("章节桥段提取失败 novel=%s ch=%s: %s", novel_id, chapter_number, e)

        # 1b) 角色叙事内核对账：cast plan vs 正文，自动投影状态与风险。
        try:
            if self._character_kernel:
                reconcile = self._character_kernel.reconcile_after_chapter(
                    novel_id,
                    chapter_number,
                    content,
                    None,
                )
                out["character_reconcile_ok"] = bool(reconcile.get("checked"))
                out["character_reconcile"] = reconcile
        except Exception as e:
            logger.warning(
                "角色叙事对账失败 novel=%s ch=%s: %s", novel_id, chapter_number, e
            )

        # 2) 文风（落库 chapter_style_scores）
        # 支持 LLM 模式（异步）和统计模式（同步）
        if voice_result is not None:
            self._apply_voice_result(out, voice_result)
            out["voice_reused"] = True
            logger.debug(
                "复用文风评分 novel=%s ch=%s mode=%s drift=%s",
                novel_id,
                chapter_number,
                out.get("voice_mode"),
                out["drift_alert"],
            )
        elif self._voice:
            try:
                async def _score_voice() -> Dict[str, Any]:
                    if getattr(self._voice, "use_llm_mode", False):
                        return await self._voice.score_chapter_async(
                            novel_id=novel_id,
                            chapter_number=chapter_number,
                            content=content,
                        )
                    return self._voice.score_chapter(
                        novel_id=novel_id,
                        chapter_number=chapter_number,
                        content=content,
                    )

                vr = await _timed_aftermath_stage(
                    "voice_drift",
                    novel_id,
                    chapter_number,
                    _score_voice,
                )
                self._apply_voice_result(out, vr)
                out["voice_reused"] = False
                logger.debug(
                    "文风评分完成 novel=%s ch=%s mode=%s drift=%s",
                    novel_id,
                    chapter_number,
                    out.get("voice_mode"),
                    out["drift_alert"],
                )
            except Exception as e:
                logger.warning("文风评分失败 novel=%s ch=%s: %s", novel_id, chapter_number, e)

        out["auxiliary_deferred"] = True
        self._schedule_auxiliary_stages(
            novel_id,
            chapter_number,
            content,
            dict(out),
            expected_content_sha256=expected_content_sha256,
            expected_content_revision=expected_content_revision,
        )

        return out

    async def _run_blocking_governance(
        self,
        novel_id: str,
        chapter_number: int,
        content: str,
        evidence: Dict[str, Any],
    ) -> None:
        """Persist the one governance decision that controls next-chapter entry."""
        from application.governance.service import NarrativeGovernanceService
        from infrastructure.persistence.database.connection import get_database
        from infrastructure.persistence.database.sqlite_governance_repository import (
            SqliteGovernanceRepository,
        )
        from infrastructure.persistence.database.sqlite_storyline_repository import (
            SqliteStorylineRepository,
        )
        from interfaces.api.dependencies import get_novel_repository

        db = getattr(self._chapter_repository, "db", None) or get_database()
        governance = NarrativeGovernanceService(
            SqliteGovernanceRepository(db),
            get_novel_repository(),
            SqliteStorylineRepository(db),
            db,
        )
        report = await asyncio.to_thread(
            governance.commit_chapter,
            novel_id,
            chapter_number,
            content,
            dict(evidence),
        )
        evidence["governance_report"] = report.to_dict()
        evidence["governance_severity"] = report.severity
        evidence["governance_should_pause"] = report.should_pause_autopilot

    async def ensure_prior_chapters_committed(
        self,
        novel_id: str,
        before_chapter_number: int,
    ) -> Dict[str, Any]:
        """Confirm or rebuild canonical history needed before a later chapter writes.

        A legacy novel with no canonical commit chain may only replay from a
        trusted active checkpoint or a contiguous committed boundary. This
        prevents an automatic continuation from silently issuing an LLM pass
        over the entire book.
        """
        before_chapter_number = int(before_chapter_number or 0)
        if before_chapter_number <= 1:
            return {"ready": True, "replayed_chapters": [], "anchor_chapter": 0}

        repository = self._chapter_repository
        db = getattr(repository, "db", None) if repository is not None else None
        if repository is None or db is None:
            return {
                "ready": False,
                "failure_reason": "canonical_history_rebuild_unavailable",
            }

        try:
            from application.world.services.chapter_narrative_sync import (
                CHAPTER_NARRATIVE_PIPELINE_VERSION,
            )
            from infrastructure.persistence.database.sqlite_chapter_narrative_commit_repository import (
                SqliteChapterNarrativeCommitRepository,
            )

            chapters = sorted(
                (
                    SimpleNamespace(
                        number=int(row["number"] or 0),
                        content=str(row["content"] or ""),
                        content_sha256=str(row["content_sha256"] or ""),
                        content_revision=int(row["content_revision"] or 0),
                        status=str(row["status"] or ""),
                    )
                    for row in db.fetch_all(
                        """
                        SELECT number, content, content_sha256, content_revision, status
                        FROM chapters
                        WHERE novel_id = ? AND number < ? AND status = 'completed'
                        ORDER BY number ASC
                        """,
                        (novel_id, before_chapter_number),
                    )
                ),
                key=lambda chapter: int(getattr(chapter, "number", 0) or 0),
            )
        except Exception as exc:
            logger.warning(
                "读取待确认章节失败 novel=%s before=%s: %s",
                novel_id,
                before_chapter_number,
                exc,
            )
            return {"ready": False, "failure_reason": "canonical_history_unavailable"}

        if not chapters:
            return {"ready": True, "replayed_chapters": [], "anchor_chapter": 0}

        commit_repository = SqliteChapterNarrativeCommitRepository(db)
        require_memory_sync = self._memory_engine is not None

        def _history_is_current(chapter_number: int) -> bool:
            kwargs: Dict[str, Any] = {
                "novel_id": novel_id,
                "chapter_number": chapter_number,
                "pipeline_version": CHAPTER_NARRATIVE_PIPELINE_VERSION,
            }
            if require_memory_sync:
                kwargs["require_memory_sync"] = True
            return commit_repository.is_current_version_ready(**kwargs)

        readiness = {
            int(getattr(chapter, "number", 0) or 0): _history_is_current(
                int(getattr(chapter, "number", 0) or 0)
            )
            for chapter in chapters
        }
        if all(readiness.values()):
            return {
                "ready": True,
                "replayed_chapters": [],
                "anchor_chapter": max(readiness),
            }

        contiguous_anchor = 0
        for chapter in chapters:
            chapter_number = int(getattr(chapter, "number", 0) or 0)
            if chapter_number != contiguous_anchor + 1 or not readiness[chapter_number]:
                break
            contiguous_anchor = chapter_number

        checkpoint_anchor = self._active_history_checkpoint_anchor(
            db,
            novel_id,
            before_chapter_number,
        )
        trusted_anchor = max(contiguous_anchor, checkpoint_anchor)
        if trusted_anchor <= 0:
            return {
                "ready": False,
                "failure_reason": "canonical_history_checkpoint_required",
            }

        replayed_chapters: List[int] = []
        for chapter in chapters:
            chapter_number = int(getattr(chapter, "number", 0) or 0)
            if chapter_number <= trusted_anchor or readiness[chapter_number]:
                continue

            content = str(getattr(chapter, "content", "") or "")
            if not content.strip():
                return {
                    "ready": False,
                    "failure_reason": "canonical_history_content_missing",
                    "chapter_number": chapter_number,
                }
            content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
            content_revision = int(getattr(chapter, "content_revision", 0) or 0)
            outcome = await self.run_after_chapter_saved(
                novel_id,
                chapter_number,
                content,
                expected_content_sha256=content_sha256,
                expected_content_revision=content_revision or None,
            )
            if not isinstance(outcome, dict) or not outcome.get("narrative_sync_ok", False):
                failure_cause = (
                    str(outcome.get("failure_reason") or "")
                    if isinstance(outcome, dict)
                    else ""
                )
                return {
                    "ready": False,
                    "failure_reason": "canonical_history_replay_failed",
                    "failure_cause": failure_cause,
                    "chapter_number": chapter_number,
                }
            if not _history_is_current(chapter_number):
                return {
                    "ready": False,
                    "failure_reason": "canonical_history_replay_uncommitted",
                    "chapter_number": chapter_number,
                }
            replayed_chapters.append(chapter_number)

        return {
            "ready": True,
            "replayed_chapters": replayed_chapters,
            "anchor_chapter": trusted_anchor,
        }

    @staticmethod
    def _active_history_checkpoint_anchor(
        db: Any,
        novel_id: str,
        before_chapter_number: int,
    ) -> int:
        try:
            row = db.fetch_one(
                """
                SELECT MAX(anchor_chapter) AS anchor_chapter
                FROM novel_checkpoints
                WHERE novel_id = ?
                  AND is_active = 1
                  AND branch_name = 'main'
                  AND anchor_chapter > 0
                  AND anchor_chapter < ?
                """,
                (novel_id, before_chapter_number),
            )
            return max(0, int(row["anchor_chapter"] or 0)) if row else 0
        except Exception:
            return 0

    def _is_current_content_version(
        self,
        novel_id: str,
        chapter_number: int,
        content: str,
        *,
        expected_content_sha256: Optional[str],
        expected_content_revision: Optional[int],
    ) -> bool:
        repository = self._chapter_repository
        if repository is None:
            return True
        try:
            import hashlib

            from domain.novel.value_objects.novel_id import NovelId

            current = repository.get_by_novel_and_number(
                NovelId(novel_id), int(chapter_number)
            )
            if current is None:
                return False
            current_hash = hashlib.sha256(
                str(getattr(current, "content", "") or "").encode("utf-8")
            ).hexdigest()
            job_hash = hashlib.sha256((content or "").encode("utf-8")).hexdigest()
            if current_hash != job_hash:
                return False
            if expected_content_sha256 and current_hash != expected_content_sha256:
                return False
            if (
                expected_content_revision is not None
                and int(getattr(current, "content_revision", 0) or 0)
                != int(expected_content_revision)
            ):
                return False
            return True
        except Exception:
            logger.exception(
                "chapter version check failed; discard aftermath novel=%s ch=%s",
                novel_id,
                chapter_number,
            )
            return False

    def _is_canonical_chapter(self, novel_id: str, chapter_number: int) -> bool:
        """Candidate runs admit Canonical work only for their formal commit records."""

        repository = self._chapter_repository
        if repository is None:
            return True
        try:
            from domain.novel.value_objects.novel_id import NovelId

            current = repository.get_by_novel_and_number(
                NovelId(novel_id), int(chapter_number)
            )
            status = str(
                getattr(getattr(current, "status", ""), "value", getattr(current, "status", ""))
            )
            if status != "completed":
                return False
            db = getattr(repository, "db", None)
            if db is None:
                return True
            run = db.fetch_one(
                "SELECT 1 FROM novel_generation_runs WHERE novel_id = ?", (novel_id,)
            )
            if run is None:
                return True
            commit_record = db.fetch_one(
                """
                SELECT chapter_id, content_sha256, content_revision
                FROM chapter_candidate_formal_commits
                WHERE novel_id = ? AND chapter_number = ? AND chapter_id = ?
                """,
                (novel_id, int(chapter_number), str(getattr(current, "id", "") or "")),
            )
            if commit_record is None:
                return False
            actual_sha256 = hashlib.sha256(
                str(getattr(current, "content", "") or "").encode("utf-8")
            ).hexdigest()
            return (
                str(getattr(current, "content_sha256", "") or "") == actual_sha256
                and str(commit_record["content_sha256"] or "") == actual_sha256
                and int(commit_record["content_revision"] or 0)
                == int(getattr(current, "content_revision", 0) or 0)
            )
        except Exception:
            logger.exception(
                "canonical chapter check failed; discard aftermath novel=%s ch=%s",
                novel_id,
                chapter_number,
            )
            return False

    @staticmethod
    def _apply_voice_result(out: Dict[str, Any], result: Dict[str, Any]) -> None:
        out["drift_alert"] = bool(result.get("drift_alert", False))
        out["similarity_score"] = result.get("similarity_score")
        out["voice_mode"] = result.get("mode", "statistics")

    async def drain_auxiliary_stages(self) -> None:
        """Wait for this pipeline's queued auxiliary aftermath work."""
        tasks = tuple(self._auxiliary_tasks)
        if not tasks:
            return

        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        self._auxiliary_tasks.difference_update(tasks)
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                raise outcome

    def _schedule_auxiliary_stages(
        self,
        novel_id: str,
        chapter_number: int,
        content: str,
        evidence: Dict[str, Any],
        *,
        expected_content_sha256: Optional[str] = None,
        expected_content_revision: Optional[int] = None,
    ) -> None:
        async def _job() -> None:
            if not self._is_current_content_version(
                novel_id,
                chapter_number,
                content,
                expected_content_sha256=expected_content_sha256,
                expected_content_revision=expected_content_revision,
            ):
                logger.info(
                    "discard stale auxiliary aftermath novel=%s ch=%s",
                    novel_id,
                    chapter_number,
                )
                return
            await self._run_auxiliary_stages(novel_id, chapter_number, content, evidence)

        task = _AUXILIARY_QUEUE.enqueue(
            "auxiliary_after_chapter",
            novel_id,
            chapter_number,
            _job,
        )
        self._auxiliary_tasks.add(task)

    async def _run_auxiliary_stages(
        self,
        novel_id: str,
        chapter_number: int,
        content: str,
        evidence: Dict[str, Any],
    ) -> None:
        # 3) 结构树 KG 推断
        await infer_kg_from_chapter(novel_id, chapter_number)

        # 4) 质量护栏（建议模式）+ 快照落库 + 溯源（与手动 POST /guardrail/check 同源）
        try:
            import uuid
            from datetime import datetime, timezone

            from application.engine.services.guardrail_execution import run_guardrail_advise_sync
            from engine.core.ports.ports import TraceRecord
            from engine.infrastructure.persistence.trace_store import SqliteTraceStore
            from infrastructure.persistence.database.chapter_guardrail_snapshot_repository import (
                ChapterGuardrailSnapshotRepository,
            )
            from infrastructure.persistence.database.connection import get_database

            t0 = time.perf_counter()
            dto = await asyncio.to_thread(
                run_guardrail_advise_sync,
                novel_id,
                content,
                f"第{chapter_number}章（保存后自动）",
            )
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            evidence["guardrail_passed"] = bool(dto.get("passed"))
            evidence["guardrail_score"] = dto.get("overall_score")

            db = get_database()
            repo = ChapterGuardrailSnapshotRepository(db)
            await asyncio.to_thread(repo.upsert, novel_id, chapter_number, dto)

            vsummary: list[str] = []
            for v in dto.get("violations") or []:
                if isinstance(v, dict) and v.get("description"):
                    vsummary.append(str(v["description"])[:120])
                if len(vsummary) >= 20:
                    break

            store = SqliteTraceStore(db)
            trace = TraceRecord(
                trace_id=str(uuid.uuid4()),
                node_type="guardrail",
                operation="chapter_after_save",
                input_summary=f"{novel_id} ch{chapter_number} len={len(content)}"[:200],
                output_summary=(
                    f"passed={dto.get('passed')} score={dto.get('overall_score')} "
                    f"viol={len(dto.get('violations') or [])}"
                )[:200],
                score=float(dto.get("overall_score") or 0.0),
                violations=vsummary,
                duration_ms=elapsed_ms,
                timestamp=datetime.now(timezone.utc).isoformat(),
                novel_id=novel_id,
            )
            await store.record(trace)
        except Exception as e:
            logger.warning("自动护栏/溯源失败 novel=%s ch=%s: %s", novel_id, chapter_number, e)

        # 5) 故事演进硬状态快照 — 只消费 evidence，不把 read model 当真源
        try:
            if self._evolution_snapshot_service:
                snapshot = await asyncio.to_thread(
                    self._evolution_snapshot_service.build_after_chapter_saved,
                    novel_id,
                    chapter_number,
                    content,
                    "main",
                    dict(evidence),
                )
                evidence["evolution_snapshot_ok"] = snapshot.status == "active"
                evidence["evolution_snapshot_id"] = snapshot.snapshot_id
                logger.debug(
                    "[Evolution] snapshot novel=%s ch=%s id=%s status=%s",
                    novel_id,
                    chapter_number,
                    snapshot.snapshot_id,
                    snapshot.status,
                )
        except Exception as e:
            logger.warning("[Evolution] 快照创建失败（非致命）novel=%s ch=%s: %s", novel_id, chapter_number, e)
            raise

        # 6) 世界线快照 — 章节完成后自动打 CHAPTER checkpoint
        try:
            if self._unified_checkpoint:
                cp_id = await asyncio.to_thread(
                    self._unified_checkpoint.create_checkpoint,
                    novel_id,
                    "CHAPTER",
                    f"第{chapter_number}章自动快照",
                    None,
                    "main",
                    None,
                    {"chapter": chapter_number},
                )
                evidence["worldline_checkpoint_id"] = cp_id
                logger.debug("[Worldline] CHAPTER checkpoint novel=%s ch=%s id=%s", novel_id, chapter_number, cp_id)
        except Exception as e:
            logger.warning("[Worldline] 自动 checkpoint 失败（非致命）novel=%s ch=%s: %s", novel_id, chapter_number, e)

        # 7) 道具生命周期同步 — 事件提取、状态机转换、知识库三元组
        try:
            if self._prop_syncer:
                sync_result = await self._prop_syncer.sync(novel_id, chapter_number, content)
                evidence["prop_sync"] = sync_result
                logger.debug(
                    "[PropSync] 完成 novel=%s ch=%s result=%s",
                    novel_id,
                    chapter_number,
                    sync_result,
                )
        except Exception as e:
            logger.warning(
                "[PropSync] 失败（非致命）novel=%s ch=%s: %s", novel_id, chapter_number, e
            )

        # ── 汇流点到达检查 ──
        try:
            from interfaces.api.dependencies import get_confluence_point_repository as _get_cp_repo
            _confluence_repo = _get_cp_repo()
            _hit_cps = [
                cp for cp in _confluence_repo.get_by_novel_id(novel_id)
                if cp.target_chapter == chapter_number and not cp.resolved
            ]
            if _hit_cps:
                for _cp in _hit_cps:
                    logger.info(
                        "[汇流点] 第%d章完成，汇流点 %s (source=%s → target=%s) 建议标记为 resolved",
                        chapter_number,
                        _cp.id,
                        _cp.source_storyline_id,
                        _cp.target_storyline_id,
                    )
        except Exception as _cp_err:
            logger.warning("汇流点检查失败（非致命）: %s", _cp_err)

    async def _extract_chapter_bridge(
        self,
        novel_id: str,
        chapter_number: int,
        content: str,
    ) -> None:
        """统一章后桥段提取。

        这是保存后管线的衔接端口，而不是某条写作路径的私有后处理。
        ChapterBridgeService 的写入是 upsert，重复调用保持幂等。
        """
        from application.engine.services.chapter_bridge_service import ChapterBridgeService
        from application.paths import get_db_path

        svc = ChapterBridgeService(
            llm_service=self._llm,
            db_path=str(get_db_path()),
        )
        await svc.extract_bridge(novel_id, chapter_number, content)
