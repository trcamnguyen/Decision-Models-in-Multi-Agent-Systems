"""Only executed inside the offline evaluation container."""
import importlib.metadata
import json
import os
from pathlib import Path
import shutil


def main():
    if importlib.metadata.version("evalplus") != "0.3.1":
        raise RuntimeError("EvalPlus version mismatch")
    # CLI requires samples for every problem. Supply a byte-preserving row subset of frozen IDs.
    # The reference/oracle/test execution remains the original EvalPlus implementation.
    samples = [json.loads(line) for line in Path("/input/samples.jsonl").read_text().splitlines()]
    ids = {sample["task_id"] for sample in samples}
    rows = [line for line in Path("/private/mbpp_plus.jsonl").read_text().splitlines()
            if json.loads(line)["task_id"] in ids]
    if {json.loads(row)["task_id"] for row in rows} != ids:
        raise RuntimeError("Missing evaluation tasks")
    Path("/tmp/subset.jsonl").write_text("\n".join(rows) + "\n")
    os.environ["MBPP_OVERRIDE_PATH"] = "/tmp/subset.jsonl"
    os.environ["XDG_CACHE_HOME"] = "/tmp/cache"
    shutil.copyfile("/input/samples.jsonl", "/tmp/samples.jsonl")
    from evalplus.evaluate import evaluate
    evaluate(dataset="mbpp", samples="/tmp/samples.jsonl", parallel=1, base_only=False,
             test_details=False, version="v0.2.0")
    shutil.copyfile("/tmp/samples_eval_results.json", "/output/results.json")


if __name__ == "__main__":
    main()
