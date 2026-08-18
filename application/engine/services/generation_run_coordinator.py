from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from domain.novel.candidate_chapter import GenerationRun, GenerationRunState, RunMode
from infrastructure.persistence.database.chapter_candidate_repository import (
    ChapterCandidateRepository,
)


class GenerationRunCoordinator:
    """Own one asyncio generation task per novel and generation epoch."""

    def __init__(
        self,
        repository_factory: Callable[[], ChapterCandidateRepository],
        workflow_factory: Callable[[], Any],
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self._repository_factory = repository_factory
        self._workflow_factory = workflow_factory
        self._logger = logger or logging.getLogger(__name__)
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._epochs: dict[str, int] = {}
        self._accepting_claims = True

    @property
    def active_novel_ids(self) -> tuple[str, ...]:
        return tuple(
            novel_id
            for novel_id, task in self._tasks.items()
            if not task.done()
        )

    def claim(self, novel_id: str) -> bool:
        """Claim a durable run if it is in the exact auto-runnable state."""

        if not self._accepting_claims:
            return False

        repository = self._repository_factory()
        try:
            run = repository.get_run(novel_id)
        except KeyError:
            return False

        existing = self._tasks.get(novel_id)
        if existing is not None and not existing.done():
            if self._epochs.get(novel_id) == run.generation_epoch:
                return True

        if not self._is_claimable(run):
            return False

        if existing is not None and not existing.done():
            existing.cancel()

        try:
            task = asyncio.create_task(
                self._run(novel_id, run.generation_epoch),
                name=f"generation-run:{novel_id}:{run.generation_epoch}",
            )
        except RuntimeError:
            self._logger.warning(
                "Generation runner claim skipped without a running event loop: novel=%s",
                novel_id,
            )
            return False
        self._tasks[novel_id] = task
        self._epochs[novel_id] = run.generation_epoch
        task.add_done_callback(self._on_task_done)
        self._logger.info(
            "Generation runner claimed novel=%s epoch=%s",
            novel_id,
            run.generation_epoch,
        )
        return True

    def start_resumable(self) -> int:
        """Claim all continuous runs left at the precise startup resume point."""

        if not self._accepting_claims:
            return 0

        repository = self._repository_factory()
        claimed = 0
        for run in repository.list_resumable_generation_runs():
            if run.run_mode == RunMode.CONTINUOUS and self.claim(run.novel_id):
                claimed += 1
        if claimed:
            self._logger.info("Generation runner startup claimed novels=%s", claimed)
        return claimed

    async def shutdown(self) -> None:
        """Stop new claims, cancel tasks, and await their actual exit."""

        self._accepting_claims = False
        tasks = tuple(self._tasks.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._epochs.clear()
        if tasks:
            self._logger.info("Generation runner shutdown awaited tasks=%s", len(tasks))

    def cancel_forced_shutdown(self) -> None:
        """Best-effort cancellation for process-forced Windows exit."""

        self._accepting_claims = False
        tasks = tuple(self._tasks.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        self._tasks.clear()
        self._epochs.clear()

    async def _run(self, novel_id: str, generation_epoch: int) -> None:
        try:
            workflow = self._workflow_factory()
            await workflow.run_continuously(
                novel_id,
                expected_generation_epoch=generation_epoch,
            )
            self._reconcile_normal_exit(novel_id, generation_epoch)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            reason = f"runner:{type(exc).__name__}:{exc}"
            self._logger.exception(
                "Generation runner failed novel=%s epoch=%s reason=%s",
                novel_id,
                generation_epoch,
                reason,
            )
            try:
                self._repository_factory().record_runner_error(
                    novel_id,
                    expected_generation_epoch=generation_epoch,
                    reason=reason,
                )
            except Exception:
                self._logger.exception(
                    "Generation runner error persistence failed novel=%s epoch=%s",
                    novel_id,
                    generation_epoch,
                )

    def _reconcile_normal_exit(self, novel_id: str, generation_epoch: int) -> None:
        """Never leave a durable ``running`` row without a live runner."""

        try:
            run = self._repository_factory().get_run(novel_id)
        except KeyError:
            return
        if (
            run.generation_epoch == generation_epoch
            and run.state == GenerationRunState.RUNNING
        ):
            reason = "runner_exited_without_terminal_state"
            self._logger.error(
                "Generation runner exited while durable run remained running "
                "novel=%s epoch=%s reason=%s",
                novel_id,
                generation_epoch,
                reason,
            )
            self._repository_factory().record_runner_error(
                novel_id,
                expected_generation_epoch=generation_epoch,
                reason=reason,
            )

    def _on_task_done(self, task: asyncio.Task[None]) -> None:
        for novel_id, current in tuple(self._tasks.items()):
            if current is task:
                self._tasks.pop(novel_id, None)
                self._epochs.pop(novel_id, None)
                break
        if task.cancelled():
            return
        try:
            task.exception()
        except Exception:
            self._logger.exception("Generation runner task completion inspection failed")

    @staticmethod
    def _is_claimable(run: GenerationRun) -> bool:
        return (
            run.run_mode == RunMode.CONTINUOUS
            and run.state == GenerationRunState.RUNNING
            and run.canonical_sync_status == "ready"
            and run.next_action == "generate_candidate"
            and run.current_candidate_id is None
            and run.current_candidate_chapter is None
            and run.current_formal_chapter < run.target_chapters
        )
