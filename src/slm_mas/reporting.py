from collections import Counter
import csv
import json
from pathlib import Path
from .logging import safe_id, write_json


def report(run_dir):
    root = Path(run_dir)
    manifest = json.loads((root / "tasks.manifest.json").read_text(encoding="utf-8"))
    run = json.loads((root / "run_manifest.json").read_text(encoding="utf-8"))
    evaluation = root / "evaluation/outcomes.json"
    outcomes = json.loads(evaluation.read_text(encoding="utf-8"))["outcomes"] if evaluation.exists() else {}
    rows, exclusions, roles, errors = [], Counter(), Counter(), Counter()
    successes = valid = known = 0
    for task_id in manifest["selected_ids"]:
        episode = root / "episodes" / safe_id(task_id)
        result_file = episode / "episode_result.json"
        if not result_file.exists():
            result = {"workflow_status": "missing", "termination_reason": "missing", "metrics": {}, "evaluation_status": "not_run"}
        else:
            result = json.loads(result_file.read_text(encoding="utf-8"))
        outcome = outcomes.get(task_id, {"evaluation_status": result["evaluation_status"], "base_pass": result.get("base_pass"), "plus_pass": result.get("plus_pass")})
        determined = result["workflow_status"] == "completed" and outcome["evaluation_status"] in ["evaluated", "no_candidate"]
        success = outcome.get("base_pass") is True and outcome.get("plus_pass") is True
        eligible = result["workflow_status"] == "completed" and determined
        known += determined
        valid += eligible
        successes += eligible and success
        if not eligible:
            exclusions[result["workflow_status"] if result["workflow_status"] != "completed" else outcome["evaluation_status"]] += 1
        metrics = result["metrics"]
        actions = metrics.get("action_sequence", [])
        roles.update(a for a in actions if a != "stop")
        event_path = episode / "events.jsonl"
        episode_errors = []
        if event_path.exists():
            for event in map(json.loads, event_path.read_text(encoding="utf-8").splitlines()):
                if event["event_type"] in ["system_error", "worker_parse_error"]:
                    category = event["payload"]["category"]
                    errors[category] += 1
                    episode_errors.append(f"{category}@events.jsonl:{event['event_seq']}")
        truncation = 0
        router_tokens = 0
        router_latency = 0
        for request_path in (episode / "router").glob("*-request.json"):
            meta = json.loads(request_path.read_text(encoding="utf-8"))["tokenization_metadata"]
            truncation += sum(meta.get("dropped_tokens", {}).values())
            router_tokens += meta["total_len"]
        for decision_path in (episode / "router").glob("*-decision.json"):
            router_latency += json.loads(decision_path.read_text(encoding="utf-8"))["elapsed_ms"]
        rows.append({"task_id": task_id, "actions": " -> ".join(actions), "termination": result["termination_reason"],
                     "workflow_status": result["workflow_status"], "worker_calls": metrics.get("worker_calls", 0),
                     "router_calls": metrics.get("router_calls", 0), "public_status": metrics.get("public_status"),
                     "evaluation_status": outcome["evaluation_status"], "base_pass": outcome.get("base_pass"),
                     "plus_pass": outcome.get("plus_pass"), "strict_success": success if determined else None,
                     "latency_ms": metrics.get("workflow_latency_ms"), "truncated_tokens": truncation,
                     "router_input_tokens": router_tokens, "router_latency_ms": router_latency,
                     "worker_tokens": metrics.get("worker_tokens"),
                     "repeated_role": metrics.get("repeated_role", 0), "unchanged_code": metrics.get("unchanged_code", 0),
                     "fail_to_pass": metrics.get("fail_to_pass", 0), "pass_to_fail": metrics.get("pass_to_fail", 0),
                     "stop_on_public_fail": metrics.get("stop_on_public_fail", False),
                     "errors": "; ".join(episode_errors), "log_ref": f"episodes/{safe_id(task_id)}/events.jsonl"})
    selected = len(manifest["selected_ids"])
    summary = {"run_id": run["run_id"], "run_mode": run["run_mode"], "selected": selected,
               "successful": successes, "valid_episodes": valid, "determined_outcomes": known,
               "strict_success_over_selected": successes / selected if selected else None,
               "strict_success_over_valid_episodes": successes / valid if valid else None,
               "evaluation_coverage": known / selected if selected else None,
               "exclusion_reasons": dict(exclusions), "role_distribution": dict(roles), "errors": dict(errors),
               "termination_counts": dict(Counter(row["termination"] for row in rows)),
               "evaluation_status_counts": dict(Counter(row["evaluation_status"] for row in rows)),
               "public_pass_count": sum(row["public_status"] == "pass" for row in rows),
               "base_pass_count": sum(row["base_pass"] is True for row in rows),
               "plus_pass_count": sum(row["plus_pass"] is True for row in rows),
               "transitions": {key: sum(row[key] for row in rows) for key in ["repeated_role", "unchanged_code", "fail_to_pass", "pass_to_fail", "stop_on_public_fail"]},
               "monetary_cost": None, "cold_setup_ms": run.get("cold_setup_ms"), "total_run_ms": run.get("total_run_ms"),
               "interpretation": "Synthetic software verification only; no real model result." if run["run_mode"] == "mock" else "Descriptive pilot with public feedback; no causal routing accuracy claim."}
    write_json(root / "summary.json", summary)
    if rows:
        with (root / "episode_metrics.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    lines = [f"# Run {run['run_id']}", "", summary["interpretation"], "",
             f"Selected: {selected}; valid with known outcome: {valid}; determined: {known}; strict successes: {successes}.",
             f"Exclusions: {dict(exclusions)}. Missing token usage/cost remain null.", "",
             "| Task | Actions | Termination | Public | Final | Errors / trajectory |",
             "|---|---|---|---|---|---|"]
    for row in rows:
        lines.append(f"| {row['task_id']} | {row['actions']} | {row['termination']} | {row['public_status']} | {row['evaluation_status']} | [{row['errors'] or 'logs'}]({row['log_ref']}) |")
    lines += ["", "Counts and denominators: [summary.json](summary.json). Full metrics: [episode_metrics.csv](episode_metrics.csv).",
              "", "Human causal explanations must be labeled hypothesis. No ground-truth next-action labels are available."]
    (root / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary
