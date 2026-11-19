import json
import math
from types import SimpleNamespace
import pytest
from slm_mas.routers.base import validate_decision
from slm_mas.schemas import PilotError, RoutingDecision
from slm_mas.state import extract_code
from slm_mas.execution.sandbox import docker_command
from slm_mas.evaluation.evalplus_adapter import parse_results


@pytest.mark.parametrize("probabilities", [{"analyst": math.nan, "implementer": 1}, {"analyst": -.1, "implementer": 1.1}, {"analyst": .2, "implementer": .2}, {"analyst": 1}, {"analyst": True, "implementer": False}])
def test_bad_distributions_fail(probabilities):
    with pytest.raises(PilotError):
        validate_decision(RoutingDecision("analyst", "analyst", probabilities), ["analyst", "implementer"])


def test_rounding_tolerance_and_order_tie_break():
    legal = ["analyst", "implementer", "repairer"]
    decision = validate_decision(RoutingDecision("repairer", "repairer", dict.fromkeys(legal, .3333)), legal)
    assert decision.action == "analyst" and decision.model_choice_raw == "repairer"


def test_parser_rejects_multiple_blocks_preserves_fenced_syntax_errors():
    with pytest.raises(PilotError):
        extract_code("```python\ndef a():pass\n```\n```python\ndef b():pass\n```")
    assert extract_code("```python\ndef broken(:\n```") == "def broken(:\n"


def test_sandbox_only_mounts_supplied_public_payload(tmp_path, config):
    command, _ = docker_command("sha256:" + "a" * 64, [(tmp_path, "/payload", False)], config["execution"], ["python", "-I", "/app/public_runner.py"])
    assert "none" in command and "--read-only" in command
    assert command.count("--mount") == 1
    assert "readonly" in command[command.index("--mount") + 1]
    assert "65532:65532" in command and "no-new-privileges" in command


def test_evaluation_parses_extended_separately():
    sample = {"task_id": "Mbpp/1", "solution": "def a():pass"}
    result = parse_results({"eval": {"Mbpp/1": [{**sample, "base_status": "pass", "plus_status": "fail"}]}}, [sample])
    assert result["Mbpp/1"]["base_pass"] and not result["Mbpp/1"]["plus_pass"]
    with pytest.raises(PilotError):
        parse_results({"eval": {}}, [sample])


def test_endpoint_single_request_and_no_usage_fabrication(config, monkeypatch):
    from slm_mas.workers.openai_compatible import EndpointWorker
    from slm_mas.schemas import WorkerRequest
    monkeypatch.setenv("WORKER_BASE_URL", "http://localhost:8000/v1")
    config["workers"]["served_model"] = "Qwen/Qwen2.5-Coder-7B-Instruct"
    worker = EndpointWorker(config["workers"])
    calls = []
    def reply(path, body=None, timeout=None):
        calls.append((path, body, timeout))
        return {"model": config["workers"]["served_model"], "choices": [{"message": {"content": "notes"}, "finish_reason": "stop"}]}
    worker.request = reply
    response = worker.generate(WorkerRequest("analyst", [{"role": "user", "content": "public task"}], 15))
    assert len(calls) == 1 and calls[0][1]["n"] == 1
    assert "seed" not in calls[0][1] and response.usage is None


def test_executor_startup_timeout_not_labeled_candidate_timeout(config, task, tmp_path, monkeypatch):
    from slm_mas.execution.sandbox import DockerPublicExecutor
    from slm_mas.logging import EpisodeLog
    from slm_mas.schemas import Artifact, digest
    monkeypatch.setattr("slm_mas.execution.sandbox.run_container", lambda *args, **kwargs: {"stdout": "", "stderr": "", "timeout": True, "returncode": -1})
    artifact = Artifact("code", "code", "pass", digest("pass"), "implementer", 1, None, None)
    with pytest.raises(PilotError, match="executor_infrastructure_timeout"):
        DockerPublicExecutor(config["execution"]).check(task, artifact, 1, EpisodeLog(tmp_path, "t", task.task_id))
