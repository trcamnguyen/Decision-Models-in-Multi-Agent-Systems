import json
import hashlib
import os
from pathlib import Path
import tempfile
import time
from ..config import is_image_digest, path_for
from ..execution.sandbox import run_container
from ..logging import safe_id, write_json
from ..schemas import PilotError, canonical, digest


def frozen_candidates(run_dir):
    run = Path(run_dir)
    manifest = json.loads((run / "tasks.manifest.json").read_text(encoding="utf-8"))
    samples, outcomes = [], {}
    for task_id in manifest["selected_ids"]:
        episode = run / "episodes" / safe_id(task_id)
        result = json.loads((episode / "episode_result.json").read_text(encoding="utf-8"))
        if result["workflow_status"] != "completed":
            outcomes[task_id] = {"evaluation_status": "excluded_system_error", "base_pass": None, "plus_pass": None}
        elif result["final_code_id"] is None:
            outcomes[task_id] = {"evaluation_status": "no_candidate", "base_pass": False, "plus_pass": False}
        else:
            code = (episode / "final.py").read_text(encoding="utf-8")
            if digest(code) != result["final_code_hash"]:
                raise PilotError("frozen_hash_mismatch", task_id)
            samples.append({"task_id": task_id, "solution": code})
    return samples, outcomes


def parse_results(raw, samples):
    result = {}
    expected = {sample["task_id"]: sample["solution"] for sample in samples}
    if set(raw.get("eval", {})) != set(expected):
        raise PilotError("evaluation_schema_error", "missing/extra task results")
    for task_id, entries in raw["eval"].items():
        if len(entries) != 1 or entries[0].get("solution") != expected[task_id]:
            raise PilotError("evaluation_schema_error", "solution mismatch")
        entry = entries[0]
        if entry.get("base_status") not in ["pass", "fail", "timeout"] or entry.get("plus_status") not in ["pass", "fail", "timeout"]:
            raise PilotError("evaluation_schema_error", "unknown status")
        result[task_id] = {"evaluation_status": "evaluated", "base_pass": entry["base_status"] == "pass",
                           "plus_pass": entry["plus_status"] == "pass", "final_code_hash": digest(expected[task_id])}
    return result


def evaluate_run(run_dir, config):
    run = Path(run_dir)
    manifest = json.loads((run / "run_manifest.json").read_text(encoding="utf-8"))
    if manifest["run_mode"] == "mock":
        raise PilotError("mock_evaluation_forbidden", "synthetic tasks have no MBPP+ final outcome")
    target = run / "evaluation"
    target.mkdir(exist_ok=True)
    if (target / "outcomes.json").exists():
        raise PilotError("evaluation_already_exists", "never overwrite an evaluation")
    samples, outcomes = frozen_candidates(run)
    freeze_hash = digest(samples)
    started = time.monotonic()
    evaluation_status = "completed"
    try:
        if samples:
            image = config["evaluation"]["image_digest"]
            if not is_image_digest(image):
                raise PilotError("evaluation_blocked", "pin the evaluation image digest")
            private_path = path_for(config, config["evaluation"]["dataset_path"])
            selected = json.loads((run / "tasks.manifest.json").read_text(encoding="utf-8"))
            if hashlib.sha256(private_path.read_bytes()).hexdigest() != selected["dataset_hash"]:
                raise PilotError("evaluation_dataset_hash_mismatch")
            with tempfile.TemporaryDirectory(prefix="slm-evaluate-", dir=target) as directory:
                root = Path(directory)
                inputs, output = root / "input", root / "output"
                inputs.mkdir()
                output.mkdir(mode=0o777)
                if os.name != "nt":
                    output.chmod(0o777)
                (inputs / "samples.jsonl").write_text("\n".join(canonical(sample) for sample in samples) + "\n", encoding="utf-8")
                execution = {**config["execution"], "memory_mb": 2048, "pids_limit": 128}
                response = run_container(image, [(inputs, "/input", False), (private_path, "/private/mbpp_plus.jsonl", False),
                                                  (output, "/output", True)], execution,
                                         ["python", "-I", "/app/evaluate.py"], config["evaluation"]["timeout_seconds"])
                write_json(target / "container_log.json", response)
                if response["timeout"] or response["returncode"] != 0:
                    raise PilotError("evaluation_error")
                raw = json.loads((output / "results.json").read_text(encoding="utf-8"))
                write_json(target / "raw_results.json", raw)
                outcomes.update(parse_results(raw, samples))
    except (PilotError, OSError, ValueError) as error:
        evaluation_status = error.category if isinstance(error, PilotError) else "evaluation_error"
        for sample in samples:
            outcomes[sample["task_id"]] = {"evaluation_status": "evaluation_error", "base_pass": None,
                                           "plus_pass": None, "error": evaluation_status}
    finally:
        if digest(frozen_candidates(run)[0]) != freeze_hash:
            raise PilotError("frozen_hash_mismatch", "evaluation modified online final candidate")
    write_json(target / "outcomes.json", {"status": evaluation_status, "outcomes": outcomes,
               "duration_ms": (time.monotonic() - started) * 1000, "frozen_samples_hash": freeze_hash,
               "package_version": "0.3.1", "image_digest": config["evaluation"]["image_digest"],
               "effective_limits": {"memory_mb": 2048, "pids_limit": 128, "cpus": config["execution"]["cpus"]}})
    write_json(target / "evaluation_finished.json", {"event_type": "evaluation_finished", "status": evaluation_status})
    return outcomes
