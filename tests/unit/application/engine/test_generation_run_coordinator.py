import asyncio

import pytest

from application.engine.services.generation_run_coordinator import GenerationRunCoordinator
from domain.novel.candidate_chapter import GenerationRun, GenerationRunState, RunMode


def _run(state=GenerationRunState.RUNNING):
    return GenerationRun(
        novel_id="novel-1",
        run_mode=RunMode.CONTINUOUS,
        state=state,
        generation_epoch=4,
        target_chapters=10,
        current_formal_chapter=1,
        canonical_sync_status="ready",
        next_action="generate_candidate",
    )


class _Repository:
    def __init__(self):
        self.run = _run()
        self.runner_errors = []

    def get_run(self, _novel_id):
        return self.run

    def list_resumable_generation_runs(self):
        return [self.run]

    def record_runner_error(self, novel_id, *, expected_generation_epoch, reason):
        self.runner_errors.append((novel_id, expected_generation_epoch, reason))
        self.run = _run(GenerationRunState.ERROR)
        return self.run


@pytest.mark.asyncio
async def test_duplicate_claims_share_one_runner_and_normal_exit_is_terminal():
    repository = _Repository()
    release = asyncio.Event()
    calls = 0

    class _Workflow:
        async def run_continuously(self, _novel_id, *, expected_generation_epoch):
            nonlocal calls
            calls += 1
            assert expected_generation_epoch == 4
            await release.wait()
            repository.run = _run(GenerationRunState.WAITING_PLANNING)

    coordinator = GenerationRunCoordinator(lambda: repository, _Workflow)

    assert coordinator.claim("novel-1") is True
    assert coordinator.claim("novel-1") is True
    assert coordinator.active_novel_ids == ("novel-1",)

    release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert calls == 1
    assert repository.runner_errors == []
    assert coordinator.active_novel_ids == ()


@pytest.mark.asyncio
async def test_runner_normal_exit_converts_orphaned_running_state_to_error():
    repository = _Repository()

    class _Workflow:
        async def run_continuously(self, _novel_id, *, expected_generation_epoch):
            assert expected_generation_epoch == 4

    coordinator = GenerationRunCoordinator(lambda: repository, _Workflow)
    assert coordinator.claim("novel-1") is True
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert repository.runner_errors == [
        ("novel-1", 4, "runner_exited_without_terminal_state")
    ]
    assert repository.run.state == GenerationRunState.ERROR
