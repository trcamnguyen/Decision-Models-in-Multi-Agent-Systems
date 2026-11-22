import argparse
from datetime import datetime, timezone
import importlib.metadata
import json
from pathlib import Path
import platform
import subprocess
import sys
import time
import uuid
import yaml
from .benchmarks.mbpp import MBPPAdapter, prepare_data
from .config import load_config, path_for
from .context import ContextBuilder, MockRenderer, mock_worker_counter, read_prompts
from .execution.sandbox import MockPublicExecutor
from .logging import EpisodeLog, safe_id, write_json
from .preflight import real_components, static_checks
from .reporting import report
from .routers.mock import MockRouter
from .runtime import run_episode
from .schemas import EpisodeResult, PilotError, PublicTask, TaskManifest, digest, plain
from .workers.mock import MockWorker


def dependencies():
    versions = {}
    for name in ["slm-mas-pilot", "PyYAML", "laya", "torch", "transformers", "evalplus", "huggingface-hub", "numpy"]:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def blocked_episode(root, run_id, task_id, category):
    log = EpisodeLog(root / "episodes" / safe_id(task_id), run_id, task_id)
    log.event("episode_started")
    log.event("system_error", category=category)
    result = EpisodeResult(task_id, category, None, None, "blocked", "not_run")
    log.write("episode_result.json", result)
    log.event("episode_finalized", result_ref="episode_result.json")


def execute(config, smoke=False, limit=None):
    started = time.monotonic()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + config["run_mode"] + "-" + uuid.uuid4().hex[:8]
    root = path_for(config, config["run_root"]) / run_id
    root.mkdir(parents=True)
    prompts = read_prompts(path_for(config, config["prompts_dir"]))
    for key, value in prompts.items():
        (root / "prompts").mkdir(exist_ok=True)
        (root / "prompts" / (key + ".txt")).write_text(value, encoding="utf-8")
    (root / "config.resolved.yaml").write_text(yaml.safe_dump(config, sort_keys=True, allow_unicode=True), encoding="utf-8")
    source_root = path_for(config, "src/slm_mas")
    source_hashes = {str(path.relative_to(source_root)): digest(path.read_text(encoding="utf-8")) for path in sorted(source_root.rglob("*.py"))}
    try:
        git = subprocess.run(["git", "rev-parse", "HEAD"], cwd=config["project_root"], capture_output=True, text=True, timeout=5)
        commit = git.stdout.strip() if git.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        commit = None
    run_manifest = {"run_id": run_id, "run_mode": config["run_mode"], "architecture_version": config["architecture_version"],
                    "config_hash": digest(config), "prompt_hashes": {k: digest(v) for k, v in prompts.items()},
                    "source_hashes": source_hashes, "git_commit": commit, "python": platform.python_version(),
                    "dependencies": dependencies(), "status": "running", "smoke": smoke}
    write_json(root / "run_manifest.json", run_manifest)
    run_log = EpisodeLog(root, run_id, "__run__")
    run_log.event("run_started", run_mode=config["run_mode"])
    errors = {}
    if config["run_mode"] == "mock":
        tasks = {f"synthetic/add-{i}": PublicTask(f"synthetic/add-{i}", "Return the sum of two integers a and b.", "add",
                   ["assert add(1, 2) == 3"], ["assert add(1, 2) == 3", "assert add(-1, 1) == 0"], "synthetic-v1") for i in range(limit or 3)}
        manifest = TaskManifest(list(tasks), "", "synthetic-v1", digest(list(tasks)), {k: v.public_payload_hash for k, v in tasks.items()})
    else:
        try:
            manifest, tasks, errors = MBPPAdapter().load_public_tasks(path_for(config, config["benchmark"]["manifest"]))
        except (OSError, ValueError, TypeError, PilotError):
            manifest, tasks = TaskManifest([], "", config["benchmark"]["dataset_version"], "", {}), {}
            errors["__selection__"] = "public_manifest_unavailable"
        original_ids = manifest.selected_ids.copy()
        if smoke:
            manifest.selected_ids = manifest.selected_ids[:limit or 3]
            write_json(root / "parent_selection.json", original_ids)
    write_json(root / "tasks.manifest.json", manifest)
    write_json(root / "development_exposed_ids.json", manifest.development_exposed_ids)
    router = None
    try:
        if config["run_mode"] == "real":
            checks = static_checks(config)
            write_json(root / "preflight.json", checks)
            if not all(check["ok"] for check in checks) or "__selection__" in errors:
                raise PilotError("preflight_blocked")
            setup_log = EpisodeLog(root / "preflight", run_id, "__preflight__")
            router, worker, executor, context, metadata = real_components(config, setup_log)
            run_manifest.update(metadata)
        else:
            context = ContextBuilder(prompts, MockRenderer(), mock_worker_counter)
            executor = MockPublicExecutor()
            run_manifest["cold_setup_ms"] = 0
            run_log.event("model_loaded", mock=True, detail="scripted fixtures; no model loaded")
        episode_errors = 0
        for task_id in manifest.selected_ids:
            if task_id in errors:
                blocked_episode(root, run_id, task_id, errors[task_id])
                episode_errors += 1
                continue
            if config["run_mode"] == "mock":
                router, worker = MockRouter(), MockWorker()
            log = EpisodeLog(root / "episodes" / safe_id(task_id), run_id, task_id)
            result = run_episode(tasks[task_id], router, worker, executor, context, log, config)
            episode_errors += result.workflow_status != "completed"
        run_manifest["status"] = "completed_with_errors" if episode_errors else "completed"
    except Exception as error:
        category = error.category if isinstance(error, PilotError) else "setup_error"
        run_manifest.update(status="blocked", error=category)
        run_log.event("system_error", category=category, exception_type=type(error).__name__)
        for task_id in manifest.selected_ids:
            if not (root / "episodes" / safe_id(task_id) / "episode_result.json").exists():
                blocked_episode(root, run_id, task_id, errors.get(task_id, category))
    finally:
        if router and hasattr(router, "close"):
            router.close()
    run_manifest["total_run_ms"] = (time.monotonic() - started) * 1000
    write_json(root / "run_manifest.json", run_manifest)
    report(root)
    return root, run_manifest["status"]


