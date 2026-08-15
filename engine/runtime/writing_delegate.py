"""StoryPipeline 写作阶段委托

环境变量 PLOTPILOT_USE_STORY_PIPELINE:
  - 未设置（默认）/ writing / 1 / true: StoryPipeline 写作
  - full / all / engine: StoryPipeline 写作（与 writing 等价）
  - off / legacy / false / 0: legacy 节拍写作（紧急回退）

生产入口统一为 EngineDaemon（Phase 9）。
"""
from __future__ import annotations

import logging
import time
import hashlib
from typing import Any, Dict, Literal

from infrastructure.engine.story_pipeline_environment import (
    STORY_PIPELINE_MODE_ENV,
    StoryPipelineEnvironmentSettings,
)
from engine.runtime.daemon_host import (
    _candidate_first_authority_blocks_completed_write,
    _pause_for_candidate_first_authority,
)

logger = logging.getLogger(__name__)

PipelineMode = Literal["off", "writing", "full"]


def get_story_pipeline_mode() -> PipelineMode:
    """解析引擎内核模式（未设置时默认 writing，Phase 4）"""
    settings = StoryPipelineEnvironmentSettings.from_env()
    if settings.is_unknown:
        logger.warning(
            "未知 %s=%r，使用默认 writing；回退 legacy 请设 off",
            STORY_PIPELINE_MODE_ENV,
            settings.raw_mode,
        )
    return settings.mode


def story_pipeline_mode_was_unset() -> bool:
    return StoryPipelineEnvironmentSettings.from_env().is_unset


def is_story_pipeline_writing_enabled() -> bool:
    """写作阶段是否走新内核（4a 或 4b）"""
    return get_story_pipeline_mode() in ("writing", "full")


def _build_runner(daemon: Any):
    from engine.runtime.runner import StoryPipelineRunner

    return StoryPipelineRunner(
        novel_repository=daemon.novel_repository,
        llm_service=daemon.llm_service,
        context_builder=daemon.context_builder,
        background_task_service=daemon.background_task_service,
        planning_service=daemon.planning_service,
        story_node_repo=daemon.story_node_repo,
        chapter_repository=daemon.chapter_repository,
        poll_interval=daemon.poll_interval,
        voice_drift_service=daemon.voice_drift_service,
        circuit_breaker=daemon.circuit_breaker,
        chapter_workflow=daemon.chapter_workflow,
        aftermath_pipeline=daemon.aftermath_pipeline,
        volume_summary_service=daemon.volume_summary_service,
        foreshadowing_repository=daemon.foreshadowing_repository,
        knowledge_service=daemon.knowledge_service,
    )


def _get_story_pipeline_commit_repository(runner: Any):
    db = getattr(getattr(runner, "chapter_repository", None), "db", None)
    if db is None:
        return None
    from infrastructure.persistence.database.sqlite_chapter_narrative_commit_repository import (
        SqliteChapterNarrativeCommitRepository,
    )

    return SqliteChapterNarrativeCommitRepository(db)


def _apply_story_pipeline_advance(novel: Any, advance: Any) -> None:
    from domain.novel.entities.novel import NovelStage

    novel.current_auto_chapters = int(advance.current_auto_chapters)
    novel.current_chapter_in_act = int(advance.current_chapter_in_act)
    novel.current_beat_index = 0
    novel.beats_completed = False
    stage = str(getattr(advance, "current_stage", "") or "auditing")
    try:
        novel.current_stage = NovelStage(stage)
    except ValueError:
        novel.current_stage = stage


def _durable_chapter_stats(
    daemon: Any,
    novel_id: str,
    *,
    chapter_number: int | None = None,
) -> Dict[str, Any]:
    reader = getattr(daemon, "_read_chapter_stats_ephemeral", None)
    if not callable(reader):
        return {}
    try:
        stats = reader(novel_id)
    except Exception as exc:
        logger.debug("[%s] 读取章节统计失败，保留现有共享状态: %s", novel_id, exc)
        return {}
    if not isinstance(stats, (tuple, list)) or len(stats) != 3:
        return {}
    completed, manuscript, total_words = (int(value or 0) for value in stats)
    fields: Dict[str, Any] = {
        "_cached_completed_chapters": completed,
        "_cached_manuscript_chapters": manuscript,
        "_cached_total_words": total_words,
    }
    if chapter_number is not None:
        fields["_cached_current_chapter_number"] = int(chapter_number)
    return fields


