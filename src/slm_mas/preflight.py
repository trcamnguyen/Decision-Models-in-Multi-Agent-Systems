import importlib.metadata
import os
import shutil
import subprocess
import time
from .config import is_image_digest, is_revision, path_for
from .context import ContextBuilder, OPTIONS, read_prompts
from .routers.base import validate_decision
from .schemas import PilotError, RoutingRequest, digest


def static_checks(config, require_data=True):
    checks = []
    def check(name, ok, detail):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
    for package, expected in [("laya", "0.4.37"), ("transformers", None)]:
        try:
            version = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            version = None
        check("dependency:" + package, version is not None and (expected is None or version == expected), version or "not installed")
    for role in ["router", "workers"]:
        check(role + ":revision", is_revision(config[role]["revision"]), "immutable 40-character commit required")
    check("worker:served_model", bool(config["workers"]["served_model"]), "explicit endpoint model mapping required")
    check("worker:endpoint", bool(os.environ.get(config["workers"]["base_url_env"])), "endpoint env presence only; value not logged")
    check("worker:chat_template", bool(config["workers"]["chat_template_hash"]), "pinned tokenizer chat-template hash required")
    check("worker:attestation", config["workers"]["deployment_attested"] and config["workers"]["effective_decoding"] is not None,
          "operator confirms checkpoint, template, context limit, decoding, and zero retries")
    check("sandbox:docker", shutil.which("docker"), "Docker executable required")
    check("sandbox:image", is_image_digest(config["execution"]["image_digest"]), "immutable public image required")
    if require_data:
        check("data:manifest", path_for(config, config["benchmark"]["manifest"]).is_file(), "prepare-data must finish first")
    if shutil.which("docker"):
        try:
            response = subprocess.run(["docker", "info", "--format", "{{.OSType}}"], capture_output=True, text=True, timeout=10)
            check("sandbox:daemon", response.returncode == 0 and response.stdout.strip() == "linux", "Linux Docker daemon required")
            if is_image_digest(config["execution"]["image_digest"]):
                image = subprocess.run(["docker", "image", "inspect", config["execution"]["image_digest"]], capture_output=True, timeout=10)
                check("sandbox:local_image", image.returncode == 0, "build image locally; no automatic pull")
        except (OSError, subprocess.TimeoutExpired):
            check("sandbox:daemon", False, "Docker unavailable or timed out")
    return checks


def real_components(config, log):
    from .execution.sandbox import DockerPublicExecutor
    from .routers.laya_td import LayaRouter
    from .workers.openai_compatible import EndpointWorker
    from .schemas import Artifact, PublicTask
    checks = static_checks(config)
    if not all(check["ok"] for check in checks):
        raise PilotError("preflight_blocked", ", ".join(check["check"] for check in checks if not check["ok"]))
    started = time.monotonic()
    worker = EndpointWorker(config["workers"])
    worker_metadata = worker.preflight()
    counter = worker.counter()
    executor = DockerPublicExecutor(config["execution"])
    # Known harmless fixture through the actual sandbox; a mock cannot satisfy this gate.
    content = "def add(a, b):\n    return a + b\n"
    task = PublicTask("preflight/add", "Add two numbers.", "add", ["assert add(1, 2) == 3"], ["assert add(1, 2) == 3"], "synthetic-preflight")
    artifact = Artifact("preflight-code", "code", content, digest(content), "preflight", 0, None, None)
    feedback = executor.check(task, artifact, config["execution"]["public_suite_timeout_seconds"], log)
    if feedback.status != "pass":
        raise PilotError("sandbox_preflight_failed")
    router = LayaRouter(config["router"])
    try:
        prompts = read_prompts(path_for(config, config["prompts_dir"]))
        options = {key: OPTIONS[key] for key in ["analyst", "implementer"]}
        view = "Task: write a Python function that adds two numbers. No code exists. Worker budget: 6."
        meta = router.measure(view, prompts["router"].strip(), options)
        if meta["option_truncated"] or meta["state_tokens_dropped"]:
            raise PilotError("sdk_truncation_error")
        request = RoutingRequest(view, list(options), prompts["router"].strip(), options, "preflight", digest(view), meta["rendered_input_hash"], meta,
                                 config["router"]["timeout_seconds"])
        warmup = time.monotonic()
        decision = router.decide(request)
        log.write("laya-real-response.json", decision.raw_response)
        log.write("laya-real-request.json", request)
        validate_decision(decision, list(options))
        metadata = {"router": router.metadata, "worker": worker_metadata,
                    "warmup_ms": (time.monotonic() - warmup) * 1000,
                    "cold_setup_ms": (time.monotonic() - started) * 1000}
        log.write("models.json", metadata)
        log.event("model_loaded", **metadata)
        context = ContextBuilder(prompts, router, counter, config["router"]["max_len"],
                                 config["workers"]["context_limit"], config["workers"]["max_new_tokens"])
        return router, worker, executor, context, metadata
    except Exception:
        router.close()
        raise
