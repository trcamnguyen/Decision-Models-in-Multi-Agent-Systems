"""One HTTP POST per dispatch, no retries, streaming or tools."""
import json
import multiprocessing as mp
import os
import time
import urllib.error
import urllib.request
from ..schemas import PilotError, WorkerResponse, digest

MODEL_ID = "Qwen/Qwen2.5-Coder-7B-Instruct"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise PilotError("worker_redirect_rejected")


def _http_once(connection, base_url, key, path, body, timeout):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    request = urllib.request.Request(base_url + path, headers=headers,
                                     data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=timeout) as response:
            data = response.read(8 * 1024 * 1024 + 1)
            if len(data) > 8 * 1024 * 1024:
                raise PilotError("worker_response_too_large")
            connection.send({"ok": True, "value": json.loads(data)})
    except Exception as error:
        connection.send({"ok": False, "category": error.category if isinstance(error, PilotError) else "worker_transport_error"})
    finally:
        connection.close()


class EndpointWorker:
    is_mock = False

    def __init__(self, config):
        self.config = config
        self.base_url = os.environ[config["base_url_env"]].rstrip("/")
        self.key = os.environ.get(config["api_key_env"], "")

    def request(self, path, body=None, timeout=None):
        timeout = timeout or self.config["request_timeout_seconds"]
        ctx = mp.get_context("spawn")
        parent, child = ctx.Pipe(duplex=False)
        process = ctx.Process(target=_http_once, args=(child, self.base_url, self.key, path, body, timeout), daemon=True)
        process.start()
        child.close()
        try:
            if not parent.poll(timeout):
                raise PilotError("worker_request_timeout")
            response = parent.recv()
            if not response["ok"]:
                raise PilotError(response["category"])
            return response["value"]
        except (OSError, EOFError):
            raise PilotError("worker_process_crash") from None
        finally:
            parent.close()
            process.join(timeout=.1)
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)
            if process.is_alive():
                process.kill()
                process.join(timeout=2)

    def preflight(self):
        models = self.request("/models")
        if self.config["served_model"] not in [item.get("id") for item in models.get("data", [])]:
            raise PilotError("worker_model_mismatch")
        return {"served_model": self.config["served_model"], "requested_model": MODEL_ID,
                "endpoint_identity_source": "models endpoint plus operator deployment attestation",
                "quantization": self.config["quantization"], "revision": self.config["revision"],
                "effective_settings_source": "operator attestation; verify provider usage on each response"}

    def counter(self):
        os.environ.setdefault("USE_TF", "0")
        from transformers import AutoTokenizer
        tokenizer = AutoTokenizer.from_pretrained(MODEL_ID, revision=self.config["revision"], trust_remote_code=False)
        template_hash = digest(tokenizer.chat_template)
        if template_hash != self.config["chat_template_hash"]:
            raise PilotError("worker_chat_template_mismatch")
        def count(messages):
            ids = tokenizer.apply_chat_template(messages, tokenize=True, add_generation_prompt=True)
            return len(ids), {"source": "pinned_qwen_tokenizer", "chat_template_hash": template_hash,
                              "rendered_input_hash": digest(ids), "revision": self.config["revision"]}
        return count

    def generate(self, request):
        body = {"model": self.config["served_model"], "messages": request.messages,
                "temperature": self.config["temperature"], "top_p": self.config["top_p"],
                "max_tokens": self.config["max_new_tokens"], "stream": False, "n": 1}
        if self.config.get("seed_supported"):
            body["seed"] = self.config["seed"]
        start = time.monotonic()
        raw = self.request("/chat/completions", body, request.timeout_seconds)
        try:
            if len(raw["choices"]) != 1 or raw.get("model") != self.config["served_model"]:
                raise ValueError()
            choice = raw["choices"][0]
            content = choice["message"]["content"]
            if not isinstance(content, str):
                raise ValueError()
            usage = raw.get("usage")
            if usage is not None and (not isinstance(usage, dict) or any(type(usage.get(key)) is not int or usage[key] < 0 for key in ["prompt_tokens", "completion_tokens", "total_tokens"])):
                raise ValueError()
            # Token mismatch invalidates the endpoint/template preflight, rather than hiding overflow.
            if usage and usage.get("prompt_tokens") is not None and usage["prompt_tokens"] != request.input_tokens:
                metadata_error = "worker_tokenization_mismatch"
            else:
                metadata_error = None
        except (KeyError, TypeError, ValueError, AttributeError):
            return WorkerResponse("", metadata={"request_body": body, "raw_response": raw,
                                                "validation_error": "worker_response_error", "automatic_retries": 0})
        return WorkerResponse(content, choice.get("finish_reason"), usage,
                              {"request_ms": (time.monotonic() - start) * 1000},
                              {"request_body": body, "raw_response": raw, "usage_source": "provider" if usage else None,
                               "validation_error": metadata_error, "automatic_retries": 0,
                               "effective_decoding": self.config["effective_decoding"],
                               "seed_requested": body.get("seed"), "seed_effective": self.config.get("seed_effective")})
