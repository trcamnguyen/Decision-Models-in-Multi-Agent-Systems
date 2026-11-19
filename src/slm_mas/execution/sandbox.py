import ast
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import uuid
from ..schemas import PilotError, PublicFeedback, digest


def docker_command(image, mounts, config, command, name=None):
    name = name or "slm-" + uuid.uuid4().hex
    args = ["docker", "run", "--rm", "--name", name, "--network", "none", "--user", "65532:65532",
            "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--cpus", str(config["cpus"]), "--memory", f"{config['memory_mb']}m",
            "--memory-swap", f"{config['memory_mb']}m", "--pids-limit", str(config["pids_limit"]),
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m,mode=1777", "--log-driver", "none"]
    for source, target, writable in mounts:
        args += ["--mount", f"type=bind,source={Path(source).resolve()},target={target}" + ("" if writable else ",readonly")]
    return args + [image] + command, name


def run_container(image, mounts, config, command, timeout, output_limit=65536):
    if not shutil.which("docker"):
        raise PilotError("sandbox_unavailable", "Docker required; host execution is forbidden")
    args, name = docker_command(image, mounts, config, command)
    # Drain continuously with bounded retained bytes, avoiding communicate() on untrusted output.
    import threading
    out, err = bytearray(), bytearray()
    totals = [0, 0]
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    def drain(stream, buffer, index):
        while chunk := stream.read(4096):
            totals[index] += len(chunk)
            buffer.extend(chunk[:max(0, output_limit - len(buffer))])
    threads = [threading.Thread(target=drain, args=(process.stdout, out, 0), daemon=True),
               threading.Thread(target=drain, args=(process.stderr, err, 1), daemon=True)]
    for thread in threads:
        thread.start()
    expired = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        expired = True
    finally:
        if expired:
            try:
                cleanup = subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
                if cleanup.returncode != 0:
                    raise PilotError("sandbox_cleanup_error")
            finally:
                process.kill()
                process.wait(timeout=10)
        for thread in threads:
            thread.join(timeout=5)
    return {"returncode": process.returncode, "stdout": out.decode("utf-8", "replace"),
            "stderr": err.decode("utf-8", "replace"), "timeout": expired,
            "truncation": {"stdout": totals[0] > output_limit, "stderr": totals[1] > output_limit}}


class DockerPublicExecutor:
    is_mock = False

    def __init__(self, config):
        self.config = config

    def check(self, task, code, timeout_seconds, log, supervisor_timeout_seconds=None):
        started = time.monotonic()
        # Outer timeout includes container startup; child supervisor reports candidate timeout itself.
        with tempfile.TemporaryDirectory(prefix="slm-public-") as directory:
            root = Path(directory)
            (root / "candidate.py").write_text(code.content, encoding="utf-8")
            (root / "public.json").write_text(json.dumps({"assertions": task.public_assertions, "timeout": timeout_seconds}), encoding="utf-8")
            result = run_container(self.config["image_digest"], [(root, "/payload", False)], self.config,
                                   ["python", "-I", "/app/public_runner.py"],
                                   supervisor_timeout_seconds or timeout_seconds + self.config.get("startup_grace_seconds", 5))
        prefix = f"public_checks/{code.artifact_id}"
        log.write_text(prefix + "-harness-stdout.txt", result["stdout"])
        log.write_text(prefix + "-harness-stderr.txt", result["stderr"])
        if result["timeout"] or result["returncode"] != 0:
            raise PilotError("executor_infrastructure_timeout" if result["timeout"] else "executor_startup_or_crash")
        try:
            data = json.loads(result["stdout"])
            if data["protocol"] != "public_runner_v1" or data["code_hash"] != code.content_hash:
                raise ValueError("harness identity")
            if data["total"] != len(task.public_assertions) or not (0 <= data["passed"] <= data["total"]):
                raise ValueError("counts")
            if type(data["passed"]) is not int or data["status"] != ("pass" if data["passed"] == data["total"] else "fail"):
                raise ValueError("status mismatch")
            if not all(isinstance(data[key], str) for key in ["stdout", "stderr"]):
                raise ValueError("output type")
        except (ValueError, KeyError, TypeError):
            raise PilotError("executor_protocol_error") from None
        stdout_ref = log.write_text(prefix + "-stdout.txt", data["stdout"])["ref"]
        stderr_ref = log.write_text(prefix + "-stderr.txt", data["stderr"])["ref"]
        return PublicFeedback(code.artifact_id, data["status"], data["passed"], data["total"] - data["passed"],
                              data["total"], data["failure_category"], data["concise_error"], stdout_ref, stderr_ref,
                              (time.monotonic() - started) * 1000, data["truncation"])


class MockPublicExecutor:
    """Recognizes two synthetic fixtures; NEVER executes generated Python on the host."""
    is_mock = True

    def check(self, task, code, timeout_seconds, log, supervisor_timeout_seconds=None):
        from ..workers.mock import GOOD_CODE, BAD_CODE
        try:
            ast.parse(code.content)
        except SyntaxError as error:
            return PublicFeedback(code.artifact_id, "fail", 0, len(task.public_assertions), len(task.public_assertions),
                                  "syntax_error", f"SyntaxError: {error.msg} at line {error.lineno}")
        if code.content not in [GOOD_CODE, BAD_CODE]:
            raise PilotError("mock_fixture_unknown", "Mock never runs arbitrary code")
        ok = code.content == GOOD_CODE
        return PublicFeedback(code.artifact_id, "pass" if ok else "fail", len(task.public_assertions) if ok else 0,
                              0 if ok else len(task.public_assertions), len(task.public_assertions),
                              None if ok else "assertion_error", None if ok else "AssertionError: synthetic fixture mismatch")
