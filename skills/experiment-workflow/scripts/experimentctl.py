#!/usr/bin/env python3
"""Portable experiment registration and local execution helpers."""

from __future__ import annotations

import argparse
from functools import wraps
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PLAN_SECTIONS = (
    "Research question",
    "Claim (H1)",
    "Null (H0)",
    "Variables",
    "Baselines",
    "Primary metric",
    "Success criterion",
    "Aggregation and significance",
    "Seed rationale and budget",
)
PLACEHOLDER_RE = re.compile(r"\b(?:TODO|TBD|FILL IN|CHANGE_ME)\b|<[^>]+>|\[fill in\]", re.IGNORECASE)


class ExperimentError(Exception):
    pass


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def root_path(value: str) -> Path:
    return Path(value).resolve()


def contained(base: Path, relative: str | Path) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute():
        raise ExperimentError(f"path must be relative: {relative}")
    result = (base / candidate).resolve()
    try:
        result.relative_to(base.resolve())
    except ValueError as exc:
        raise ExperimentError(f"path escapes its allowed root: {relative}") from exc
    return result


def experiment_dir(root: Path, slug: str) -> Path:
    if not SLUG_RE.fullmatch(slug):
        raise ExperimentError("experiment id must use lowercase letters, digits, and hyphens")
    return contained(root, Path("experiments") / slug)


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ExperimentError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ExperimentError(f"expected a JSON object in {path}")
    return value


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    payload = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


ALLOWED_TRANSITIONS = {
    "registered": {"running", "execution_complete"},
    "running": {"registered", "execution_complete", "uncertain"},
    "uncertain": {"registered", "execution_complete"},
    "execution_complete": {"results_pending"},
    "results_pending": {"results_pending", "results_documented"},
    "results_documented": set(),
}


def state_path(directory: Path) -> Path:
    return contained(directory, "state.json")


def read_state(directory: Path, slug: str) -> dict[str, Any]:
    path = state_path(directory)
    if not path.exists():
        return {"schemaVersion": SCHEMA_VERSION, "slug": slug, "registrations": {}}
    state = read_json(path)
    if state.get("schemaVersion") != SCHEMA_VERSION or state.get("slug") != slug or not isinstance(state.get("registrations"), dict):
        raise ExperimentError(f"invalid state.json for {slug}")
    return state


def record_registration(directory: Path, slug: str, registration_id: str) -> None:
    state = read_state(directory, slug)
    if registration_id in state["registrations"]:
        raise ExperimentError(f"registration already exists in process state: {registration_id}")
    state["registrations"][registration_id] = {
        "status": "registered",
        "events": [{"from": "draft", "to": "registered", "at": now_utc(), "reason": "exact plan/config snapshot registered"}],
    }
    state["currentRegistration"] = registration_id
    state["status"] = "registered"
    atomic_json(state_path(directory), state)


def transition_state(directory: Path, slug: str, registration_id: str, new_status: str, reason: str) -> None:
    state = read_state(directory, slug)
    entry = state["registrations"].get(registration_id)
    if not isinstance(entry, dict):
        raise ExperimentError(f"registration is absent from state.json: {registration_id}")
    old_status = entry.get("status")
    if new_status not in ALLOWED_TRANSITIONS.get(old_status, set()):
        raise ExperimentError(f"invalid state transition for {registration_id}: {old_status} -> {new_status}")
    entry["status"] = new_status
    entry.setdefault("events", []).append({"from": old_status, "to": new_status, "at": now_utc(), "reason": reason})
    if state.get("currentRegistration") == registration_id:
        state["status"] = new_status
    atomic_json(state_path(directory), state)


def registration_status(directory: Path, slug: str, registration_id: str) -> str:
    state = read_state(directory, slug)
    entry = state["registrations"].get(registration_id)
    if not isinstance(entry, dict) or not isinstance(entry.get("status"), str):
        raise ExperimentError(f"registration is absent from state.json: {registration_id}")
    return entry["status"]


def acquire_operation_lock(directory: Path, operation: str, recover_stale: bool = False) -> Path:
    lock_path = contained(directory, "operation.lock")
    if lock_path.exists():
        if not recover_stale:
            raise ExperimentError("operation.lock exists; inspect status and resolve only after confirming the prior process stopped")
        lock_path.unlink()
    try:
        with lock_path.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump({"operation": operation, "pid": os.getpid(), "startedAt": now_utc()}, handle)
            handle.write("\n")
    except FileExistsError as exc:
        raise ExperimentError("another experiment operation is in progress") from exc
    return lock_path


def release_operation_lock(lock_path: Path) -> None:
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass


