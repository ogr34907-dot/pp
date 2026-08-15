from types import SimpleNamespace

import pytest

from application.engine.services.context_lifecycle import (
    DEFAULT_PHASE_THRESHOLDS,
    build_lifecycle_directive,
    classify_phase,
    estimate_total_chapters,
    get_phase_directives,
    load_phase_thresholds,
)
from engine.core.entities.story import StoryPhase


class FakeRegistry:
    def __init__(self, fields=None, directives=None):
        self.fields = fields or {}
        self.directives = directives or {}

    def get_field(self, _prompt_id, key, default=None):
        return self.fields.get(key, default)

    def get_directives_dict(self, _prompt_id, directives_key="_directives"):
        return self.directives


class FakeNovelRepository:
    def __init__(self, target_chapters):
        self.target_chapters = target_chapters

    def get_by_id(self, _novel_id):
        return SimpleNamespace(target_chapters=self.target_chapters)

def test_total_chapters_comes_only_from_persisted_novel_target():
    assert estimate_total_chapters(FakeNovelRepository(120), "novel-1") == 120


@pytest.mark.parametrize("target_chapters", [None, 0, True, 1.5, "invalid"])
def test_total_chapters_rejects_invalid_persisted_novel_target(target_chapters):
    with pytest.raises(ValueError, match="target_chapters"):
        estimate_total_chapters(FakeNovelRepository(target_chapters), "novel-1")


def test_phase_thresholds_and_classification_are_configurable():
    thresholds = load_phase_thresholds(
        FakeRegistry(fields={"_phase_thresholds": {"opening": 0.2, "convergence": 0.8}}),
        "prompt",
        DEFAULT_PHASE_THRESHOLDS,
    )

    assert thresholds["opening"] == 0.2
    assert thresholds["convergence"] == 0.8
    assert classify_phase(0.1, thresholds) == StoryPhase.OPENING
    assert classify_phase(0.2, thresholds) == StoryPhase.DEVELOPMENT
    assert classify_phase(0.81, thresholds) == StoryPhase.CONVERGENCE


def test_build_lifecycle_directive_renders_directive_and_extra():
    directive = build_lifecycle_directive(
        target_chapters=100,
        chapter_number=92,
        thresholds=DEFAULT_PHASE_THRESHOLDS,
        registry=FakeRegistry(
            fields={"_convergence_extra": "还剩 {remaining} 章，收束所有承诺。\n"},
            directives={"CONVERGENCE": "开始汇流"},
        ),
        prompt_id="prompt",
    )

    assert "开始汇流" in directive
    assert "第 92 章 / 约 100 章" in directive
    assert "还剩 8 章" in directive


def test_get_phase_directives_ignores_unknown_keys():
    directives = get_phase_directives(
        FakeRegistry(directives={"OPENING": "开局", "UNKNOWN": "nope"}),
        "prompt",
    )

    assert directives == {StoryPhase.OPENING: "开局"}
