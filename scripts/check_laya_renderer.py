"""Offline integration check against an explicitly downloaded checkpoint tokenizer.

This verifies actual SDK rendering; it is NOT a real model response fixture.
"""
import argparse
import os
from pathlib import Path
from types import SimpleNamespace


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tokenizer-dir", required=True)
    parser.add_argument("--config", default="configs/pilot_laya_mbpp.yaml")
    parser.add_argument("--output", default="verification/laya_renderer.json")
    args = parser.parse_args()
    os.environ.setdefault("USE_TF", "0")
    from transformers import AutoTokenizer
    from slm_mas.config import load_config, path_for
    from slm_mas.context import ContextBuilder, OPTIONS, mock_worker_counter, read_prompts
    from slm_mas.logging import EpisodeLog, write_json
    from slm_mas.routers.laya_td import LayaRenderer
    from slm_mas.schemas import BudgetState, PublicTask, WorkflowState, PilotError
    from slm_mas.state import ArtifactStore
    import tempfile
    config = load_config(args.config)
    tokenizer = AutoTokenizer.from_pretrained(args.tokenizer_dir, local_files_only=True)
    renderer = LayaRenderer(SimpleNamespace(tok=tokenizer), config["router"])
    prompts = read_prompts(path_for(config, config["prompts_dir"]))
    results = {}
    for actions in [["analyst", "implementer"], list(OPTIONS)]:
        options = {key: OPTIONS[key] for key in actions}
        measured = renderer.measure("Task: add two numbers. No code. Budget: 6.", prompts["router"].strip(), options)
        assert not measured["option_truncated"] and not measured["state_tokens_dropped"]
        results[str(len(actions))] = measured
    truncated = renderer.measure("word " * 3000, prompts["router"].strip(), OPTIONS)
    assert truncated["state_tokens_dropped"] > 0
    long_options = {key: value + " long description" * 60 for key, value in OPTIONS.items()}
    assert renderer.measure("short", prompts["router"].strip(), long_options)["option_truncated"]
    task = PublicTask("synthetic/renderer", "Return the sum of a and b.", "add", [], ["assert add(1,2)==3"], "synthetic")
    builder = ContextBuilder(prompts, renderer, mock_worker_counter)
    with tempfile.TemporaryDirectory(dir=Path(args.output).resolve().parent) as directory:
        state = WorkflowState(task, BudgetState())
        store = ArtifactStore(EpisodeLog(directory, "renderer", task.task_id))
        code = "def add(a,b):\n" + "    # long code line\n" * 1000 + "    return a+b\n"
        state.current_code_id = store.add("code", code, "implementer", 1, None, None).artifact_id
        request = builder.routing(state, store)
        assert request.tokenization_metadata["total_len"] <= 1024
        assert not request.tokenization_metadata["state_tokens_dropped"]
        assert "CODE_EXCERPT_INCOMPLETE" in request.routing_view
        results["long_code_context"] = request.tokenization_metadata
    write_json(Path(args.output), {"kind": "real SDK and checkpoint tokenizer; no model inference",
               "revision": config["router"]["revision"], "sdk_version": "0.4.37", "results": results,
               "overflow_and_option_truncation_detection": "passed"})
    print(f"Renderer verification: {args.output}")


if __name__ == "__main__":
    main()
