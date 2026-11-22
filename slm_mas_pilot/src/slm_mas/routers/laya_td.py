"""Adapter for laya 0.4.37. All SDK/model work lives in one supervised process."""
import importlib.metadata
import multiprocessing as mp
import time
import warnings
from ..schemas import PilotError, RoutingDecision, digest

SDK_VERSION = "0.4.37"
MODEL_ID = "convaiinnovations/laya-typed-decisions"


class LayaRenderer:
    def __init__(self, agent, config):
        self.agent, self.config = agent, config

    def count(self, text):
        tok = self.agent.tok
        return len(tok(text.replace(tok.mask_token, " "), add_special_tokens=False)["input_ids"])

    def measure(self, view, question, options):
        from laya.common import build_head, build_sequence, render_options
        tok = self.agent.tok
        q = {"t": "choice", "ins": question, "crit": options}
        head, markers, stats = build_head(tok, q, self.config["head_max_len"])
        # Compare each entire untruncated span, including instructions, not only marker count.
        expected_ins = tok("choice question: " + question.replace(tok.mask_token, " "), add_special_tokens=False)["input_ids"]
        truncated = head[1:markers[0] - 1] != expected_ins
        for i, rendered in enumerate(render_options(q)):
            expected = tok(" " + rendered.replace(tok.mask_token, " "), add_special_tokens=False)["input_ids"]
            end = markers[i + 1] if i + 1 < len(markers) else len(head) - 1
            truncated |= head[markers[i] + 1:end] != expected
        ids, actual_markers, option_stats, truncation = build_sequence(
            tok, view, q, self.config["max_len"], self.config["head_max_len"],
            return_stats=True, return_truncation_stats=True)
        return {"head_len": len(head), "state_len": truncation["state_tokens"],
                # Report length BEFORE SDK's clamp so the caller can shrink optional fields.
                "total_len": len(head) + truncation["state_tokens"] + 1,
                "rendered_len": len(ids), "rendered_token_ids": ids,
                "rendered_input_hash": digest(ids), "option_truncated": truncated or len(actual_markers) != len(options),
                **truncation, "option_stats": option_stats, "source": f"laya=={SDK_VERSION}:build_sequence"}


def _serve(connection, config):
    try:
        if importlib.metadata.version("laya") != SDK_VERSION:
            raise PilotError("sdk_version_mismatch")
        import laya
        started = time.monotonic()
        agent = laya.load(MODEL_ID, device=config["device"], revision=config["revision"],
                          backend="eager", fast=False, compile=False, vegaml=False)
        # The SDK retries GPU OOM on CPU after emitting this warning. Turn it into an error
        # BEFORE the retry, rather than letting a hidden second inference enter the trace.
        warnings.filterwarnings("error", message="Warning: GPU memory exceeded during inference.*", category=RuntimeWarning)
        agent.amp_enabled = False
        renderer = LayaRenderer(agent, config)
        connection.send({"ok": True, "value": {"sdk_version": SDK_VERSION, "model_id": MODEL_ID,
                         "revision": agent.revision, "device": str(agent.device), "dtype": str(agent.dtype),
                         "quantization": None, "cold_load_ms": (time.monotonic() - started) * 1000}})
        while True:
            operation, arguments = connection.recv()
            if operation == "close":
                break
            try:
                if operation == "count":
                    value = renderer.count(*arguments)
                elif operation == "measure":
                    value = renderer.measure(*arguments)
                elif operation == "predict":
                    view, question, options, expected_hash = arguments
                    measured = renderer.measure(view, question, options)
                    if measured["option_truncated"] or measured["state_tokens_dropped"] or measured["rendered_input_hash"] != expected_hash:
                        raise PilotError("sdk_truncation_error")
                    value = agent.predict(view, {"next_agent": {"type": "choice", "instructions": question, "criteria": options}},
                                          max_len=config["max_len"], head_max_len=config["head_max_len"])
                    value["adapter_effective_device"] = str(agent.device)
                    value["adapter_cpu_fallback_count"] = agent.cpu_fallback_count
                else:
                    raise PilotError("router_protocol_error")
                connection.send({"ok": True, "value": value})
            except Exception as error:
                connection.send({"ok": False, "category": error.category if isinstance(error, PilotError) else "router_inference_error"})
    except Exception as error:
        connection.send({"ok": False, "category": error.category if isinstance(error, PilotError) else "router_load_error"})
    finally:
        connection.close()


class LayaRouter:
    is_mock = False

    def __init__(self, config):
        self.config = config
        self.episode_deadline = None
        ctx = mp.get_context("spawn")
        self.connection, child = ctx.Pipe()
        self.process = ctx.Process(target=_serve, args=(child, config), daemon=True)
        self.process.start()
        child.close()
        try:
            self.metadata = self._receive(config.get("load_timeout_seconds", 300))
            if self.metadata["revision"] != config["revision"]:
                raise PilotError("router_revision_mismatch")
            requested = config["device"]
            if self.metadata["device"] != requested and not (requested == "cuda" and self.metadata["device"].startswith("cuda:")):
                raise PilotError("router_device_mismatch")
        except Exception:
            self.close()
            raise

    def _receive(self, timeout):
        if not self.connection.poll(timeout):
            self.close()
            raise PilotError("router_timeout")
        try:
            response = self.connection.recv()
        except (EOFError, OSError):
            raise PilotError("router_process_crash") from None
        if not response["ok"]:
            raise PilotError(response["category"])
        return response["value"]

    def rpc(self, operation, arguments, timeout=None):
        timeout = timeout or self.config["timeout_seconds"]
        if self.episode_deadline is not None:
            left = self.episode_deadline - time.monotonic()
            if left <= 0:
                raise PilotError("wallclock_budget_exhausted")
            timeout = min(timeout, left)
        self.connection.send((operation, arguments))
        return self._receive(timeout)

    def set_deadline(self, deadline):
        self.episode_deadline = deadline

    def count(self, text):
        return self.rpc("count", [text])

    def measure(self, view, question, options):
        return self.rpc("measure", [view, question, options])

    def decide(self, request):
        raw = self.rpc("predict", [request.routing_view, request.question_text, request.option_descriptions, request.input_hash], request.timeout_seconds)
        try:
            answer = raw["answers"]["next_agent"]
            if answer["type"] != "choice" or raw.get("usage", {}).get("truncated") or raw.get("adapter_cpu_fallback_count", 0):
                raise ValueError()
            return RoutingDecision(answer["choice"], answer["choice"], answer.get("probabilities"), raw_response=raw)
        except (KeyError, TypeError, ValueError):
            # Return invalid response so runtime persists it before validation fails.
            return RoutingDecision("invalid", "invalid", validation_status="error", raw_response=raw)

    def close(self):
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(timeout=3)
            if self.process.is_alive():
                self.process.kill()
                self.process.join(timeout=3)
        self.connection.close()
