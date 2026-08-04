from types import SimpleNamespace

from interfaces.api import dependencies


def test_auto_workflow_holds_context_builders_configured_memory_engine(monkeypatch):
    memory_engine = object()
    context_builder = SimpleNamespace(
        budget_allocator=SimpleNamespace(memory_engine=memory_engine)
    )
    monkeypatch.setattr(dependencies, "get_context_builder", lambda: context_builder)
    for name in (
        "get_consistency_checker",
        "get_storyline_manager",
        "get_plot_arc_repository",
        "get_state_extractor",
        "get_state_updater",
        "get_bible_repository",
        "get_foreshadowing_repository",
        "get_voice_fingerprint_service",
        "get_evolution_gate_service",
    ):
        monkeypatch.setattr(dependencies, name, lambda: object())

    workflow = dependencies.build_auto_workflow(object())

    assert workflow.memory_engine is memory_engine
    assert workflow.context_builder.budget_allocator.memory_engine is memory_engine
