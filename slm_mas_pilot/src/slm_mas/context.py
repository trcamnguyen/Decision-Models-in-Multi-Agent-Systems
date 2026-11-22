from pathlib import Path
import re
from .schemas import PilotError, RoutingRequest, WorkerRequest, canonical, digest, plain
from .state import legal_actions

OPTIONS = {
    "analyst": "Analyze requirements, algorithm, or unresolved issues.",
    "implementer": "Create or rewrite the Python solution.",
    "repairer": "Inspect and repair the current Python solution.",
    "stop": "Submit the current solution without further work.",
}


class MockRenderer:
    """Synthetic word/punctuation tokens ONLY for offline contract tests."""
    is_mock = True

    def count(self, text):
        return len(re.findall(r"\w+|[^\w\s]", text))

    def measure(self, view, question, options):
        head = self.count(question + canonical(options)) + 3
        state = self.count(view)
        return {"head_len": head, "state_len": state, "total_len": head + state + 1,
                "option_truncated": False, "state_tokens_dropped": 0,
                "rendered_input_hash": digest([question, options, view]), "source": "synthetic_mock_tokens"}


def excerpt(text, allowance, count, kind):
    if count(text) <= allowance:
        return text
    marker = {"code": "CODE_EXCERPT_INCOMPLETE", "analysis": "ANALYSIS_EXCERPT_INCOMPLETE",
              "events": "EVENTS_EXCERPT_INCOMPLETE", "error": "ERROR_EXCERPT_INCOMPLETE"}[kind]
    if count(marker) > allowance:
        return ""
    lines = text.splitlines(keepends=True)
    if kind == "code":
        prefix, suffix = [], []
        for line in lines:
            if count("".join(prefix) + line) > (allowance - count(marker)) * 2 // 3:
                break
            prefix.append(line)
        for line in reversed(lines[len(prefix):]):
            if count("".join(prefix) + marker + "\n" + line + "".join(suffix)) > allowance:
                break
            suffix.insert(0, line)
        return "".join(prefix) + marker + "\n" + "".join(suffix)
    if kind == "events":
        lines.reverse()
    kept = []
    for line in lines:
        if count("".join(kept) + line + marker) > allowance:
            break
        kept.append(line)
    if kind == "events":
        kept.reverse()
    return "".join(kept) + marker


class ContextBuilder:
    def __init__(self, prompts, renderer, worker_counter, router_limit=1024, worker_limit=8192, output_reserve=1024):
        self.prompts = prompts
        self.renderer, self.worker_counter = renderer, worker_counter
        self.router_limit, self.worker_limit, self.output_reserve = router_limit, worker_limit, output_reserve

    def routing(self, state, store):
        actions = legal_actions(state)
        options = {action: OPTIONS[action] for action in actions}
        question = self.prompts["router"].strip()
        code, analysis = store.get(state.current_code_id), store.get(state.latest_analysis_id)
        feedback = state.latest_feedback
        if feedback and feedback.code_artifact_id != state.current_code_id:
            raise PilotError("state_error", "stale feedback")
        mandatory = {
            "task": state.task.task_text, "entry_point": state.task.entry_point,
            "budget": plain(state.budget), "code_exists": code is not None,
            "last_attempt_error": state.last_worker_attempt.error if state.last_worker_attempt else None,
            "public": ({k: getattr(feedback, k) for k in ["status", "passed", "failed", "total", "failure_category"]} if feedback else None),
            "analysis_based_on_code_id": analysis.based_on_code_id if analysis else None,
            "current_code_id": state.current_code_id,
        }
        raw_error = feedback.concise_error or "" if feedback else ""
        mandatory["concise_error"] = excerpt(raw_error, 96, self.renderer.count, "error")
        original = {"code": code.content if code else "", "analysis": analysis.content if analysis else "",
                    "events": "\n".join(canonical(e) for e in state.recent_events[-3:])}
        def render(fields):
            return canonical(mandatory) + "\n" + "\n".join(f"{key}:\n{value}" for key, value in fields.items())
        empty = {k: "" for k in original}
        baseline = self.renderer.measure(render(empty), question, options)
        if baseline["total_len"] > self.router_limit or baseline.get("state_tokens_dropped", 0):
            raise PilotError("input_overflow", "mandatory router content does not fit")
        if baseline["option_truncated"]:
            raise PilotError("input_overflow", "SDK truncates question/options")
        remaining = self.router_limit - baseline["total_len"]
        allowances = {"code": int(remaining * .65), "analysis": int(remaining * .25)}
        allowances["events"] = remaining - sum(allowances.values())
        unused = 0
        for key in original:
            need = self.renderer.count(original[key])
            unused += max(0, allowances[key] - need)
            allowances[key] = min(allowances[key], need)
        for key in ["code", "analysis", "events"]:
            added = min(unused, max(0, self.renderer.count(original[key]) - allowances[key]))
            allowances[key] += added
            unused -= added
        while True:
            fields = {k: excerpt(v, allowances[k], self.renderer.count, k) for k, v in original.items()}
            view = render(fields)
            measured = self.renderer.measure(view, question, options)
            if measured["total_len"] <= self.router_limit and not measured.get("state_tokens_dropped", 0):
                break
            key = max(allowances, key=allowances.get)
            if allowances[key] == 0:
                raise PilotError("input_overflow", "rendered mandatory content overflow")
            allowances[key] = max(0, allowances[key] - max(1, measured["total_len"] - self.router_limit))
        measured.update({"before": original, "after": fields,
                         "dropped_tokens": {k: max(0, self.renderer.count(v) - self.renderer.count(fields[k].replace("CODE_EXCERPT_INCOMPLETE", "").replace("ANALYSIS_EXCERPT_INCOMPLETE", "").replace("EVENTS_EXCERPT_INCOMPLETE", ""))) for k, v in original.items()},
                         "error_before": raw_error, "error_after": mandatory["concise_error"],
                         "error_truncated": raw_error != mandatory["concise_error"]})
        return RoutingRequest(view, actions, question, options, "routing_view_v0", digest(state),
                              measured["rendered_input_hash"], measured)

    def worker(self, state, store, role):
        code, notes = store.get(state.current_code_id), store.get(state.latest_analysis_id)
        feedback = state.latest_feedback
        if feedback and feedback.code_artifact_id != state.current_code_id:
            raise PilotError("state_error", "stale feedback")
        payload = {"task": plain(state.task), "current_code": plain(code), "latest_analysis": plain(notes),
                   "public_feedback": plain(feedback),
                   "last_attempt_error": state.last_worker_attempt.error if state.last_worker_attempt else None}
        messages = [{"role": "system", "content": self.prompts["common"] + "\n" + self.prompts[role]},
                    {"role": "user", "content": canonical(payload)}]
        tokens, metadata = self.worker_counter(messages)
        if tokens + self.output_reserve > self.worker_limit:
            raise PilotError("input_overflow", "full worker task/code exceeds context plus output reserve")
        return WorkerRequest(role, messages, tokens, metadata)


def read_prompts(directory):
    return {name: (Path(directory) / f"{name}.txt").read_text(encoding="utf-8")
            for name in ["common", "analyst", "implementer", "repairer", "router"]}


def mock_worker_counter(messages):
    return MockRenderer().count(canonical(messages)), {"source": "synthetic_mock_tokens"}
