from pathlib import Path
import pytest
from slm_mas.config import load_config
from slm_mas.context import ContextBuilder, MockRenderer, mock_worker_counter, read_prompts
from slm_mas.execution.sandbox import MockPublicExecutor
from slm_mas.logging import EpisodeLog
from slm_mas.routers.mock import MockRouter
from slm_mas.runtime import run_episode
from slm_mas.schemas import PublicTask
from slm_mas.workers.mock import MockWorker


@pytest.fixture
def task():
    return PublicTask("synthetic/add", "Return a plus b.", "add", ["assert add(1,2)==3"], ["assert add(1,2)==3"], "synthetic-v1")


@pytest.fixture
def config():
    return load_config(mock=True)


@pytest.fixture
def context(config):
    return ContextBuilder(read_prompts(Path(config["project_root"]) / "prompts"), MockRenderer(), mock_worker_counter)


@pytest.fixture
def episode(tmp_path, task, config, context):
    def run(actions, responses, executor=None, router=None, worker=None):
        router = router or MockRouter(actions)
        worker = worker or MockWorker(responses)
        log = EpisodeLog(tmp_path, "test", task.task_id)
        result = run_episode(task, router, worker, executor or MockPublicExecutor(), context, log, config)
        return result, router, worker, tmp_path
    return run
