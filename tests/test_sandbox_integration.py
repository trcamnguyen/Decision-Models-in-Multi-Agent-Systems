"""Opt-in integration. Never silently executes a candidate on the host."""
import os
import pytest
from slm_mas.execution.sandbox import DockerPublicExecutor
from slm_mas.logging import EpisodeLog
from slm_mas.schemas import Artifact, digest

IMAGE = os.environ.get("SLM_TEST_DOCKER_IMAGE")


@pytest.mark.skipif(not IMAGE, reason="SLM_TEST_DOCKER_IMAGE (pinned built Linux image) is required")
@pytest.mark.parametrize("code,category", [
    ("def add(a,b): return a+b\n", None),
    ("def add(a,b): return a-b\n", "assertion_error"),
    ("def add(:\n", "syntax_error"),
    ("while True: pass\n", "execution_timeout"),
    ("import os\nos._exit(0)\n", "candidate_process_exit"),
    ("print('all tests passed')\ndef add(a,b): return 0\n", "assertion_error"),
])
def test_container_feedback(code, category, config, task, tmp_path):
    config["execution"]["image_digest"] = IMAGE
    artifact = Artifact("integration-code", "code", code, digest(code), "implementer", 1, None, None)
    executor = DockerPublicExecutor(config["execution"])
    feedback = executor.check(task, artifact, 2, EpisodeLog(tmp_path, "integration", task.task_id))
    assert feedback.failure_category == category
    assert feedback.status == ("pass" if category is None else "fail")
