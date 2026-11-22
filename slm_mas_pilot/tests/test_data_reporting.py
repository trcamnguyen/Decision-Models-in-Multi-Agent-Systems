import json
from pathlib import Path
import pytest
from slm_mas.benchmarks.mbpp import MBPPAdapter, prepare_from_files
from slm_mas.cli import execute, blocked_episode
from slm_mas.evaluation.evalplus_adapter import frozen_candidates
from slm_mas.logging import safe_id, write_json
from slm_mas.reporting import report
from slm_mas.schemas import PilotError, digest


def test_prepare_strict_public_boundary_and_reproducible_selection(tmp_path):
    original = [{"task_id": i, "prompt": "Add numbers.", "code": "def add(a,b): return a+b # REF_SENTINEL", "test_list": ["assert add(1,2)==3"]} for i in range(1, 5)]
    plus = [{"task_id": f"Mbpp/{i}", "prompt": "Add numbers.", "entry_point": "add", "canonical_solution": "REFERENCE_SENTINEL", "base_input": ["BASE_SENTINEL"], "plus_input": ["PLUS_SENTINEL"]} for i in range(1, 5)]
    op, pp = tmp_path / "original.json", tmp_path / "plus.jsonl"
    op.write_text(json.dumps(original))
    pp.write_text("\n".join(json.dumps(row) for row in plus))
    first = prepare_from_files(op, pp, tmp_path / "a", 3, 42, "f" * 40)
    second = prepare_from_files(op, pp, tmp_path / "b", 3, 42, "f" * 40)
    assert first.read_text() == second.read_text()
    manifest, tasks, errors = MBPPAdapter().load_public_tasks(first)
    assert len(tasks) == 3 and not errors
    for path in (tmp_path / "a/public").glob("*.json"):
        assert "SENTINEL" not in path.read_text()
    task_id = manifest.selected_ids[0]
    payload_path = tmp_path / "a/public" / (safe_id(task_id) + ".json")
    payload = json.loads(payload_path.read_text())
    payload["plus_input"] = "HIDDEN"
    write_json(payload_path, payload)
    _, tasks, errors = MBPPAdapter().load_public_tasks(first)
    assert errors[task_id] == "public_schema_error" and task_id not in tasks


def test_mapping_ignores_citation_url_but_not_changed_requirements():
    from slm_mas.benchmarks.mbpp import map_public
    original = {"task_id": 1, "prompt": "Add numbers. https://example.org/reference", "code": "def add(a,b): return a+b", "test_list": ["assert add(1,2)==3"]}
    extended = {"task_id": "Mbpp/1", "prompt": "Add numbers.", "entry_point": "add"}
    assert map_public("Mbpp/1", original, extended, "rev").task_text == original["prompt"]
    extended["prompt"] = "Multiply numbers."
    with pytest.raises(PilotError):
        map_public("Mbpp/1", original, extended, "rev")


def test_reports_include_missing_blocked_and_no_candidate(tmp_path, config):
    config["run_root"] = str(tmp_path / "runs")
    root, _ = execute(config, smoke=True, limit=1)
    manifest = json.loads((root / "tasks.manifest.json").read_text())
    original = manifest["selected_ids"][0]
    manifest["selected_ids"] += ["missing", "blocked", "output-failure"]
    write_json(root / "tasks.manifest.json", manifest)
    blocked_episode(root, root.name, "blocked", "sandbox_unavailable")
    path = root / "episodes" / safe_id("output-failure")
    write_json(path / "episode_result.json", {"task_id": "output-failure", "workflow_status": "completed", "termination_reason": "worker_budget_exhausted", "evaluation_status": "no_candidate", "metrics": {}, "final_code_id": None})
    write_json(root / "evaluation/outcomes.json", {"outcomes": {original: {"evaluation_status": "evaluated", "base_pass": True, "plus_pass": True}}})
    before = (root / "episodes" / safe_id(original) / "final.py").read_bytes()
    summary = report(root)
    assert summary["selected"] == 4 and summary["valid_episodes"] == 2
    assert summary["strict_success_over_selected"] == .25
    assert summary["strict_success_over_valid_episodes"] == .5
    assert summary["evaluation_coverage"] == .5
    assert (root / "episodes" / safe_id(original) / "final.py").read_bytes() == before


def test_freeze_hash_guard(tmp_path, config):
    config["run_root"] = str(tmp_path / "runs")
    root, _ = execute(config, smoke=True, limit=1)
    samples, _ = frozen_candidates(root)
    path = root / "episodes" / safe_id(samples[0]["task_id"]) / "final.py"
    path.write_text("tampered")
    with pytest.raises(PilotError, match="frozen_hash_mismatch"):
        frozen_candidates(root)


@pytest.mark.parametrize("broken", [False, True])
def test_offline_evaluation_preserves_online_tree_and_classifies_failure(tmp_path, config, monkeypatch, broken):
    from slm_mas.evaluation.evalplus_adapter import evaluate_run
    config["run_root"] = str(tmp_path / "runs")
    root, _ = execute(config, smoke=True, limit=1)
    # The container is explicitly mocked here; this test is not a model/benchmark experiment.
    manifest = json.loads((root / "run_manifest.json").read_text())
    manifest["run_mode"] = "real"
    write_json(root / "run_manifest.json", manifest)
    private = tmp_path / "private.jsonl"
    private.write_text("{}\n")
    tasks = json.loads((root / "tasks.manifest.json").read_text())
    import hashlib
    tasks["dataset_hash"] = hashlib.sha256(private.read_bytes()).hexdigest()
    write_json(root / "tasks.manifest.json", tasks)
    config["evaluation"].update(image_digest="sha256:" + "a" * 64, dataset_path=str(private))
    samples, _ = frozen_candidates(root)
    before = {str(p): p.read_bytes() for p in (root / "episodes").rglob("*") if p.is_file()}
    def fake_container(image, mounts, settings, command, timeout):
        if broken:
            return {"timeout": True, "returncode": -1}
        output = next(Path(source) for source, target, _ in mounts if target == "/output")
        write_json(output / "results.json", {"eval": {s["task_id"]: [{**s, "base_status": "pass", "plus_status": "fail"}] for s in samples}})
        return {"timeout": False, "returncode": 0}
    monkeypatch.setattr("slm_mas.evaluation.evalplus_adapter.run_container", fake_container)
    outcomes = evaluate_run(root, config)
    result = outcomes[samples[0]["task_id"]]
    assert result["evaluation_status"] == ("evaluation_error" if broken else "evaluated")
    assert result["base_pass"] is (None if broken else True)
    assert {str(p): p.read_bytes() for p in (root / "episodes").rglob("*") if p.is_file()} == before
