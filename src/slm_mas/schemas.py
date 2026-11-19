from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
import hashlib
import json
from typing import Any


def plain(value: Any) -> Any:
    return asdict(value) if is_dataclass(value) else value


def canonical(value: Any) -> str:
    return json.dumps(plain(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256((value if isinstance(value, str) else canonical(value)).encode("utf-8")).hexdigest()


class PilotError(Exception):
    def __init__(self, category: str, detail: str = ""):
        self.category, self.detail = category, detail
        super().__init__(f"{category}: {detail}")


@dataclass(frozen=True)
class PublicTask:
    task_id: str
    task_text: str
    entry_point: str
    public_examples: list[str]
    public_assertions: list[str]
    source_revision: str
    public_payload_hash: str = ""

    def __post_init__(self):
        payload = asdict(self)
        payload.pop("public_payload_hash")
        expected = digest(payload)
        if self.public_payload_hash and self.public_payload_hash != expected:
            raise PilotError("public_payload_hash_mismatch", self.task_id)
        object.__setattr__(self, "public_payload_hash", expected)


@dataclass(frozen=True)
class Artifact:
    artifact_id: str
    kind: str
    content: str
    content_hash: str
    producer_role: str
    worker_step: int
    parent_id: str | None
    based_on_code_id: str | None


@dataclass
class PublicFeedback:
    code_artifact_id: str
    status: str
    passed: int
    failed: int
    total: int
    failure_category: str | None = None
    concise_error: str | None = None
    stdout_ref: str | None = None
    stderr_ref: str | None = None
    duration_ms: float = 0
    truncation_metadata: dict = field(default_factory=dict)


@dataclass
class WorkerRequest:
    role: str
    messages: list[dict[str, str]]
    input_tokens: int
    tokenization_metadata: dict = field(default_factory=dict)
    timeout_seconds: float = 120


@dataclass
class WorkerResponse:
    content: str
    finish_reason: str | None = None
    usage: dict | None = None
    timings: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)


@dataclass
class WorkerAttempt:
    role: str
    input_snapshot_ref: str
    raw_response_ref: str | None = None
    parse_status: str = "pending"
    artifact_id: str | None = None
    finish_reason: str | None = None
    usage: dict | None = None
    timings: dict = field(default_factory=dict)
    error: str | None = None
    output_truncated: bool = False
    role_output_violation: bool = False


@dataclass
class BudgetState:
    worker_dispatches_used: int = 0
    worker_dispatches_limit: int = 6
    router_decisions_used: int = 0
    elapsed_ms: float = 0
    deadline_ms: float = 900000


@dataclass
class WorkflowState:
    task: PublicTask
    budget: BudgetState
    current_code_id: str | None = None
    latest_analysis_id: str | None = None
    latest_feedback: PublicFeedback | None = None
    last_worker_attempt: WorkerAttempt | None = None
    recent_events: list[dict] = field(default_factory=list)


@dataclass
class RoutingRequest:
    routing_view: str
    ordered_legal_actions: list[str]
    question_text: str
    option_descriptions: dict[str, str]
    view_policy_version: str
    source_state_hash: str
    input_hash: str
    tokenization_metadata: dict
    timeout_seconds: float = 30


@dataclass
class RoutingDecision:
    action: str
    model_choice_raw: str
    probabilities: dict[str, float] | None = None
    raw_response_ref: str | None = None
    elapsed_ms: float = 0
    validation_status: str = "valid"
    raw_response: Any = None
    probability_summary: dict | None = None


@dataclass
class EpisodeResult:
    task_id: str
    termination_reason: str
    final_code_id: str | None
    final_code_hash: str | None
    workflow_status: str
    evaluation_status: str = "not_run"
    base_pass: bool | None = None
    plus_pass: bool | None = None
    metrics: dict = field(default_factory=dict)


@dataclass
class TaskManifest:
    selected_ids: list[str]
    public_dir: str
    dataset_version: str
    dataset_hash: str
    public_hashes: dict[str, str]
    preparation_errors: dict[str, str] = field(default_factory=dict)
    source_revision: str = ""
    sample_seed: int = 42
    development_exposed_ids: list[str] = field(default_factory=list)
