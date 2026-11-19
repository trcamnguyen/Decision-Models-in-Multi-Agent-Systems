import ast
import time
from .logging import EpisodeLog
from .routers.base import validate_decision
from .schemas import BudgetState, EpisodeResult, PilotError, WorkerAttempt, WorkflowState, digest, plain
from .state import ArtifactStore, extract_code


def run_episode(task, router, worker, executor, context, log: EpisodeLog, config):
    if config["run_mode"] == "real" and any(getattr(x, "is_mock", False) for x in [router, worker, executor, context.renderer]):
        raise PilotError("configuration_error", "mock component forbidden in real mode")
    start = time.monotonic()
    budget = BudgetState(worker_dispatches_limit=config["budget"]["max_worker_dispatches"],
                         deadline_ms=config["budget"]["episode_timeout_seconds"] * 1000)
    state, store = WorkflowState(task, budget), ArtifactStore(log)
    if hasattr(router, "set_deadline"):
        router.set_deadline(start + budget.deadline_ms / 1000)
    log.event("episode_started", state, run_mode=config["run_mode"])
    log.write("snapshots/000.json", state)
    reason, status = "worker_budget_exhausted", "completed"
    actions, attempts, transitions = [], [], []
    unchanged = 0
    def remaining():
        budget.elapsed_ms = (time.monotonic() - start) * 1000
        if budget.elapsed_ms >= budget.deadline_ms:
            raise PilotError("wallclock_budget_exhausted")
        return (budget.deadline_ms - budget.elapsed_ms) / 1000
    try:
        while budget.worker_dispatches_used < budget.worker_dispatches_limit:
            remaining()
            source_state = plain(state)
            request = context.routing(state, store)
            request.timeout_seconds = min(config["router"]["timeout_seconds"], remaining())
            budget.router_decisions_used += 1
            rid = f"router/{budget.router_decisions_used:03d}"
            log.write(rid + "-source-state.json", source_state)
            ref = log.write(rid + "-request.json", request)
            log.event("routing_view_built", state, **ref)
            log.event("router_request", state, **ref)
            called = time.monotonic()
            decision = router.decide(request)
            decision.elapsed_ms = (time.monotonic() - called) * 1000
            decision.raw_response_ref = rid + "-raw.json"
            ref = log.write(decision.raw_response_ref, decision.raw_response)
            log.event("router_response", state, **ref)
            decision = validate_decision(decision, request.ordered_legal_actions)
            ref = log.write(rid + "-decision.json", decision)
            log.event("router_decision", state, **ref, action=decision.action)
            actions.append(decision.action)
            remaining()
            if decision.action == "stop":
                reason = "router_stop"
                break
            role = decision.action
            request = context.worker(state, store, role)
            request.timeout_seconds = min(config["workers"]["request_timeout_seconds"], remaining())
            budget.worker_dispatches_used += 1
            wid = f"worker/{budget.worker_dispatches_used:03d}"
            ref = log.write(wid + "-request.json", request)
            attempt = WorkerAttempt(role, ref["ref"])
            state.last_worker_attempt = attempt
            log.event("worker_request", state, **ref)
            called = time.monotonic()
            response = worker.generate(request)
            attempt.raw_response_ref = wid + "-response.json"
            attempt.timings = {**response.timings, "request_ms": (time.monotonic() - called) * 1000}
            attempt.finish_reason, attempt.usage = response.finish_reason, response.usage
            attempt.output_truncated = response.finish_reason == "length"
            ref = log.write(attempt.raw_response_ref, response)
            log.event("worker_response", state, **ref)
            if response.metadata.get("validation_error"):
                raise PilotError(response.metadata["validation_error"])
            remaining()
            old_code = store.get(state.current_code_id)
            old_feedback = state.latest_feedback
            if role == "analyst":
                attempt.role_output_violation = "```" in response.content or bool(__import__("re").search(r"(?m)^\s*(?:async )?def \w+\(", response.content))
                art = store.add("analysis", response.content, role, budget.worker_dispatches_used,
                                state.latest_analysis_id, state.current_code_id)
                state.latest_analysis_id = art.artifact_id
                attempt.artifact_id, attempt.parse_status = art.artifact_id, "notes"
                log.event("artifact_created", state, artifact_id=art.artifact_id)
            else:
                try:
                    code = extract_code(response.content)
                except PilotError as error:
                    attempt.error, attempt.parse_status = error.category, "error"
                    log.event("worker_parse_error", state, category=error.category, response_ref=attempt.raw_response_ref)
                else:
                    art = store.add("code", code, role, budget.worker_dispatches_used, state.current_code_id, state.current_code_id)
                    unchanged += int(old_code is not None and old_code.content_hash == art.content_hash)
                    state.current_code_id, state.latest_feedback = art.artifact_id, None
                    attempt.artifact_id, attempt.parse_status = art.artifact_id, "parsed"
                    log.event("artifact_created", state, artifact_id=art.artifact_id, content_hash=art.content_hash,
                              parent_hash=old_code.content_hash if old_code else None,
                              unchanged=old_code is not None and old_code.content_hash == art.content_hash)
                    log.event("public_check_started", state, code_artifact_id=art.artifact_id,
                              public_suite_hash=digest(task.public_assertions))
                    left = remaining()
                    suite_timeout = config["execution"]["public_suite_timeout_seconds"]
                    feedback = executor.check(task, art, timeout_seconds=min(suite_timeout, left), log=log,
                                              supervisor_timeout_seconds=min(suite_timeout + config["execution"].get("startup_grace_seconds", 5), left))
                    if feedback.code_artifact_id != art.artifact_id:
                        raise PilotError("executor_error", "feedback artifact mismatch")
                    state.latest_feedback = feedback
                    if old_feedback:
                        transitions.append([old_feedback.status, feedback.status])
                    ref = log.write(f"public_checks/{art.artifact_id}.json", feedback)
                    log.event("public_check_finished", state, **ref)
            attempts.append(attempt)
            log.write(wid + "-attempt.json", attempt)
            state.recent_events.append({"role": role, "step": budget.worker_dispatches_used, "parse": attempt.parse_status,
                                        "public": state.latest_feedback.status if state.latest_feedback else None})
            state.recent_events = state.recent_events[-3:]
            budget.elapsed_ms = (time.monotonic() - start) * 1000
            ref = log.write(f"snapshots/{budget.worker_dispatches_used:03d}.json", state)
            log.event("state_updated", state, **ref)
            remaining()
    except Exception as error:
        category = error.category if isinstance(error, PilotError) else "internal_error"
        if (time.monotonic() - start) * 1000 >= budget.deadline_ms:
            category = "wallclock_budget_exhausted"
        reason = category if category == "wallclock_budget_exhausted" else "system_error"
        status = "completed" if reason == "wallclock_budget_exhausted" else "system_error"
        # Never persist arbitrary transport exception text: it may include credentials/URLs.
        log.event("system_error" if status == "system_error" else "deadline_reached", state,
                  category=category, exception_type=type(error).__name__)
        if state.last_worker_attempt and state.last_worker_attempt.parse_status == "pending":
            state.last_worker_attempt.error = category
            log.write(f"worker/{budget.worker_dispatches_used:03d}-attempt.json", state.last_worker_attempt)
    code = store.get(state.current_code_id)
    if code:
        log.write_text("final.py", code.content)
    known_usage = bool(attempts) and len(attempts) == budget.worker_dispatches_used and all(a.usage is not None and all(a.usage.get(k) is not None for k in ["prompt_tokens", "completion_tokens", "total_tokens"]) for a in attempts)
    result = EpisodeResult(task.task_id, reason, code.artifact_id if code else None, code.content_hash if code else None, status,
        evaluation_status="no_candidate" if code is None and status == "completed" else "not_run",
        metrics={"action_sequence": actions, "worker_calls": budget.worker_dispatches_used,
                 "router_calls": budget.router_decisions_used, "workflow_latency_ms": (time.monotonic() - start) * 1000,
                 "public_status": state.latest_feedback.status if state.latest_feedback else None,
                 "repeated_role": sum(a == b and a != "stop" for a, b in zip(actions, actions[1:])),
                 "unchanged_code": unchanged, "fail_to_pass": sum(a != "pass" and b == "pass" for a, b in transitions),
                 "pass_to_fail": sum(a == "pass" and b != "pass" for a, b in transitions),
                 "stop_on_public_fail": reason == "router_stop" and state.latest_feedback is not None and state.latest_feedback.status != "pass",
                 "parse_errors": sum(a.parse_status == "error" for a in attempts),
                 "worker_tokens": {k: sum(a.usage[k] for a in attempts) for k in ["prompt_tokens", "completion_tokens", "total_tokens"]} if known_usage else None,
                 "monetary_cost": None})
    log.write("snapshots/final.json", state)
    log.write("episode_result.json", result)
    log.event("episode_finalized", state, result_ref="episode_result.json", final_hash=result.final_code_hash)
    return result