def mark_exposed(root, ids, reason):
    path = Path(root) / "development_exposed_ids.json"
    previous = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    write_json(path, sorted(set(previous) | set(ids)))
    audit = Path(root) / "exposure_audit.jsonl"
    with audit.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"ids": ids, "reason": reason, "timestamp_utc": datetime.now(timezone.utc).isoformat()}) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description="SLM MAS pilot v0; real runs never use mock fallbacks")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ["doctor", "prepare-data", "smoke", "run", "evaluate", "report", "inspect", "mark-exposed"]:
        sub = commands.add_parser(command)
        sub.add_argument("--config")
        if command in ["evaluate", "report", "inspect", "mark-exposed"]:
            sub.add_argument("--run-dir", required=True)
        if command == "smoke":
            sub.add_argument("--mock", action="store_true")
            sub.add_argument("--limit", type=int, default=3)
        if command in ["inspect", "mark-exposed"]:
            sub.add_argument("--task-id", required=True)
        if command == "mark-exposed":
            sub.add_argument("--reason", required=True)
        if command == "prepare-data":
            sub.add_argument("--output", default="data")
    args = parser.parse_args(argv)
    try:
        if args.command in ["report", "inspect", "mark-exposed"]:
            root = Path(args.run_dir)
            if args.command == "report":
                print(json.dumps(report(root), indent=2))
            else:
                manifest = json.loads((root / "tasks.manifest.json").read_text(encoding="utf-8"))
                if args.task_id not in manifest["selected_ids"]:
                    raise PilotError("unknown_task_id")
                if args.command == "mark-exposed":
                    mark_exposed(root, [args.task_id], args.reason)
                else:
                    # Read only; exposure is recorded explicitly with mark-exposed.
                    path = root / "episodes" / safe_id(args.task_id)
                    print((path / "episode_result.json").read_text(encoding="utf-8"))
                    print(f"Trajectory: {path / 'events.jsonl'}")
            return 0
        if args.command == "evaluate":
            from .evaluation.evalplus_adapter import evaluate_run
            config = load_config(args.config) if args.config else yaml.safe_load((Path(args.run_dir) / "config.resolved.yaml").read_text(encoding="utf-8"))
            outcomes = evaluate_run(args.run_dir, config)
            report(args.run_dir)
            print(f"Evaluation: {Path(args.run_dir) / 'evaluation/outcomes.json'}")
            return 2 if any(value["evaluation_status"] == "evaluation_error" for value in outcomes.values()) else 0
        config = load_config(args.config, mock=getattr(args, "mock", False))
        if args.command == "prepare-data":
            print(prepare_data(config, path_for(config, args.output)))
            return 0
        if args.command == "doctor":
            checks = static_checks(config)
            print(json.dumps({"checks": checks, "dependencies": dependencies()}, indent=2))
            if not all(check["ok"] for check in checks):
                return 2
            run_id = "doctor-" + uuid.uuid4().hex[:8]
            log = EpisodeLog(path_for(config, "runs") / run_id, run_id, "__preflight__")
            router, _, _, _, metadata = real_components(config, log)
            router.close()
            print(json.dumps(metadata, indent=2))
            return 0
        if getattr(args, "limit", 1) < 1:
            raise PilotError("configuration_error", "limit must be positive")
        root, status = execute(config, args.command == "smoke", getattr(args, "limit", None))
        print(json.dumps({"run_dir": str(root), "status": status}, ensure_ascii=True))
        return 0 if status == "completed" else 2
    except (PilotError, OSError, ValueError) as error:
        # Print classified failures, never URLs, credentials, full exception strings or environment.
        print(json.dumps({"error": error.category if isinstance(error, PilotError) else type(error).__name__}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
