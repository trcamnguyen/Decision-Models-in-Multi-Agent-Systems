import json
import pytest
from slm_mas.logging import EpisodeLog
from slm_mas.schemas import BudgetState, PilotError, WorkflowState
from slm_mas.state import ArtifactStore


def test_router_truncation_keeps_task_options_and_worker_full_code(task, context, tmp_path):
    state = WorkflowState(task, BudgetState())
    store = ArtifactStore(EpisodeLog(tmp_path, "t", task.task_id))
    code = "def add(a,b):\n" + "    # a long explanatory line of code\n" * 500 + "    return a+b\n"
    art = store.add("code", code, "implementer", 1, None, None)
    state.current_code_id = art.artifact_id
    request = context.routing(state, store)
    assert task.task_text in request.routing_view
    assert request.ordered_legal_actions == ["analyst", "implementer", "repairer", "stop"]
    assert "CODE_EXCERPT_INCOMPLETE" in request.routing_view
    assert request.tokenization_metadata["total_len"] <= context.router_limit
    assert request.tokenization_metadata["dropped_tokens"]["code"] > 0
    full = json.loads(context.worker(state, store, "repairer").messages[1]["content"])
    assert full["current_code"]["content"] == code


def test_mandatory_overflow_never_silent(task, context, tmp_path):
    context.router_limit = 5
    with pytest.raises(PilotError, match="input_overflow"):
        context.routing(WorkflowState(task, BudgetState()), ArtifactStore(EpisodeLog(tmp_path, "t", task.task_id)))


def test_private_sentinels_not_in_model_inputs(task, context, tmp_path):
    private = tmp_path / "private.json"
    private.write_text(json.dumps({"canonical_solution": "HIDDEN_REFERENCE_SENTINEL", "plus_input": "HIDDEN_PLUS_SENTINEL"}))
    state = WorkflowState(task, BudgetState())
    store = ArtifactStore(EpisodeLog(tmp_path / "online", "t", task.task_id))
    router = context.routing(state, store)
    worker = context.worker(state, store, "implementer")
    assert "HIDDEN_" not in router.routing_view + json.dumps(worker.messages)
    assert "canonical_solution" not in router.routing_view + json.dumps(worker.messages)
