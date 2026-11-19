"""Only prepare_data may access reference/private data. Online loader uses a strict public schema."""
import ast
from dataclasses import fields
import gzip
import hashlib
import json
from pathlib import Path
import random
import re
import urllib.request
from ..logging import safe_id, write_json
from ..schemas import PilotError, PublicTask, TaskManifest, digest, plain


def map_public(task_id, original, extended, source_revision):
    text, assertions = original.get("prompt"), original.get("test_list")
    entry = extended.get("entry_point")
    if str(original.get("task_id")) != task_id.split("/")[-1] or extended.get("task_id") != task_id:
        raise PilotError("dataset_mapping_error", "task ID mismatch")
    if not isinstance(text, str) or not text.strip() or not assertions or not isinstance(entry, str):
        raise PilotError("dataset_mapping_error", "missing public task/assertions/entry")
    functions = {n.name for n in ast.walk(ast.parse(original["code"])) if isinstance(n, ast.FunctionDef)}
    # MBPP+ removes citation URLs from some original statements. Ignore only URLs for
    # correspondence checking; preserve the entire original public text in the export.
    normalized = lambda value: " ".join(re.sub(r"https?://\S+", "", value).split())
    if entry not in functions or normalized(text) not in normalized(extended["prompt"]):
        raise PilotError("dataset_mapping_error", "entry point or task text mismatch")
    for assertion in assertions:
        tree = ast.parse(assertion)
        if len(tree.body) != 1 or not isinstance(tree.body[0], ast.Assert):
            raise PilotError("dataset_mapping_error", "public example is not an assertion")
        if not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == entry for node in ast.walk(tree)):
            raise PilotError("dataset_mapping_error", "assertion does not call entry point")
    # Do not silently install packages required by the canonical source.
    import sys
    for node in ast.walk(ast.parse(original["code"])):
        names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else ([node.module or ""] if isinstance(node, ast.ImportFrom) else [])
        if any(name.split(".")[0] not in sys.stdlib_module_names for name in names):
            raise PilotError("dependency_incompatibility", "original solution uses non-standard library")
    return PublicTask(task_id, text, entry, list(assertions), list(assertions), source_revision)


def prepare_from_files(original_path, plus_path, output, sample_size, seed, source_revision, dataset_version="v0.2.0"):
    root = Path(output)
    if (root / "manifests/pilot_20.json").exists():
        raise PilotError("data_already_prepared", "choose a new output directory to change the manifest")
    original_bytes, plus_bytes = Path(original_path).read_bytes(), Path(plus_path).read_bytes()
    originals = json.loads(original_bytes)
    original_map = {str(item["task_id"]): item for item in originals}
    rows = [json.loads(line) for line in plus_bytes.decode("utf-8").splitlines() if line.strip()]
    extended = {row["task_id"]: row for row in rows}
    if len(extended) != len(rows):
        raise PilotError("dataset_mapping_error", "duplicate extended IDs")
    ids = sorted(extended, key=lambda key: int(key.split("/")[-1]))
    if sample_size > len(ids) or sample_size < 1:
        raise PilotError("configuration_error", "invalid sample size")
    selected = random.Random(seed).sample(ids, sample_size)
    (root / "private").mkdir(parents=True, exist_ok=True)
    (root / "private/mbpp_plus.jsonl").write_bytes(plus_bytes)
    (root / "private/sanitized-mbpp.json").write_bytes(original_bytes)
    errors, hashes = {}, {}
    source = source_revision + ":sha256:" + hashlib.sha256(original_bytes).hexdigest()
    for task_id in selected:
        try:
            task = map_public(task_id, original_map[task_id.split("/")[-1]], extended[task_id], source)
            write_json(root / "public" / (safe_id(task_id) + ".json"), task)
            hashes[task_id] = task.public_payload_hash
        except (PilotError, KeyError, SyntaxError, TypeError) as error:
            errors[task_id] = error.category if isinstance(error, PilotError) else "dataset_mapping_error"
    manifest = TaskManifest(selected, "../public", dataset_version, hashlib.sha256(plus_bytes).hexdigest(), hashes, errors, source, seed)
    path = root / "manifests/pilot_20.json"
    write_json(path, manifest)
    write_json(root / "development_exposed_ids.json", [])
    write_json(root / "preparation_audit.json", {"actual_count": len(ids), "expected_count": 378,
               "count_matches": len(ids) == 378, "dataset_version": dataset_version, "selected_ids": selected,
               "preparation_errors": errors, "source_revision": source_revision,
               "private_dataset_sha256": manifest.dataset_hash})
    return path


def prepare_data(config, output):
    """Network is allowed only in explicit preparation. Raw snapshots are retained offline."""
    root = Path(output)
    if (root / "manifests/pilot_20.json").exists():
        raise PilotError("data_already_prepared")
    root.mkdir(parents=True, exist_ok=True)
    downloads = root / "private/downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    version = config["benchmark"]["dataset_version"]
    def download(url):
        request = urllib.request.Request(url, headers={"User-Agent": "slm-mas-pilot/0.1"})
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.read()
    # Resolve the public upstream to a commit before downloading, rather than trusting a moving branch.
    revision = json.loads(download("https://api.github.com/repos/google-research/google-research/commits?path=mbpp/sanitized-mbpp.json&per_page=1"))[0]["sha"]
    original = downloads / "sanitized-mbpp.json"
    original.write_bytes(download(f"https://raw.githubusercontent.com/google-research/google-research/{revision}/mbpp/sanitized-mbpp.json"))
    plus = downloads / "mbpp_plus.jsonl"
    plus.write_bytes(gzip.decompress(download(f"https://github.com/evalplus/mbppplus_release/releases/download/{version}/MbppPlus.jsonl.gz")))
    return prepare_from_files(original, plus, root, config["benchmark"]["sample_size"], config["benchmark"]["sample_seed"], revision, version)


class MBPPAdapter:
    def load_public_tasks(self, manifest_path):
        path = Path(manifest_path)
        manifest = TaskManifest(**json.loads(path.read_text(encoding="utf-8")))
        exposure_path = path.parent.parent / "development_exposed_ids.json"
        if exposure_path.exists():
            exposed = json.loads(exposure_path.read_text(encoding="utf-8"))
            manifest.development_exposed_ids = sorted(set(manifest.development_exposed_ids) | (set(exposed) & set(manifest.selected_ids)))
        public_root = (path.parent / manifest.public_dir).resolve()
        tasks, errors = {}, dict(manifest.preparation_errors)
        allowed = {f.name for f in fields(PublicTask)}
        for task_id in manifest.selected_ids:
            if task_id in errors:
                continue
            try:
                payload = json.loads((public_root / (safe_id(task_id) + ".json")).read_text(encoding="utf-8"))
                if set(payload) != allowed:
                    raise PilotError("public_schema_error", "private/unknown fields forbidden")
                task = PublicTask(**payload)
                if task.task_id != task_id or task.public_payload_hash != manifest.public_hashes[task_id]:
                    raise PilotError("public_payload_hash_mismatch")
                tasks[task_id] = task
            except (OSError, ValueError, TypeError, PilotError, KeyError) as error:
                errors[task_id] = error.category if isinstance(error, PilotError) else "public_load_error"
        return manifest, tasks, errors