def _canonical_failure_publication(
    daemon: Any,
    runner: Any,
    novel_id: str,
    *,
    fallback_chapter_number: int | None,
    fallback_reason: str,
) -> Dict[str, Any]:
    from application.engine.services.canonical_aftermath_recovery import (
        resolve_canonical_aftermath_status,
    )

    db = getattr(getattr(runner, "chapter_repository", None), "db", None)
    durable = resolve_canonical_aftermath_status(novel_id, database=db)
    chapter_number = (
        durable.chapter_number
        if durable is not None and durable.chapter_number is not None
        else fallback_chapter_number
    )
    failure_reason = (
        durable.failure_reason
        if durable is not None and durable.failure_reason
        else fallback_reason
    )
    fields: Dict[str, Any] = {
        "canonical_aftermath_chapter_number": chapter_number,
        "canonical_aftermath_failure_reason": failure_reason,
    }
    if chapter_number is not None:
        fields["current_chapter_number"] = int(chapter_number)
    fields.update(
        _durable_chapter_stats(
            daemon,
            novel_id,
            chapter_number=chapter_number,
        )
    )
    return fields


def _canonical_ready_publication(
    daemon: Any,
    novel_id: str,
    *,
    chapter_number: int,
) -> Dict[str, Any]:
    fields: Dict[str, Any] = {
        "autopilot_pause_reason": "",
        "canonical_aftermath_chapter_number": None,
        "canonical_aftermath_failure_reason": "",
        "requires_ai_review": False,
        "has_active_invocation": False,
    }
    fields.update(
        _durable_chapter_stats(
            daemon,
            novel_id,
            chapter_number=chapter_number,
        )
    )
    return fields


def _pause_for_story_pipeline_advance_failure(
    daemon: Any,
    novel: Any,
    novel_id: str,
    reason: str,
) -> None:
    from domain.novel.entities.novel import NovelStage

    novel.current_stage = NovelStage.PAUSED_FOR_REVIEW
    novel.last_audit_narrative_ok = False
    daemon._update_shared_state(
        novel_id,
        current_stage=NovelStage.PAUSED_FOR_REVIEW.value,
        last_audit_narrative_ok=False,
        autopilot_pause_reason=reason,
    )
    daemon._flush_novel(novel)


def _candidate_first_authority_exists(host: Any, novel_id: str) -> bool:
    database = getattr(getattr(host, "chapter_repository", None), "db", None)
    return _candidate_first_authority_blocks_completed_write(database, novel_id)


async def run_writing(host: Any, novel: Any) -> None:
    """写作阶段统一入口 — 按 host 配置或环境变量选择新/旧管线"""
    novel_id = str(getattr(getattr(novel, "novel_id", ""), "value", novel.novel_id))
    if _candidate_first_authority_exists(host, novel_id):
        _pause_for_candidate_first_authority(host, novel, novel_id)
        return
    if getattr(host, "use_story_pipeline_for_writing", False):
        await run_story_pipeline_writing(host, novel)
        return
    from engine.runtime.legacy_writing_delegate import run_legacy_writing

    await run_legacy_writing(host, novel)


