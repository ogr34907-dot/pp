import pytest
from fastapi import HTTPException

from interfaces.api.v1.engine.dag import dag_routes


@pytest.fixture(autouse=True)
def clear_dag_cache():
    dag_routes._dag_cache.clear()
    yield
    dag_routes._dag_cache.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("control", "args"),
    [
        (dag_routes.toggle_node, ("novel-1", "exec_writer")),
        (
            dag_routes.update_node_config,
            ("novel-1", "exec_writer"),
        ),
        (dag_routes.run_dag, ("novel-1",)),
        (dag_routes.stop_dag, ("novel-1",)),
    ],
)
async def test_dag_mutation_controls_are_retired_before_loading_a_definition(
    monkeypatch,
    control,
    args,
):
    monkeypatch.setattr(
        dag_routes,
        "_get_dag_for_novel",
        lambda _novel_id: (_ for _ in ()).throw(
            AssertionError("retired controls must not load a mutable DAG")
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        await control(*args)

    assert exc_info.value.status_code == 410
    assert exc_info.value.detail == "candidate_generation_control_required"


def test_dag_display_ignores_historical_mutable_definitions(monkeypatch):
    monkeypatch.setattr(
        dag_routes,
        "DAGVersionManager",
        lambda: (_ for _ in ()).throw(
            AssertionError("candidate display must use the protected default DAG")
        ),
        raising=False,
    )

    dag = dag_routes._get_dag_for_novel("novel-1")

    assert dag.id == dag_routes.get_default_dag().id
    assert dag.nodes == dag_routes.get_default_dag().nodes
