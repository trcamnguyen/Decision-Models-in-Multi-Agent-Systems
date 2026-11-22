"""Linux-container-only harness. Never import/run as a host execution fallback."""
import ast
import hashlib
import json
import multiprocessing as mp
import os
from pathlib import Path
import resource
import signal
import time
import traceback

LIMIT = 8192


def child(connection, code, assertion, output_prefix):
    os.setsid()
    resource.setrlimit(resource.RLIMIT_FSIZE, (LIMIT, LIMIT))
    for fd, suffix in [(1, "out"), (2, "err")]:
        stream = os.open(output_prefix + suffix, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.dup2(stream, fd)
        os.close(stream)
    try:
        namespace = {"__name__": "candidate"}
        exec(compile(code, "/payload/candidate.py", "exec"), namespace)
        exec(compile(assertion, "/payload/public_assertion.py", "exec"), namespace)
    except BaseException as error:
        category = "assertion_error" if isinstance(error, AssertionError) else "runtime_exception"
        # Render in the isolated child; do not send arbitrary exception objects to parent.
        connection.send({"ok": False, "category": category, "error": (type(error).__name__ + ": " + str(error))[:2048],
                         "traceback": traceback.format_exc(limit=5)[-LIMIT:]})
    else:
        connection.send({"ok": True})
    finally:
        connection.close()


def main():
    code = Path("/payload/candidate.py").read_text(encoding="utf-8")
    payload = json.loads(Path("/payload/public.json").read_text(encoding="utf-8"))
    assertions = payload["assertions"]
    result = {"protocol": "public_runner_v1", "code_hash": hashlib.sha256(code.encode()).hexdigest(),
              "total": len(assertions), "passed": 0, "status": "fail", "failure_category": None,
              "concise_error": None, "stdout": "", "stderr": "", "truncation": {"stdout": False, "stderr": False}}
    try:
        ast.parse(code)
    except SyntaxError as error:
        result.update(failure_category="syntax_error", concise_error=f"SyntaxError: {error.msg} at line {error.lineno}")
    else:
        end = time.monotonic() + payload["timeout"]
        ctx = mp.get_context("fork")
        for index, assertion in enumerate(assertions):
            receive, send = ctx.Pipe(duplex=False)
            prefix = f"/tmp/test-{index}-"
            process = ctx.Process(target=child, args=(send, code, assertion, prefix))
            process.start()
            send.close()
            left = max(0, end - time.monotonic())
            outcome = None
            if receive.poll(left):
                try:
                    outcome = receive.recv()
                except EOFError:
                    pass
            timed_out = outcome is None and process.is_alive() and time.monotonic() >= end
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.join(timeout=1)
            if process.is_alive():
                process.kill()
                process.join()
            receive.close()
            for key, suffix in [("stdout", "out"), ("stderr", "err")]:
                path = Path(prefix + suffix)
                text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
                combined = result[key] + text
                result["truncation"][key] |= len(combined.encode()) >= LIMIT
                result[key] = combined.encode()[:LIMIT].decode("utf-8", "replace")
            if outcome and outcome.get("ok") is True:
                result["passed"] += 1
            elif result["failure_category"] is None:
                result["failure_category"] = "execution_timeout" if timed_out else (outcome or {}).get("category", "candidate_process_exit")
                result["concise_error"] = (outcome or {}).get("error", result["failure_category"])
                result["stderr"] = (result["stderr"] + (outcome or {}).get("traceback", ""))[-LIMIT:]
            if timed_out:
                break
        result["status"] = "pass" if result["passed"] == len(assertions) else "fail"
    print(json.dumps(result))


if __name__ == "__main__":
    main()
