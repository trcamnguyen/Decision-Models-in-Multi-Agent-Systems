import copy
from pathlib import Path
import re
import math
import yaml
from .schemas import PilotError

ROOT = Path(__file__).resolve().parents[2]


def load_config(path=None, mock=False):
    default = Path.cwd() / "configs/pilot_laya_mbpp.yaml"
    path = Path(path or (default if default.is_file() else ROOT / "configs/pilot_laya_mbpp.yaml")).resolve()
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    # All paths are relative to the project root containing configs/, independent of caller cwd.
    base = path.parent.parent if path.parent.name == "configs" else path.parent
    config["project_root"] = str(base)
    if mock:
        config = copy.deepcopy(config)
        config["run_mode"] = "mock"
        config["router"]["kind"] = "mock"
        config["workers"]["backend"] = "mock"
        config["execution"]["backend"] = "mock"
    validate_config(config)
    return config


def path_for(config, value):
    return Path(config["project_root"]) / value


def validate_config(config):
    if config.get("run_mode") not in ["real", "mock"] or config.get("concurrency") != 1:
        raise PilotError("configuration_error", "only sequential real/mock modes are supported")
    for section, names in {"budget": ["max_worker_dispatches", "episode_timeout_seconds"],
                           "router": ["max_len", "head_max_len", "timeout_seconds"],
                           "workers": ["max_new_tokens", "context_limit", "request_timeout_seconds"],
                           "execution": ["cpus", "memory_mb", "pids_limit", "public_suite_timeout_seconds"]}.items():
        for name in names:
            value = config[section][name]
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise PilotError("configuration_error", f"{section}.{name} must be positive")
    if not isinstance(config["budget"]["max_worker_dispatches"], int):
        raise PilotError("configuration_error", "dispatch budget must be an integer")
    if config["workers"]["automatic_retries"] != 0 or config["evaluation"]["mode"] != "offline_only":
        raise PilotError("configuration_error", "retries/online final evaluation are forbidden")
    if config["workers"]["max_new_tokens"] >= config["workers"]["context_limit"]:
        raise PilotError("configuration_error", "output reserve leaves no input context")
    if config["run_mode"] == "real":
        expected = [(config["router"]["kind"], "laya_td"), (config["workers"]["backend"], "openai_compatible"),
                    (config["execution"]["backend"], "docker"),
                    (config["router"]["model_id"], "convaiinnovations/laya-typed-decisions"),
                    (config["workers"]["requested_model"], "Qwen/Qwen2.5-Coder-7B-Instruct")]
        if any(actual != wanted for actual, wanted in expected) or config["router"]["auto_checkpoint_selection"] or config["router"]["fallback"] != "none":
            raise PilotError("configuration_error", "v0 forbids changing models, fallback or mock in real mode")
    if config["router"]["probability_sum_tolerance"] != .0003:
        raise PilotError("configuration_error", "adapter probability tolerance is pinned to 0.0003")
    supported = {("router", "sdk_version"): "0.4.37", ("router", "decision_policy"): "argmax",
                 ("router", "view_policy"): "routing_view_v0", ("benchmark", "name"): "mbpp_plus",
                 ("benchmark", "dataset_version"): "v0.2.0", ("benchmark", "public_feedback_policy"): "mbpp_public_original_examples_v0",
                 ("execution", "network"): "none", ("evaluation", "package_version"): "0.3.1"}
    for (section, name), expected in supported.items():
        if str(config[section][name]) != expected:
            raise PilotError("configuration_error", f"unsupported {section}.{name}; change adapter/version explicitly")
    if not all(config["logging"].get(name) is True for name in ["save_exact_model_payloads", "save_full_artifacts", "save_state_snapshots", "redact_secrets"]):
        raise PilotError("configuration_error", "audit logging cannot be disabled in v0")


def is_revision(value):
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{40}", value))


def is_image_digest(value):
    return isinstance(value, str) and bool(re.fullmatch(r"(?:[^\s]+@)?sha256:[0-9a-f]{64}", value))