async def run_story_pipeline_writing(daemon: Any, novel: Any) -> None:
    """执行单章写作（新管线），并同步 novel 状态到 daemon 模型"""
    from domain.novel.entities.novel import NovelStage
    from engine.pipelines.registry import get_pipeline_registry
    from application.world.services.chapter_narrative_sync import (
        CHAPTER_NARRATIVE_PIPELINE_VERSION,
    )

    novel_id = novel.novel_id.value if hasattr(novel.novel_id, "value") else str(novel.novel_id)
    runner = _build_runner(daemon)
    target_words = int(getattr(novel, "target_words_per_chapter", None) or runner.DEFAULT_TARGET_WORDS)
    genre = (getattr(novel, "genre", "") or "").strip().lower()
    commit_repository = _get_story_pipeline_commit_repository(runner)

    logger.info("[%s] StoryPipeline 写作模式 genre=%s", novel_id, genre or "(default)")

    if commit_repository is None:
        _pause_for_story_pipeline_advance_failure(
            daemon,
            novel,
            novel_id,
            "required_narrative_memory_unavailable:canonical_commit_repository",
        )
        return

    try:
        recovered_advances = commit_repository.recover_pending_story_pipeline_advances(
            novel_id=novel_id,
            pipeline_version=CHAPTER_NARRATIVE_PIPELINE_VERSION,
            require_memory_sync=True,
        )
    except Exception as exc:
        _pause_for_story_pipeline_advance_failure(
            daemon,
            novel,
            novel_id,
            f"story_pipeline_advance_recovery_failed:{exc}",
        )
        return

    if recovered_advances:
        recovery = recovered_advances[-1]
        if recovery.disposition not in {"applied", "already_applied"}:
            _pause_for_story_pipeline_advance_failure(
                daemon,
                novel,
                novel_id,
                "story_pipeline_advance_recovery_failed:"
                f"{recovery.disposition}:{recovery.failure_reason}",
            )
            return
        _apply_story_pipeline_advance(novel, recovery)
        daemon._update_shared_state(
            novel_id,
            writing_substep="pipeline_done",
            writing_substep_label="恢复已提交章节的审计",
            current_chapter_number=recovery.chapter_number,
            audit_aftermath_reused=True,
            audit_aftermath_rebuilt=False,
            **_canonical_ready_publication(
                daemon,
                novel_id,
                chapter_number=recovery.chapter_number,
            ),
        )
        daemon._flush_novel(novel)
        logger.info(
            "[%s] StoryPipeline 恢复已提交第%s章的状态推进",
            novel_id,
            recovery.chapter_number,
        )
        return

    def _writing_sink(substep: str, label: str, extra: Dict[str, Any]) -> None:
        merged = dict(extra)
        nw = merged.get("story_pipeline_wave_index")
        try:
            import sys

            shared = sys.modules.get("__shared_state")
            if shared is not None and nw is not None:
                nk = f"novel:{novel_id}"
                prev = dict(shared.get(nk, {}))
                pw = prev.get("story_pipeline_wave_index")
                if pw != nw:
                    merged["story_pipeline_wave_entered_at"] = time.time()
                elif "story_pipeline_wave_entered_at" not in merged and prev.get("story_pipeline_wave_entered_at"):
                    merged["story_pipeline_wave_entered_at"] = prev["story_pipeline_wave_entered_at"]
                # 新的一章开篇：清零事件轨迹
                if substep == "chapter_found" and int(nw) == 1:
                    merged["story_pipeline_events"] = [
                        {
                            "t": time.time(),
                            "wave": nw,
                            "wave_id": merged.get("story_pipeline_wave_id"),
                            "substep": substep,
                            "label": label,
                        }
                    ]
                else:
                    ev = list(prev.get("story_pipeline_events") or [])
                    ev.append(
                        {
                            "t": time.time(),
                            "wave": nw,
                            "wave_id": merged.get("story_pipeline_wave_id"),
                            "substep": substep,
                            "label": label,
                        }
                    )
                    merged["story_pipeline_events"] = ev[-32:]
            elif nw is not None:
                merged.setdefault("story_pipeline_wave_entered_at", time.time())
        except Exception:
            if nw is not None:
                merged.setdefault("story_pipeline_wave_entered_at", time.time())

        daemon._update_shared_state(
            novel_id,
            writing_substep=substep,
            writing_substep_label=label,
            **merged,
        )

    generation_prefs = getattr(novel, "generation_prefs", None)
    locked_genre = str(
        getattr(generation_prefs, "locked_genre", getattr(novel, "genre", "")) or ""
    ).strip()
    ctx = runner._make_context(
        novel_id=novel_id,
        target_word_count=target_words,
        phase=runner._get_novel_phase(novel),
        auto_approve_mode=getattr(novel, "auto_approve_mode", False),
        genre=locked_genre,
        era=getattr(novel, "era", "ancient"),
    )
    ctx.writing_progress_sink = _writing_sink

    pipeline = get_pipeline_registry().create_pipeline(genre)
    result = await pipeline.run_chapter(ctx)

    if not result.success and result.error == "candidate_first_required":
        _pause_for_candidate_first_authority(daemon, novel, novel_id)
        logger.info("[%s] StoryPipeline formal write blocked by Candidate-first", novel_id)
        return

    if not result.success and result.error == "awaiting_ai_review":
        novel.current_stage = NovelStage.PAUSED_FOR_REVIEW
        daemon._flush_novel(novel)
        logger.info("[%s] StoryPipeline 等待 AI Invocation 审阅", novel_id)
        return

    if not result.success and result.error == "interrupted":
        try:
            from application.engine.services.autopilot_recovery_policy import AutopilotRecoveryPolicy
            from application.engine.services.chapter_generation_workspace import ChapterGenerationWorkspace

            policy = AutopilotRecoveryPolicy(workspace=ChapterGenerationWorkspace())
            decision = policy.decide_on_daemon_tick(novel)
            policy.apply_transient_cleanup(decision)
        except Exception:
            pass
        daemon._update_shared_state(
            novel_id,
            writing_substep="interrupted",
            writing_substep_label="正文生成已中断，等待重试",
            current_stage="writing",
        )
        if daemon._is_still_running(novel):
            novel.current_stage = NovelStage.WRITING
            daemon._flush_novel(novel)
        logger.info("[%s] StoryPipeline 正文生成中断，未提交正式章节", novel_id)
        return

    error = result.error or "unknown"
    if not result.success and (
        error == "canonical_aftermath_not_ready"
        or error.startswith("canonical_history_")
        or error.startswith("required_narrative_memory_unavailable:")
        or error.startswith("required_context_build_failed:")
        or error.startswith("required_auxiliary_state_sync_failed:")
    ):
        novel.current_stage = NovelStage.PAUSED_FOR_REVIEW
        novel.last_audit_narrative_ok = False
        canonical_fields: Dict[str, Any] = {}
        if error == "canonical_aftermath_not_ready":
            canonical_fields = _canonical_failure_publication(
                daemon,
                runner,
                novel_id,
                fallback_chapter_number=(
                    result.chapter_number or getattr(ctx, "chapter_number", None)
                ),
                fallback_reason=error,
            )
        daemon._update_shared_state(
            novel_id,
            current_stage=NovelStage.PAUSED_FOR_REVIEW.value,
            last_audit_narrative_ok=False,
            autopilot_pause_reason=error,
            **canonical_fields,
        )
        daemon._flush_novel(novel)
        logger.warning("[%s] StoryPipeline 因规范记忆未提交而暂停: %s", novel_id, error)
        return

    if result.success:
        chapter_num = result.chapter_number or ctx.chapter_number
        advance = commit_repository.advance_story_pipeline_once(
            novel_id=novel_id,
            chapter_number=chapter_num,
            pipeline_version=CHAPTER_NARRATIVE_PIPELINE_VERSION,
            require_memory_sync=True,
        )
        if advance.disposition not in {"applied", "already_applied"}:
            _pause_for_story_pipeline_advance_failure(
                daemon,
                novel,
                novel_id,
                "story_pipeline_advance_failed:"
                f"{advance.disposition}:{advance.failure_reason}",
            )
            return
        if getattr(result, "audit_snapshot", None):
            pending = getattr(daemon, "_pending_story_pipeline_aftermath", None)
            if pending is not None:
                content = result.content or ""
                pending[(novel_id, chapter_num)] = {
                    **dict(result.audit_snapshot),
                    "chapter_number": chapter_num,
                    "tension_composite": result.tension,
                    "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    "source": "story_pipeline",
                    "reused": False,
                }
        _apply_story_pipeline_advance(novel, advance)
        if result.tension:
            novel.last_chapter_tension = result.tension

        daemon._update_shared_state(
            novel_id,
            writing_substep="pipeline_done",
            writing_substep_label="审计准备",
            current_chapter_number=chapter_num,
            last_chapter_tension=result.tension,
            audit_aftermath_reused=False,
            audit_aftermath_rebuilt=False,
            **_canonical_ready_publication(
                daemon,
                novel_id,
                chapter_number=chapter_num,
            ),
        )
        daemon._flush_novel(novel)
        logger.info(
            "[%s] StoryPipeline 完成：第%s章 %s字 张力%s",
            novel_id,
            chapter_num,
            result.word_count,
            result.tension,
        )
        return

    if "所有章节已写完" in error:
        logger.info("[%s] StoryPipeline：当前幕章节已全部写完", novel_id)
        if await daemon._current_act_fully_written(novel):
            novel.current_act = (novel.current_act or 0) + 1
            novel.current_chapter_in_act = 0
            novel.current_stage = NovelStage.ACT_PLANNING
            daemon._update_shared_state(
                novel_id,
                current_stage="act_planning",
                writing_substep="act_planning",
                writing_substep_label="幕级规划",
            )
        else:
            # Pipeline 没找到下一章，但当前幕又没有达到“已全部完成”的条件。
            # 这通常表示章节规划节点缺失或刚被并发清理，不应跳到下一幕；
            # 回到本幕规划，让引擎补齐当前幕章节。
            novel.current_chapter_in_act = 0
            novel.current_stage = NovelStage.ACT_PLANNING
            daemon._update_shared_state(
                novel_id,
                current_stage="act_planning",
                writing_substep="act_planning",
                writing_substep_label="幕级规划",
            )
        daemon._flush_novel(novel)
        return

    novel.consecutive_error_count = (getattr(novel, "consecutive_error_count", 0) or 0) + 1
    daemon._flush_novel(novel)
    logger.error("[%s] StoryPipeline 写作失败: %s", novel_id, error)