def serialized_experiment(function):
    @wraps(function)
    def wrapper(root: Path, slug: str, *args, **kwargs):
        directory = experiment_dir(root, slug)
        lock_path = acquire_operation_lock(directory, function.__name__)
        try:
            return function(root, slug, *args, **kwargs)
        finally:
            release_operation_lock(lock_path)
    return wrapper


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def plan_sections(text: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        heading = re.match(r"^##\s+(.+?)\s*#*\s*$", line)
        if heading:
            current = heading.group(1)
            sections.setdefault(current, [])
        elif current is not None:
            sections[current].append(line)
    return {name: "\n".join(lines).strip() for name, lines in sections.items()}


def validate_plan(text: str) -> list[str]:
    sections = plan_sections(text)
    errors: list[str] = []
    for name in PLAN_SECTIONS:
        body = sections.get(name, "")
        if not body:
            errors.append(f"plan.md needs a filled '## {name}' section")
        elif PLACEHOLDER_RE.search(body):
            errors.append(f"plan.md section '{name}' still contains a placeholder")
    return errors


def validate_config(config: dict[str, Any], slug: str, root: Path) -> list[str]:
    errors: list[str] = []
    if config.get("schemaVersion") != SCHEMA_VERSION:
        errors.append(f"experiment.json schemaVersion must be {SCHEMA_VERSION}")
    if config.get("slug") != slug:
        errors.append(f"experiment.json slug must equal '{slug}'")
    for field in ("datasetIdentity", "splitIdentity"):
        if not isinstance(config.get(field), str) or not config[field].strip() or PLACEHOLDER_RE.search(config[field]):
            errors.append(f"experiment.json needs a non-empty {field}")

    seeds = config.get("seedSet")
    if not isinstance(seeds, list) or len(seeds) < 1 or any(not isinstance(seed, int) or isinstance(seed, bool) for seed in seeds):
        errors.append("seedSet must contain at least one integer seed")
    elif len(set(seeds)) != len(seeds):
        errors.append("seedSet must not contain duplicates")

    command = config.get("command")
    if not isinstance(command, list) or not command or any(not isinstance(part, str) or not part for part in command):
        errors.append("command must be a non-empty argv array of non-empty strings")
    else:
        if any(PLACEHOLDER_RE.search(part) for part in command):
            errors.append("command still contains a placeholder")
        try:
            for part in command:
                part.format_map({"seed": "0", "run_dir": "RUN_DIR", "experiment_dir": "EXPERIMENT_DIR", "data_amount": "1000", "variant_id": "baseline", "variant_label": "Baseline"})
        except (KeyError, ValueError) as exc:
            errors.append(f"command contains an unsupported placeholder: {exc}")
        joined_command = "\0".join(command)
        if "{seed}" not in joined_command:
            errors.append("command must include the {seed} placeholder")
        if "{data_amount}" not in joined_command:
            errors.append("command must include the {data_amount} placeholder")

    data_amount = config.get("defaultDataAmount")
    if not isinstance(data_amount, int) or isinstance(data_amount, bool) or data_amount < 1:
        errors.append("defaultDataAmount must be a positive integer number of examples")

    variants = config.get("variants")
    if not isinstance(variants, list) or len(variants) < 2:
        errors.append("variants must contain at least a baseline and one ablation")
    else:
        ids: set[str] = set()
        for variant in variants:
            if not isinstance(variant, dict):
                errors.append("each variant must be a JSON object")
                continue
            variant_id = variant.get("id")
            if not isinstance(variant_id, str) or not SLUG_RE.fullmatch(variant_id):
                errors.append("each variant id must use lowercase letters, digits, and hyphens")
            elif variant_id in ids:
                errors.append(f"duplicate variant id: {variant_id}")
            else:
                ids.add(variant_id)
            if not isinstance(variant.get("label"), str) or not variant["label"].strip() or PLACEHOLDER_RE.search(variant["label"]):
                errors.append(f"variant {variant_id!r} needs a non-empty label")
            args = variant.get("args", [])
            if not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
                errors.append(f"variant {variant_id!r} args must be an array of strings")
            else:
                if any(PLACEHOLDER_RE.search(arg) for arg in args):
                    errors.append(f"variant {variant_id!r} args still contain a placeholder")
                try:
                    for arg in args:
                        arg.format_map({"seed": "0", "run_dir": "RUN_DIR", "experiment_dir": "EXPERIMENT_DIR", "data_amount": "1000", "variant_id": "baseline", "variant_label": "Baseline"})
                except (KeyError, ValueError) as exc:
                    errors.append(f"variant {variant_id!r} contains an unsupported placeholder: {exc}")
            amount = variant.get("dataAmount", data_amount)
            if not isinstance(amount, int) or isinstance(amount, bool) or amount < 1:
                errors.append(f"variant {variant_id!r} dataAmount must be a positive integer")
            elif variant_id != "baseline" and not args and amount == data_amount:
                errors.append(f"variant {variant_id!r} must change argv args or override dataAmount")
        if "baseline" not in ids:
            errors.append("variants must include an id named 'baseline'")

    working = config.get("workingDirectory")
    if not isinstance(working, str):
        errors.append("workingDirectory must be a repository-relative path")
    else:
        try:
            if not contained(root, working).is_dir():
                errors.append(f"workingDirectory does not exist: {working}")
        except ExperimentError as exc:
            errors.append(str(exc))

    timeout = config.get("timeoutSeconds")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout < 1 or timeout > 86400:
        errors.append("timeoutSeconds must be an integer from 1 to 86400")

    outputs = config.get("expectedOutputs")
    if not isinstance(outputs, list) or not outputs or any(not isinstance(item, str) or not item for item in outputs):
        errors.append("expectedOutputs must list at least one relative output path")
    else:
        for item in outputs:
            parts = item.replace("\\", "/").split("/")
            if item.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", item) or any(part in ("", ".", "..") for part in parts):
                errors.append(f"expected output must stay inside the run directory: {item}")

    env_names = config.get("environmentNames", [])
    if not isinstance(env_names, list) or any(not isinstance(item, str) or not ENV_RE.fullmatch(item) for item in env_names):
        errors.append("environmentNames must be an array of environment variable names")
    if isinstance(env_names, list) and len(set(env_names)) != len(env_names):
        errors.append("environmentNames must not contain duplicates")
    return errors


def load_draft(root: Path, slug: str) -> tuple[Path, dict[str, Any], bytes, bytes, list[str]]:
    directory = experiment_dir(root, slug)
    if not directory.is_dir():
        raise ExperimentError(f"experiment does not exist: {slug}; run init first")
    config_path = contained(directory, "experiment.json")
    plan_path = contained(directory, "plan.md")
    config = read_json(config_path)
    try:
        config_bytes = config_path.read_bytes()
        plan_bytes = plan_path.read_bytes()
        plan_text = plan_bytes.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise ExperimentError(f"cannot read experiment plan/config: {exc}") from exc
    errors = validate_config(config, slug, root) + validate_plan(plan_text)
    return directory, config, plan_bytes, config_bytes, errors


def skill_template(name: str, slug: str) -> str:
    template_path = Path(__file__).resolve().parents[1] / "templates" / name
    try:
        return template_path.read_text(encoding="utf-8").replace("{{slug}}", slug)
    except (OSError, UnicodeError) as exc:
        raise ExperimentError(f"cannot read skill template {name}: {exc}") from exc


def update_experiments_index(root: Path, slug: str | None = None) -> None:
    index_path = contained(root, "experiments/README.md")
    index_path.parent.mkdir(parents=True, exist_ok=True)
    if not index_path.exists():
        index_path.write_text("# Experiments\n\n", encoding="utf-8")
    text = index_path.read_text(encoding="utf-8")
    lines = ["- [User preferences](user-preferences.md)\n"]
    if slug is not None:
        lines.append(f"- [{slug}]({slug}/README.md)\n")
    with index_path.open("a", encoding="utf-8", newline="\n") as handle:
        for line in lines:
            if line not in text:
                if text and not text.endswith("\n"):
                    handle.write("\n")
                    text += "\n"
                handle.write(line)
                text += line


def ensure_user_preferences(root: Path) -> None:
    path = contained(root, Path("experiments") / "user-preferences.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(skill_template("user-preferences.md", ""))
    except FileExistsError:
        if not path.is_file():
            raise ExperimentError(f"user preferences path exists but is not a file: {path}")


def init_user_preferences(root: Path) -> int:
    ensure_user_preferences(root)
    update_experiments_index(root)
    print("Prepared user preference record: experiments/user-preferences.md")
    return 0


def init_experiment(root: Path, slug: str) -> int:
    directory = experiment_dir(root, slug)
    templates = {
        "README.md": skill_template("experiment-readme.md", slug),
        "plan.md": skill_template("plan.md", slug),
        "references.md": skill_template("references.md", slug),
        "learnings.md": skill_template("learnings.md", slug),
        "results/README.md": skill_template("results-index.md", slug),
    }
    try:
        directory.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise ExperimentError(f"refusing to overwrite existing experiment: {slug}") from exc
    config = {
        "schemaVersion": SCHEMA_VERSION,
        "slug": slug,
        "datasetIdentity": "",
        "splitIdentity": "",
        "defaultDataAmount": None,
        "seedSet": [1],
        "variants": [
            {"id": "baseline", "label": "Baseline", "args": []},
            {"id": "ablation", "label": "Ablation", "args": ["--ablation", "CHANGE_ME"]},
        ],
        "command": ["python", "train.py", "--seed", "{seed}", "--data-amount", "{data_amount}", "--output", "{run_dir}/metrics.json"],
        "workingDirectory": ".",
        "timeoutSeconds": 3600,
        "expectedOutputs": ["metrics.json"],
        "environmentNames": [],
    }
    for relative, content in templates.items():
        target = contained(directory, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
    with (directory / "experiment.json").open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(config, handle, indent=2)
        handle.write("\n")
    atomic_json(directory / "state.json", {"schemaVersion": SCHEMA_VERSION, "slug": slug, "status": "draft", "currentRegistration": None, "draftCreatedAt": now_utc(), "registrations": {}})
    ensure_user_preferences(root)
    update_experiments_index(root, slug)
    print(f"Created experiment scaffold: experiments/{slug}/")
    return 0


def validate_experiment(root: Path, slug: str, quiet: bool = False) -> tuple[Path, dict[str, Any], bytes, bytes]:
    directory, config, plan_bytes, config_bytes, errors = load_draft(root, slug)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        raise ExperimentError(f"experiment '{slug}' is incomplete or invalid")
    if not quiet:
        print(f"Valid experiment draft: {slug}")
    return directory, config, plan_bytes, config_bytes


def registration_digest(plan_bytes: bytes, config_bytes: bytes) -> str:
    digest = hashlib.sha256()
    digest.update(b"plan\0")
    digest.update(plan_bytes)
    digest.update(b"\0config\0")
    digest.update(config_bytes)
    return "sha256-" + digest.hexdigest()


@serialized_experiment
def register_experiment(root: Path, slug: str) -> int:
    directory, config, plan_bytes, config_bytes = validate_experiment(root, slug, quiet=True)
    state = read_state(directory, slug)
    unfinished = [rid for rid, entry in state["registrations"].items() if entry.get("status") != "results_documented"]
    if unfinished:
        raise ExperimentError(f"finish or resolve existing registration(s) before registering another: {unfinished}")
    registration_id = registration_digest(plan_bytes, config_bytes)
    if registration_id in state["registrations"]:
        raise ExperimentError(f"this exact plan is already registered: {registration_id}")
    registration_root = contained(directory, "registrations")
    registration_root.mkdir(parents=True, exist_ok=True)
    target = contained(registration_root, registration_id)
    if target.exists():
        try:
            _, stored_plan, stored_config = load_registration(directory, slug, registration_id)
        except (ExperimentError, OSError, ValueError) as exc:
            raise ExperimentError(f"registration snapshot exists but cannot be safely recovered: {target}: {exc}") from exc
        if stored_plan != plan_bytes or stored_config != config_bytes:
            raise ExperimentError(f"existing registration snapshot does not match the current draft: {target}")
        record_registration(directory, slug, registration_id)
        print(f"Recovered registered {slug}: {registration_id}")
        return 0

    # A previous process may have stopped before atomically publishing its
    # staged snapshot. Only remove transaction directories created by this
    # helper, while holding the experiment operation lock.
    for staged in registration_root.glob(".pending-sha256-*"):
        if staged.is_dir() and not staged.is_symlink() and re.fullmatch(
            r"\.pending-sha256-[0-9a-f]{64}-[A-Za-z0-9_-]+", staged.name
        ):
            shutil.rmtree(staged)

    staging = Path(tempfile.mkdtemp(prefix=f".pending-{registration_id}-", dir=registration_root))
    try:
        (staging / "plan.md").write_bytes(plan_bytes)
        (staging / "experiment.json").write_bytes(config_bytes)
        metadata = {
            "schemaVersion": SCHEMA_VERSION,
            "registrationId": registration_id,
            "experiment": slug,
            "createdAt": now_utc(),
            "planSha256": sha256_bytes(plan_bytes),
            "configSha256": sha256_bytes(config_bytes),
        }
        atomic_json(staging / "registration.json", metadata)
        os.replace(staging, target)
    finally:
        if staging.exists():
            shutil.rmtree(staging)

    record_registration(directory, slug, registration_id)
    print(f"Registered {slug}: {registration_id}")
    return 0


def load_registration(directory: Path, slug: str, registration_id: str) -> tuple[dict[str, Any], bytes, bytes]:
    if not re.fullmatch(r"sha256-[0-9a-f]{64}", registration_id):
        raise ExperimentError("registration id must be the full sha256-... value printed by register")
    target = contained(directory, Path("registrations") / registration_id)
    metadata = read_json(contained(target, "registration.json"))
    plan_bytes = contained(target, "plan.md").read_bytes()
    config_bytes = contained(target, "experiment.json").read_bytes()
    config = json.loads(config_bytes.decode("utf-8"))
    if not isinstance(config, dict):
        raise ExperimentError("registered experiment.json is not an object")
    expected = registration_digest(plan_bytes, config_bytes)
    if metadata.get("registrationId") != expected or expected != registration_id:
        raise ExperimentError("registered plan/config digest does not match its registration id")
    if metadata.get("experiment") != slug:
        raise ExperimentError("registration belongs to a different experiment")
    errors = validate_config(config, slug, directory.parents[1]) + validate_plan(plan_bytes.decode("utf-8"))
    if errors:
        raise ExperimentError("registered experiment is invalid: " + "; ".join(errors))
    return config, plan_bytes, config_bytes


def git_facts(root: Path) -> dict[str, Any]:
    try:
        head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5, check=True)
        status = subprocess.run(["git", "-C", str(root), "status", "--porcelain"], capture_output=True, text=True, timeout=5, check=True)
        return {"available": True, "head": head.stdout.strip(), "dirty": bool(status.stdout.strip())}
    except (OSError, subprocess.SubprocessError):
        return {"available": False, "head": None, "dirty": None}


def output_facts(run_dir: Path, outputs: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    facts: list[dict[str, Any]] = []
    missing: list[str] = []
    for relative in outputs:
        path = contained(run_dir, relative)
        if not path.is_file():
            missing.append(relative)
            facts.append({"path": relative, "exists": False})
        else:
            facts.append({"path": relative, "exists": True, "sizeBytes": path.stat().st_size, "sha256": sha256_file(path)})
    return facts, missing


def run_attempt(root: Path, directory: Path, slug: str, registration_id: str, config: dict[str, Any], plan_bytes: bytes, config_bytes: bytes, variant: dict[str, Any], seed: int) -> str:
    data_amount = variant.get("dataAmount", config["defaultDataAmount"])
    run_id = "run-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    variant_id = variant["id"]
    runs_root = contained(root, Path(".experiment-runs") / slug / registration_id / variant_id / f"seed-{seed}")
    runs_root.mkdir(parents=True, exist_ok=True)
    run_dir = contained(runs_root, run_id)
    run_dir.mkdir()
    (run_dir / "plan.md").write_bytes(plan_bytes)
    (run_dir / "experiment.json").write_bytes(config_bytes)

    run_dir_abs = str(run_dir.resolve())
    exp_dir_abs = str(directory.resolve())
    format_values = {
        "seed": str(seed),
        "run_dir": run_dir_abs,
        "experiment_dir": exp_dir_abs,
        "data_amount": str(data_amount),
        "variant_id": variant_id,
        "variant_label": variant["label"],
    }
    command = [part.format_map(format_values) for part in config["command"]]
    command.extend(arg.format_map(format_values) for arg in variant.get("args", []))
    working = contained(root, config["workingDirectory"])
    env = os.environ.copy()
    env.update({
        "EXPERIMENT_ID": slug,
        "EXPERIMENT_REGISTRATION": registration_id,
        "EXPERIMENT_SEED": str(seed),
        "EXPERIMENT_RUN_DIR": run_dir_abs,
        "EXPERIMENT_VARIANT_ID": variant_id,
        "EXPERIMENT_VARIANT": variant["label"],
        "EXPERIMENT_DATA_AMOUNT": str(data_amount),
    })
    env_facts = [{"name": name, "set": name in os.environ} for name in sorted(config.get("environmentNames", []))]
    started = now_utc()
    manifest: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "runId": run_id,
        "experiment": slug,
        "registrationId": registration_id,
        "variantId": variant_id,
        "variantLabel": variant["label"],
        "dataAmount": data_amount,
        "seed": seed,
        "status": "running",
        "startedAt": started,
        "endedAt": None,
        "command": command,
        "workingDirectory": str(working),
        "runArtifacts": {"localDirectory": run_dir_abs, "stdout": "stdout.log", "stderr": "stderr.log"},
        "datasetIdentity": config["datasetIdentity"],
        "splitIdentity": config["splitIdentity"],
        "environmentNames": env_facts,
        "runtime": {"python": sys.version.split()[0], "platform": platform.platform()},
        "git": git_facts(root),
        "planSha256": sha256_bytes(plan_bytes),
        "configSha256": sha256_bytes(config_bytes),
        "expectedOutputs": list(config["expectedOutputs"]),
        "outputs": [],
        "exitCode": None,
    }
    manifest_path = run_dir / "manifest.json"
    atomic_json(manifest_path, manifest)

    timed_out = False
    launch_error: str | None = None
    return_code: int | None = None
    with (run_dir / "stdout.log").open("wb") as stdout, (run_dir / "stderr.log").open("wb") as stderr:
        try:
            process = subprocess.Popen(command, cwd=working, env=env, stdout=stdout, stderr=stderr, shell=False)
        except OSError as exc:
            launch_error = str(exc)
            process = None
        if process is not None:
            manifest["pid"] = process.pid
            try:
                atomic_json(manifest_path, manifest)
                try:
                    return_code = process.wait(timeout=config["timeoutSeconds"])
                except subprocess.TimeoutExpired:
                    timed_out = True
                    process.terminate()
                    try:
                        return_code = process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        return_code = process.wait()
            except KeyboardInterrupt:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                manifest["status"] = "interrupted"
                manifest["endedAt"] = now_utc()
                manifest["exitCode"] = process.returncode
                atomic_json(manifest_path, manifest)
                print(f"Interrupted run saved at {run_dir}", file=sys.stderr)
                raise
            except Exception:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                raise

    outputs, missing = output_facts(run_dir, config["expectedOutputs"])
    manifest["endedAt"] = now_utc()
    manifest["exitCode"] = return_code
    manifest["outputs"] = outputs
    if launch_error is not None:
        manifest["status"] = "launch_error"
        manifest["error"] = launch_error
    elif timed_out:
        manifest["status"] = "timed_out"
    elif return_code != 0:
        manifest["status"] = "failed"
    elif missing:
        manifest["status"] = "incomplete"
        manifest["missingOutputs"] = missing
    else:
        manifest["status"] = "succeeded"
    atomic_json(manifest_path, manifest)
    print(f"{manifest['status']}: {run_dir}")
    return manifest["status"]


def cell_attempts(root: Path, slug: str, registration_id: str, variant_id: str, seed: int) -> list[tuple[Path, dict[str, Any]]]:
    cell = contained(root, Path(".experiment-runs") / slug / registration_id / variant_id / f"seed-{seed}")
    attempts: list[tuple[Path, dict[str, Any]]] = []
    if cell.is_dir():
        for path in sorted(cell.glob("*/manifest.json")):
            try:
                attempts.append((path, read_json(path)))
            except ExperimentError:
                continue
    return attempts


def matrix_complete(root: Path, slug: str, registration_id: str, config: dict[str, Any]) -> bool:
    terminal = {"succeeded", "failed", "incomplete", "launch_error", "uncertain_resolved"}
    for variant in config["variants"]:
        for seed in config["seedSet"]:
            attempts = cell_attempts(root, slug, registration_id, variant["id"], seed)
            if not any(item.get("status") in terminal for _, item in attempts):
                return False
    return True


@serialized_experiment
def run_experiment(root: Path, slug: str, registration_id: str, requested_seeds: list[int] | None, requested_variants: list[str] | None) -> int:
    directory = experiment_dir(root, slug)
    config, plan_bytes, config_bytes = load_registration(directory, slug, registration_id)
    state = registration_status(directory, slug, registration_id)
    if state != "registered":
        raise ExperimentError(f"registration is in state '{state}'; only 'registered' plans can run")
    seeds = config["seedSet"] if requested_seeds is None else requested_seeds
    if len(set(seeds)) != len(seeds) or any(seed not in config["seedSet"] for seed in seeds):
        raise ExperimentError(f"requested seeds must be unique members of registered seedSet {config['seedSet']}")
    variants = config["variants"]
    variant_ids = [item["id"] for item in variants]
    selected_ids = variant_ids if requested_variants is None else requested_variants
    if len(set(selected_ids)) != len(selected_ids) or any(item not in variant_ids for item in selected_ids):
        raise ExperimentError(f"requested variants must be unique registered ids: {variant_ids}")

    explicit_selection = requested_seeds is not None or requested_variants is not None
    pending: list[tuple[dict[str, Any], int]] = []
    for variant in variants:
        if variant["id"] not in selected_ids:
            continue
        for seed in seeds:
            existing = cell_attempts(root, slug, registration_id, variant["id"], seed)
            if existing:
                if explicit_selection:
                    raise ExperimentError(f"{variant['id']} seed {seed} already has an attempt; retries require a new registration")
                print(f"Skipping recorded cell {variant['id']} seed {seed}; no retry is automatic.")
                continue
            pending.append((variant, seed))
    if not pending:
        raise ExperimentError("no unattempted cells remain for this registration")

    transition_state(directory, slug, registration_id, "running", "local matrix execution started")
    outcomes: list[str] = []
    try:
        for variant, seed in pending:
            outcome = run_attempt(root, directory, slug, registration_id, config, plan_bytes, config_bytes, variant, seed)
            outcomes.append(outcome)
            if outcome == "timed_out":
                transition_state(directory, slug, registration_id, "uncertain", "timed-out process tree must be inspected before continuing")
                print("Matrix paused after timeout; inspect for child processes, then resolve explicitly.", file=sys.stderr)
                return 1
    except KeyboardInterrupt:
        transition_state(directory, slug, registration_id, "uncertain", "runner interrupted; inspect process and outputs before resolving")
        print("Matrix interrupted; completed attempts remain recorded and unstarted cells remain pending.", file=sys.stderr)
        return 130
    except Exception:
        transition_state(directory, slug, registration_id, "uncertain", "runner exited unexpectedly; inspect process and outputs before resolving")
        raise

    if matrix_complete(root, slug, registration_id, config):
        transition_state(directory, slug, registration_id, "execution_complete", "every declared variant/seed cell has a terminal attempt")
    else:
        transition_state(directory, slug, registration_id, "registered", "selected cells finished; matrix still has unattempted cells")
    succeeded = outcomes.count("succeeded")
    print(f"Invocation complete: {succeeded}/{len(outcomes)} attempts succeeded; failed cells were not retried.")
    return 0 if all(item == "succeeded" for item in outcomes) else 1


def resolve_uncertain(root: Path, slug: str, registration_id: str, note: str, process_stopped: bool) -> int:
    if not process_stopped:
        raise ExperimentError("resolve requires --process-stopped after you inspect the process and confirm it has stopped")
    if not note.strip():
        raise ExperimentError("resolve requires a non-empty --note")
    directory = experiment_dir(root, slug)
    lock_path = acquire_operation_lock(directory, "resolve", recover_stale=True)
    try:
        return _resolve_uncertain_locked(root, slug, registration_id, note)
    finally:
        release_operation_lock(lock_path)


def _resolve_uncertain_locked(root: Path, slug: str, registration_id: str, note: str) -> int:
    if not note.strip():
        raise ExperimentError("resolve requires a non-empty --note")
    directory = experiment_dir(root, slug)
    config, _, _ = load_registration(directory, slug, registration_id)
    current = registration_status(directory, slug, registration_id)
    if current not in {"running", "uncertain"}:
        raise ExperimentError(f"resolve only applies to running/uncertain registrations, current state is '{current}'")

    run_root = contained(root, Path(".experiment-runs") / slug / registration_id)
    manifest_paths = run_root.rglob("manifest.json") if run_root.exists() else []
    for path in manifest_paths:
        manifest = read_json(path)
        if manifest.get("status") in {"running", "interrupted", "timed_out"}:
            outputs, missing = output_facts(path.parent, manifest.get("expectedOutputs", []))
            manifest["previousStatus"] = manifest["status"]
            manifest["status"] = "uncertain_resolved"
            manifest["resolvedAt"] = now_utc()
            manifest["resolutionNote"] = note.strip()
            manifest["outputs"] = outputs
            if missing:
                manifest["missingOutputs"] = missing
            atomic_json(path, manifest)

    if current == "running":
        transition_state(directory, slug, registration_id, "uncertain", "operator is resolving an interrupted/unknown run")
    transition_state(directory, slug, registration_id, "registered", "operator confirmed process stopped: " + note.strip())
    if matrix_complete(root, slug, registration_id, config):
        transition_state(directory, slug, registration_id, "execution_complete", "all matrix cells have terminal or explicitly resolved attempts")
    print(f"Resolved {registration_id}; remaining unattempted cells may now be run explicitly.")
    return 0


def result_rows(root: Path, slug: str, registration_id: str, config: dict[str, Any], summary_dir: Path) -> str:
    rows = ["| Variant | Seed | Data examples | Status | Outputs | Run manifest |", "|---|---:|---:|---|---|---|"]
    for variant in config["variants"]:
        amount = variant.get("dataAmount", config["defaultDataAmount"])
        for seed in config["seedSet"]:
            attempts = cell_attempts(root, slug, registration_id, variant["id"], seed)
            if not attempts:
                rows.append(f"| {variant['label']} | {seed} | {amount} | pending | — | — |")
                continue
            for manifest_path, manifest in attempts:
                outputs = ", ".join(
                    f"{item.get('path')} sha256:{item.get('sha256', 'missing')}"
                    for item in manifest.get("outputs", [])
                ) or "none recorded"
                manifest_snapshots = summary_dir / "run-manifests"
                manifest_snapshots.mkdir(parents=True, exist_ok=True)
                snapshot_name = f"{variant['id']}-seed-{seed}-{manifest.get('runId', 'run')}.json"
                manifest_snapshot = contained(manifest_snapshots, snapshot_name)
                if manifest_snapshot.exists():
                    if manifest_snapshot.read_bytes() != manifest_path.read_bytes():
                        raise ExperimentError(f"manifest snapshot conflict: {manifest_snapshot}")
                else:
                    with manifest_snapshot.open("xb") as handle:
                        handle.write(manifest_path.read_bytes())
                relative_manifest = os.path.relpath(manifest_snapshot, start=summary_dir).replace("\\", "/")
                label = str(manifest.get("runId", "run")).replace("|", "\\|")
                status = str(manifest.get("status", "unknown")).replace("|", "\\|")
                variant_label = str(variant["label"]).replace("|", "\\|").replace("\n", " ")
                outputs = outputs.replace("|", "\\|").replace("\n", " ")
                rows.append(f"| {variant_label} | {seed} | {amount} | {status} | {outputs} | [{label}]({relative_manifest}) |")
    return "\n".join(rows)


@serialized_experiment
def report_experiment(root: Path, slug: str, registration_id: str) -> int:
    directory = experiment_dir(root, slug)
    config, _, _ = load_registration(directory, slug, registration_id)
    current = registration_status(directory, slug, registration_id)
    if current not in {"execution_complete", "results_pending"}:
        raise ExperimentError(f"report requires execution_complete/results_pending, current state is '{current}'")
    if not matrix_complete(root, slug, registration_id, config):
        raise ExperimentError("report requires a terminal attempt for every registered variant/seed cell")

    result_dir = contained(directory, Path("results") / registration_id)
    result_dir.mkdir(parents=True, exist_ok=True)
    existing = [int(match.group(1)) for path in result_dir.glob("summary-*.md") if (match := re.fullmatch(r"summary-(\d{3,})\.md", path.name))]
    revision = max(existing, default=0) + 1
    summary_name = f"summary-{revision:03d}.md"
    summary_path = contained(result_dir, summary_name)
    template = skill_template("summary.md", slug)
    content = template.replace("{{registration_id}}", registration_id)
    content = content.replace("{{created_at}}", now_utc())
    content = content.replace("{{matrix}}", result_rows(root, slug, registration_id, config, result_dir))
    with summary_path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(content)

    results_index = contained(directory, "results/README.md")
    relative_summary = f"{registration_id}/{summary_name}"
    index_line = f"- [{summary_name}]({relative_summary}) — registration `{registration_id}`\n"
    index_text = results_index.read_text(encoding="utf-8")
    if index_line not in index_text:
        with results_index.open("a", encoding="utf-8", newline="\n") as handle:
            if index_text and not index_text.endswith("\n"):
                handle.write("\n")
            handle.write(index_line)
    if current == "execution_complete":
        transition_state(directory, slug, registration_id, "results_pending", f"created {relative_summary}")
    else:
        transition_state(directory, slug, registration_id, "results_pending", f"created additional summary revision {relative_summary}")
    print(f"Created result summary: {summary_path}")
    return 0


RESULT_SECTIONS = (
    "Observed results",
    "Analysis against the registered criterion",
    "Conclusion and decision",
    "Limitations and uncertainty",
    "Next steps",
)


@serialized_experiment
def add_learning(root: Path, slug: str, registration_id: str, summary_relative: str, title: str, finding: str, limits: str, decision: str) -> int:
    directory = experiment_dir(root, slug)
    if registration_status(directory, slug, registration_id) != "results_pending":
        raise ExperimentError("learning entries require a registration in results_pending state")
    summary_path = contained(directory, summary_relative)
    required_parent = contained(directory, Path("results") / registration_id)
    if not summary_path.is_relative_to(required_parent) or not re.fullmatch(r"summary-\d{3,}\.md", summary_path.name) or not summary_path.is_file():
        raise ExperimentError("summary must be an existing summary-NNN.md under results/<registration-id>/")
    fields = {"title": title, "finding": finding, "limits": limits, "decision": decision}
    if any(not value.strip() for value in fields.values()):
        raise ExperimentError("title, finding, limits, and decision must all be non-empty")
    if "\n" in title or "\r" in title:
        raise ExperimentError("learning title must be one line")
    learnings_path = contained(directory, "learnings.md")
    current = learnings_path.read_text(encoding="utf-8")
    relative_summary = summary_path.relative_to(directory).as_posix()
    if registration_id in current and relative_summary in current:
        raise ExperimentError("a learning entry already references this registration and summary")
    entry = (
        f"\n## {datetime.now(timezone.utc).date().isoformat()} — {title.strip()} — registration: {registration_id}\n\n"
        f"- Finding: {finding.strip()}\n"
        f"- Evidence / result summary: [{summary_path.name}]({relative_summary})\n"
        f"- Limits and uncertainty: {limits.strip()}\n"
        f"- Decision or next experiment: {decision.strip()}\n"
    )
    with learnings_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(entry)
    print(f"Appended learning entry to {learnings_path}")
    return 0


@serialized_experiment
def finalize_results(root: Path, slug: str, registration_id: str, summary_relative: str) -> int:
    directory = experiment_dir(root, slug)
    if registration_status(directory, slug, registration_id) != "results_pending":
        raise ExperimentError("finalize requires a registration in results_pending state")
    summary_path = contained(directory, summary_relative)
    required_parent = contained(directory, Path("results") / registration_id)
    if not summary_path.is_relative_to(required_parent) or not re.fullmatch(r"summary-\d{3,}\.md", summary_path.name):
        raise ExperimentError("summary must be a generated summary-NNN.md under results/<registration-id>/")
    try:
        summary_text = summary_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ExperimentError(f"cannot read result summary: {exc}") from exc
    sections = plan_sections(summary_text)
    for heading in RESULT_SECTIONS:
        body = sections.get(heading, "")
        if not body or PLACEHOLDER_RE.search(body):
            raise ExperimentError(f"result summary section '{heading}' must be completed before finalize")

    learnings_path = contained(directory, "learnings.md")
    learnings = learnings_path.read_text(encoding="utf-8")
    relative_summary = summary_path.relative_to(directory).as_posix()
    matching_entry = any(
        registration_id in block and relative_summary in block and not PLACEHOLDER_RE.search(block)
        for block in re.split(r"(?m)(?=^##\s+)", learnings)
    )
    if not matching_entry:
        raise ExperimentError(f"append a dated learning entry in learnings.md linking {relative_summary} and {registration_id}")
    transition_state(directory, slug, registration_id, "results_documented", f"finalized {relative_summary} and linked learning entry")
    print(f"Results documented for {registration_id}")
    return 0


def show_status(root: Path, slug: str) -> int:
    directory = experiment_dir(root, slug)
    if not directory.is_dir():
        raise ExperimentError(f"experiment does not exist: {slug}")
    registrations = contained(directory, "registrations")
    lock_path = contained(directory, "operation.lock")
    operation_in_progress = lock_path.exists()
    if operation_in_progress:
        print("An operation lock exists. Treat the helper operation as active or uncertain; do not start another operation.")
    if not registrations.is_dir():
        state = read_state(directory, slug)
        print(f"Experiment {slug}; state={state.get('status', 'draft')}.")
        if operation_in_progress:
            print("  Next action: wait for the active helper to finish, then inspect status again.")
        else:
            print("  Next action: complete plan.md and experiment.json, validate the draft, then register it.")
        return 0
    any_registration = False
    for registration in sorted(path for path in registrations.iterdir() if path.is_dir()):
        try:
            config, _, _ = load_registration(directory, slug, registration.name)
        except (ExperimentError, OSError, ValueError) as exc:
            print(f"Invalid registration {registration.name}: {exc}")
            continue
        any_registration = True
        registration_state = registration_status(directory, slug, registration.name)
        state_label = "running/unknown" if registration_state == "running" else registration_state
        print(f"Registration {registration.name}; state={state_label}; default data={config['defaultDataAmount']} examples")
        for variant in config["variants"]:
            variant_id = variant["id"]
            amount = variant.get("dataAmount", config["defaultDataAmount"])
            for seed in config["seedSet"]:
                attempts = cell_attempts(root, slug, registration.name, variant_id, seed)
                if not attempts:
                    print(f"  {variant_id} data={amount} examples seed={seed}: pending")
                else:
                    for _, item in attempts:
                        attempt_state = "running/unknown" if item.get("status") == "running" else item.get("status", "unknown")
                        print(f"  {variant_id} data={amount} examples seed={seed}: {attempt_state} {item.get('runId')}")
        print("  Inspect every attempt; no result selection or scientific verdict is applied.")
        next_actions = {
            "registered": "after an explicit user request, run unattempted cells; use selectors only to narrow the matrix",
            "running": "wait for the active helper; if it stopped unexpectedly, inspect the process and outputs before resolving",
            "uncertain": "inspect the PID, logs, process, and outputs; resolve only after confirming the process stopped",
            "execution_complete": "create a result summary with report",
            "results_pending": "complete a summary, append its linked learning entry, then finalize",
            "results_documented": "this registration is complete; a new round requires a revised draft and new registration",
        }
        if operation_in_progress:
            next_action = "wait for the active helper; if it stopped unexpectedly, inspect the process and outputs before resolving or starting another operation"
        else:
            next_action = next_actions.get(registration_state, "inspect the registration state and records before continuing")
        print(f"  Next action: {next_action}.")
    if not any_registration:
        print(f"No valid registered plan for {slug}; state=draft.")
        if operation_in_progress:
            print("  Next action: wait for the active helper to finish, then inspect status again.")
        else:
            print("  Next action: complete plan.md and experiment.json, validate the draft, then register it.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage, register, run, and document local ablation matrices.")
    parser.add_argument("--root", default=".", help="target repository root (default: current directory)")
    subparsers = parser.add_subparsers(dest="command", required=True)
    init_parser = subparsers.add_parser("init", help="create an experiment plan/config scaffold")
    init_parser.add_argument("slug")
    subparsers.add_parser("preferences-init", help="create the shared user preference record if absent")
    validate_parser = subparsers.add_parser("validate", help="validate the draft plan and run config")
    validate_parser.add_argument("slug")
    register_parser = subparsers.add_parser("register", help="freeze exact plan/config bytes before running")
    register_parser.add_argument("slug")
    run_parser = subparsers.add_parser("run", help="run the registered variant-by-seed matrix locally")
    run_parser.add_argument("slug")
    run_parser.add_argument("--registration", required=True)
    run_parser.add_argument("--seed", type=int, action="append", help="run only these seed(s); repeat for multiple seeds (default: all registered)")
    run_parser.add_argument("--variant", action="append", help="run only these variant id(s); repeat to select multiple (default: all registered)")
    status_parser = subparsers.add_parser("status", help="list local attempts for each variant and seed")
    status_parser.add_argument("slug")
    resolve_parser = subparsers.add_parser("resolve", help="record a user-confirmed resolution of uncertain local execution")
    resolve_parser.add_argument("slug")
    resolve_parser.add_argument("--registration", required=True)
    resolve_parser.add_argument("--note", required=True)
    resolve_parser.add_argument("--process-stopped", action="store_true", required=True)
    report_parser = subparsers.add_parser("report", help="create a non-overwriting results summary scaffold")
    report_parser.add_argument("slug")
    report_parser.add_argument("--registration", required=True)
    finalize_parser = subparsers.add_parser("finalize", help="validate and mark a result summary documented")
    finalize_parser.add_argument("slug")
    finalize_parser.add_argument("--registration", required=True)
    finalize_parser.add_argument("--summary", required=True, help="summary path relative to experiments/<slug>")
    learning_parser = subparsers.add_parser("learning-add", help="append a dated evidence-linked learning entry")
    learning_parser.add_argument("slug")
    learning_parser.add_argument("--registration", required=True)
    learning_parser.add_argument("--summary", required=True)
    learning_parser.add_argument("--title", required=True)
    learning_parser.add_argument("--finding", required=True)
    learning_parser.add_argument("--limits", required=True)
    learning_parser.add_argument("--decision", required=True)
    args = parser.parse_args(argv)
    root = root_path(args.root)
    try:
        if not root.is_dir():
            raise ExperimentError(f"target repository root is not a directory: {root}")
        if args.command == "init":
            return init_experiment(root, args.slug)
        if args.command == "preferences-init":
            return init_user_preferences(root)
        if args.command == "validate":
            validate_experiment(root, args.slug)
            return 0
        if args.command == "register":
            return register_experiment(root, args.slug)
        if args.command == "run":
            return run_experiment(root, args.slug, args.registration, args.seed, args.variant)
        if args.command == "resolve":
            return resolve_uncertain(root, args.slug, args.registration, args.note, args.process_stopped)
        if args.command == "report":
            return report_experiment(root, args.slug, args.registration)
        if args.command == "finalize":
            return finalize_results(root, args.slug, args.registration, args.summary)
        if args.command == "learning-add":
            return add_learning(root, args.slug, args.registration, args.summary, args.title, args.finding, args.limits, args.decision)
        return show_status(root, args.slug)
    except (ExperimentError, OSError, ValueError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
