from infrastructure.ai.generation_profiles import generation_config_from_profile


def test_generation_profiles_define_real_task_budgets_and_timeouts():
    macro = generation_config_from_profile("planning_macro")
    act = generation_config_from_profile("planning_act")
    chapter = generation_config_from_profile("planning_chapter_preplan")
    review = generation_config_from_profile("review_json")

    assert (macro.max_tokens, macro.timeout_seconds) == (16384, 180)
    assert (act.max_tokens, act.timeout_seconds) == (8192, 120)
    assert (chapter.max_tokens, chapter.timeout_seconds) == (4096, 90)
    assert (review.max_tokens, review.timeout_seconds) == (4096, 60)


def test_unknown_generation_profile_keeps_timeout_unspecified():
    config = generation_config_from_profile("not-configured")

    assert config.is_explicit("timeout_seconds") is False
