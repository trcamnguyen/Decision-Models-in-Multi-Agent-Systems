import json
import pytest
from slm_mas.schemas import BudgetState, PilotError, RoutingDecision, WorkflowState
from slm_mas.state import legal_actions
from slm_mas.workers.mock import BAD_CODE, GOOD_CODE


def fence(code):
    return f"```python\n{code}```"


def read(root, name):
    return json.loads((root / name).read_text(encoding="utf-8"))


def test_legal_actions_not_controlled_by_test_outcome(task):
    state = WorkflowState(task, BudgetState())
    assert legal_actions(state) == ["analyst", "implementer"]
    state.current_code_id = "code"
    assert legal_actions(state) == ["analyst", "implementer", "repairer", "stop"]


def test_repair_handoff_latest_candidate_and_stop(episode):
    result, router, worker, root = episode(["implementer", "repairer", "stop"], [fence(BAD_CODE), fence(GOOD_CODE)])
    assert result.termination_reason == "router_stop"
    assert (router.calls, worker.calls) == (3, 2)
    assert (root / "final.py").read_text() == GOOD_CODE
    payload = json.loads(read(root, "worker/002-request.json")["messages"][1]["content"])
    assert payload["public_feedback"]["code_artifact_id"] == payload["current_code"]["artifact_id"]
    final = read(root, "snapshots/final.json")
    assert final["latest_feedback"]["code_artifact_id"] == result.final_code_id
    assert result.metrics["fail_to_pass"] == 1


def test_syntax_feedback_then_parse_error_keeps_candidate(episode):
    result, _, _, root = episode(["implementer", "repairer", "stop"], [fence("def add(:\n"), "not code"])
    final = read(root, "snapshots/final.json")
    assert final["latest_feedback"]["failure_category"] == "syntax_error"
    assert final["last_worker_attempt"]["error"] == "output_parse_error"
    assert result.final_code_id.startswith("code-001")
    assert result.metrics["stop_on_public_fail"]


def test_analyst_preserves_code_feedback_and_provenance(episode):
    result, _, _, root = episode(["implementer", "analyst", "stop"], [fence(GOOD_CODE), "Check negatives.\n```python\ndef bad(): pass\n```"])
    final = read(root, "snapshots/final.json")
    notes = read(root, f"artifacts/{final['latest_analysis_id']}.json")
    assert notes["based_on_code_id"] == result.final_code_id
    assert final["latest_feedback"]["status"] == "pass"
    assert final["last_worker_attempt"]["role_output_violation"]
    assert result.metrics["pass_to_fail"] == 0


def test_regression_retained_and_analysis_not_counted(episode):
    result, _, _, root = episode(["implementer", "analyst", "repairer", "stop"], [fence(GOOD_CODE), "notes", fence(BAD_CODE)])
    assert (root / "final.py").read_text() == BAD_CODE
    assert result.metrics["pass_to_fail"] == 1


@pytest.mark.parametrize("role,response,has_code", [("analyst", "notes", False), ("implementer", fence(GOOD_CODE), True), ("implementer", "bad output", False)])
def test_six_dispatches_no_seventh_or_final_call(episode, role, response, has_code):
    result, router, worker, root = episode([role] * 6, [response] * 6)
    assert router.calls == worker.calls == 6
    assert result.termination_reason == "worker_budget_exhausted"
    assert (result.final_code_id is not None) == has_code
    if not has_code:
        assert result.evaluation_status == "no_candidate"
    assert result.metrics["worker_tokens"] is None


def test_invalid_router_stops_no_fallback(episode):
    result, router, worker, root = episode(["stop"], ["unused"])
    assert result.workflow_status == "system_error"
    assert worker.calls == 0
    assert read(root, "router/001-raw.json")["choice"] == "stop"


def test_infrastructure_error_not_candidate_failure(episode):
    class BrokenExecutor:
        def check(self, *args, **kwargs):
            raise PilotError("executor_startup_or_crash")
    result, _, _, root = episode(["implementer"], [fence(GOOD_CODE)], executor=BrokenExecutor())
    assert result.workflow_status == "system_error"
    assert result.final_code_id is not None
    assert read(root, "snapshots/final.json")["latest_feedback"] is None


def test_transport_error_consumes_dispatch(episode):
    class BrokenWorker:
        calls = 0
        def generate(self, request):
            self.calls += 1
            raise PilotError("worker_transport_error")
    result, _, worker, root = episode(["implementer"], [], worker=BrokenWorker())
    assert worker.calls == result.metrics["worker_calls"] == 1
    assert result.workflow_status == "system_error"
    assert read(root, "worker/001-attempt.json")["error"] == "worker_transport_error"


def test_real_runtime_rejects_mock_components(episode, config):
    config["run_mode"] = "real"
    with pytest.raises(PilotError, match="mock component"):
        episode(["implementer"], [fence(GOOD_CODE)])


def test_worker_context_overflow_before_dispatch(episode, context):
    context.worker_limit = 1
    result, _, worker, root = episode(["implementer"], [fence(GOOD_CODE)])
    assert result.workflow_status == "system_error" and worker.calls == 0
    assert "input_overflow" in (root / "events.jsonl").read_text()


def test_candidate_timeout_is_feedback_not_infrastructure_error(episode):
    from slm_mas.schemas import PublicFeedback
    class CandidateTimeout:
        def check(self, task, code, **kwargs):
            return PublicFeedback(code.artifact_id, "fail", 0, 1, 1, "execution_timeout", "candidate deadline exceeded")
    result, _, _, root = episode(["implementer", "stop"], [fence(GOOD_CODE)], executor=CandidateTimeout())
    assert result.workflow_status == "completed"
    assert read(root, "snapshots/final.json")["latest_feedback"]["failure_category"] == "execution_timeout"


def test_router_source_snapshot_hash_is_exact(episode):
    from slm_mas.schemas import digest
    _, _, _, root = episode(["implementer", "stop"], [fence(GOOD_CODE)])
    for index in [1, 2]:
        request = read(root, f"router/{index:03d}-request.json")
        assert request["source_state_hash"] == digest(read(root, f"router/{index:03d}-source-state.json"))


def test_output_length_and_missing_usage_not_fabricated(episode):
    from slm_mas.schemas import WorkerResponse
    result, _, _, root = episode(["implementer", "stop"], [WorkerResponse(fence(GOOD_CODE), "length")])
    assert read(root, "worker/001-attempt.json")["output_truncated"]
    assert result.metrics["worker_tokens"] is None
