"""Stage J: build and adversarially verify a Stage I task package in Docker."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from .stage_i import ROLE_DIRS

MODEL_ID = "claude-sonnet-5-5"
MAX_LOG_CHARS = 50_000
DEFAULT_TIMEOUT_SECONDS = 1800
DEFAULT_MEMORY_MB = 4096
DEFAULT_CPUS = 2
DEFAULT_PIDS_LIMIT = 256


class StageJVerificationError(RuntimeError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        self.details = details or {}
        super().__init__(message)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _safe_relative_path(value: Any) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError(f"unsafe workspace path: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"workspace path must be clean and relative: {value!r}")
    return path


def _load_manifest(package_dir: Path) -> dict[str, Any]:
    manifest_path = package_dir / "authoring" / "stage-j.json"
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"required verification manifest is missing: {manifest_path}") from exc
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValueError("authoring/stage-j.json must be an object with version 1")
    base_image = value.get("base_image")
    if not isinstance(base_image, str) or not re.fullmatch(
        r"python:\d+\.\d+(?:\.\d+)?(?:-[a-zA-Z0-9.-]+)?(?:@sha256:[0-9a-f]{64})?",
        base_image,
    ):
        raise ValueError("stage-j.json base_image must be a tagged or digest-pinned official Python image")
    requirements_file = _safe_relative_path(value.get("requirements_file", "requirements.in"))
    build_timeout = value.get("build_timeout_seconds", 3600)
    if not isinstance(build_timeout, int) or isinstance(build_timeout, bool) or not 1 <= build_timeout <= 14400:
        raise ValueError("stage-j.json build_timeout_seconds must be an integer from 1 to 14400")
    for key, default, low, high in (
        ("timeout_seconds", DEFAULT_TIMEOUT_SECONDS, 1, 7200),
        ("memory_mb", DEFAULT_MEMORY_MB, 512, 32768),
        ("pids_limit", DEFAULT_PIDS_LIMIT, 32, 2048),
    ):
        item = value.get(key, default)
        if not isinstance(item, int) or isinstance(item, bool) or not low <= item <= high:
            raise ValueError(f"stage-j.json {key} must be an integer from {low} to {high}")
        value[key] = item
    cpus = value.get("cpus", DEFAULT_CPUS)
    if not isinstance(cpus, (int, float)) or isinstance(cpus, bool) or not 0.5 <= cpus <= 8:
        raise ValueError("stage-j.json cpus must be between 0.5 and 8")
    value["cpus"] = cpus
    for key in ("build_command", "reference_solution_command", "grader_command"):
        _validate_command(value.get(key), key, allow_empty=(key == "build_command"))
    all_case_names = {"reference", "untouched", "reference-replay"}
    for group in ("alternate_solutions", "negative_cases"):
        cases = value.get(group)
        if not isinstance(cases, list) or not cases:
            raise ValueError(f"stage-j.json {group} must contain at least one authoring case")
        names: set[str] = set()
        for index, case in enumerate(cases):
            if not isinstance(case, dict):
                raise ValueError(f"{group}[{index}] must be an object")
            name = case.get("name")
            if (
                not isinstance(name, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", name)
                or ".." in name
                or name.startswith("reference-attempt-")
                or name in all_case_names
            ):
                raise ValueError(f"{group}[{index}] needs a unique safe name (letters, digits, dot, underscore, hyphen)")
            names.add(name)
            all_case_names.add(name)
            if group == "alternate_solutions":
                _validate_command(case.get("command"), f"{group}[{index}].command")
                files = case.get("workspace_files")
                if not isinstance(files, dict) or not files:
                    raise ValueError(f"{group}[{index}].workspace_files must provide an independent solution implementation")
                for relative, content in files.items():
                    _safe_relative_path(relative)
                    if not isinstance(content, str):
                        raise ValueError(f"{group}[{index}] file content must be text")
            else:
                files = case.get("workspace_files")
                if not isinstance(files, dict) or not files:
                    raise ValueError(f"{group}[{index}].workspace_files must contain an incorrect or incomplete fixture")
                for relative, content in files.items():
                    _safe_relative_path(relative)
                    if not isinstance(content, str):
                        raise ValueError(f"{group}[{index}] file content must be text")
                expected = case.get("expected_failed_checks")
                if not isinstance(expected, list) or not expected or any(not isinstance(x, str) or not x.strip() for x in expected):
                    raise ValueError(f"{group}[{index}].expected_failed_checks must name the intended grader checks")
    requirements_path = package_dir / "authoring" / "image" / requirements_file
    if not requirements_path.is_file():
        raise ValueError(f"dependency list is missing: authoring/image/{requirements_file}")
    requirements = [line.strip() for line in requirements_path.read_text(encoding="utf-8").splitlines()]
    requirement_pattern = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*==[A-Za-z0-9][A-Za-z0-9.+!_-]*$")
    for line_number, line in enumerate(requirements, start=1):
        if line and not line.startswith("#") and not requirement_pattern.fullmatch(line):
            raise ValueError(
                f"{requirements_file}:{line_number} must pin one package as name==exact-version; "
                "URLs, ranges, and editable installs are not allowed"
            )
    value["base_image"] = base_image
    value["requirements_file"] = requirements_file.as_posix()
    value["build_timeout_seconds"] = build_timeout
    return value


def _validate_command(command: Any, name: str, *, allow_empty: bool = False) -> None:
    if allow_empty and command == []:
        return
    if not isinstance(command, list) or not command or any(not isinstance(x, str) or not x for x in command):
        raise ValueError(f"{name} must be a non-empty argv array of strings")


def _run_process(argv: list[str], timeout: int, *, preserve_full_output: bool = False) -> dict[str, Any]:
    started = time.monotonic()
    try:
        result = subprocess.run(
            argv,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": tempfile.gettempdir(),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0",
            },
        )
        result_data = {
            "returncode": result.returncode,
            "stdout": result.stdout[-MAX_LOG_CHARS:],
            "stderr": result.stderr[-MAX_LOG_CHARS:],
            "stdout_truncated": len(result.stdout) > MAX_LOG_CHARS,
            "stderr_truncated": len(result.stderr) > MAX_LOG_CHARS,
            "duration_seconds": round(time.monotonic() - started, 3),
            "timed_out": False,
        }
        if preserve_full_output:
            result_data["stdout_full"] = result.stdout
            result_data["stderr_full"] = result.stderr
        return result_data
    except subprocess.TimeoutExpired as exc:
        result_data = {
            "returncode": None,
            "stdout": str(exc.stdout or "")[-MAX_LOG_CHARS:],
            "stderr": str(exc.stderr or "")[-MAX_LOG_CHARS:],
            "duration_seconds": round(time.monotonic() - started, 3),
            "timed_out": True,
            "timeout_seconds": timeout,
        }
        if preserve_full_output:
            result_data["stdout_full"] = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            result_data["stderr_full"] = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return result_data


def _docker_argv(
    package_dir: Path,
    work_dir: Path,
    config: dict[str, Any],
    command: list[str],
    *,
    include_reference: bool = False,
    include_grader: bool = False,
    read_only_workspace: bool = False,
) -> list[str]:
    argv = [
        "docker", "run", "--pull=never", "--rm", "--network=none", "--read-only", "--cap-drop=ALL",
        "--security-opt=no-new-privileges", f"--pids-limit={config['pids_limit']}",
        f"--memory={config['memory_mb']}m", f"--cpus={config['cpus']}",
        "--tmpfs", f"/tmp:rw,exec,nosuid,size={config['memory_mb']}m",
        "--tmpfs", f"/home:rw,nosuid,size=64m",
        "--mount", f"type=bind,src={work_dir.resolve()},dst=/workspace" + (",readonly" if read_only_workspace else ""),
    ]
    if include_reference:
        argv += ["--mount", f"type=bind,src={(package_dir / 'reference').resolve()},dst=/reference,readonly"]
    if include_grader:
        argv += ["--mount", f"type=bind,src={(package_dir / 'grader').resolve()},dst=/grader,readonly"]
    argv += ["--workdir=/workspace", config["docker_image"], *command]
    return argv


def _run_task_command(
    package_dir: Path,
    work_dir: Path,
    config: dict[str, Any],
    command: list[str],
    label: str,
    *,
    include_reference: bool = False,
    include_grader: bool = False,
    read_only_workspace: bool = False,
) -> dict[str, Any]:
    preserve_full = label.startswith(("solution:reference-attempt-", "build:reference-attempt-"))
    result = _run_process(
        _docker_argv(
            package_dir, work_dir, config, command,
            include_reference=include_reference,
            include_grader=include_grader,
            read_only_workspace=read_only_workspace,
        ),
        config["timeout_seconds"],
        preserve_full_output=preserve_full,
    )
    if preserve_full:
        log_dir = Path(config["reference_log_dir"])
        log_dir.mkdir(parents=True, exist_ok=True)
        for stream in ("stdout", "stderr"):
            full_text = result.pop(f"{stream}_full", "")
            log_path = log_dir / f"{label.replace(':', '-')}.{stream}.txt"
            log_path.write_text(full_text, encoding="utf-8")
            result[f"{stream}_log"] = str(log_path.resolve())
            result[f"{stream}_sha256"] = _sha256_file(log_path)
    result["label"] = label
    result["command"] = command
    result["sandbox"] = {
        "network": "disabled",
        "read_only_root": True,
        "memory_mb": config["memory_mb"],
        "cpus": config["cpus"],
        "pids_limit": config["pids_limit"],
        "workspace_read_only": read_only_workspace,
        "reference_mounted": include_reference,
        "grader_mounted": include_grader,
    }
    return result


def _grade_case(
    package_dir: Path,
    workspace: Path,
    config: dict[str, Any],
    name: str,
    *,
    expected_pass: bool,
    expected_failed_checks: list[str] | None = None,
) -> dict[str, Any]:
    result = _run_task_command(
        package_dir, workspace, config, config["grader_command"], f"grade:{name}",
        include_grader=True, read_only_workspace=True,
    )
    result["expected_pass"] = expected_pass
    result["checks_outcome"] = False
    result["grader_result"] = None
    if result["returncode"] == 0:
        try:
            grader_result = json.loads(result["stdout"])
            if not isinstance(grader_result, dict) or not isinstance(grader_result.get("passed"), bool):
                raise ValueError("grader JSON needs a boolean 'passed' field")
            checks = grader_result.get("checks")
            if not isinstance(checks, list) or not checks or any(
                not isinstance(check, dict)
                or not isinstance(check.get("name"), str)
                or not isinstance(check.get("passed"), bool)
                or not isinstance(check.get("reason"), str)
                or not check["reason"].strip()
                for check in checks
            ):
                raise ValueError("grader JSON checks need name, passed, and non-empty reason fields")
            if grader_result["passed"] != all(check["passed"] for check in checks):
                raise ValueError("grader top-level passed must equal the conjunction of its check results")
            observed = {check["name"] for check in checks if not check["passed"]}
            expected_reason_ok = expected_failed_checks is None or set(expected_failed_checks).issubset(observed)
            result["grader_result"] = grader_result
            result["checks_outcome"] = grader_result["passed"] == expected_pass and expected_reason_ok
            if expected_failed_checks is not None:
                result["expected_failed_checks"] = expected_failed_checks
                result["observed_failed_checks"] = sorted(observed)
                result["intended_failure_reason_confirmed"] = expected_reason_ok
        except (json.JSONDecodeError, ValueError) as exc:
            result["grader_protocol_error"] = str(exc)
    result["passed"] = bool(result["checks_outcome"] and result["returncode"] == 0)
    return result


def _run_case(
    package_dir: Path,
    original_workspace: Path,
    temp_root: Path,
    config: dict[str, Any],
    case: dict[str, Any],
) -> dict[str, Any]:
    workspace = temp_root / case["name"]
    _copy_workspace(original_workspace, workspace)
    if case.get("workspace_files"):
        _apply_case_files(workspace, case["workspace_files"])
    result: dict[str, Any] = {"name": case["name"], "steps": [], "passed": False}
    if config["build_command"]:
        build = _run_task_command(
            package_dir, workspace, config, config["build_command"], f"build:{case['name']}"
        )
        result["steps"].append(build)
        if build["returncode"] != 0:
            result["error"] = "workspace build command failed"
            result["solution_ran"] = False if case.get("reference") else None
            return result
    if case.get("command"):
        solution = _run_task_command(
            package_dir,
            workspace,
            config,
            case["command"],
            f"solution:{case['name']}",
            include_reference=bool(case.get("reference")),
        )
        result["steps"].append(solution)
        result["solution_ran"] = solution["returncode"] == 0
        if solution["returncode"] != 0:
            result["error"] = "solution command failed"
            return result
    before_grade = _hash_tree(workspace)
    grade = _grade_case(
        package_dir,
        workspace,
        config,
        case["name"],
        expected_pass=case["expected_pass"],
        expected_failed_checks=case.get("expected_failed_checks"),
    )
    result["steps"].append(grade)
    if case.get("reference"):
        result["grader_passed_reference"] = grade["passed"]
    after_grade = _hash_tree(workspace)
    result["grader_mutated_workspace"] = before_grade != after_grade
    result["passed"] = grade["passed"] and not result["grader_mutated_workspace"]
    result["workspace_sha256"] = after_grade
    if result["grader_mutated_workspace"]:
        result["error"] = "grader changed the solver workspace"
    return result


def _full_step_output(step: dict[str, Any]) -> dict[str, str]:
    outputs: dict[str, str] = {}
    for stream in ("stdout", "stderr"):
        log_path = step.get(f"{stream}_log")
        if isinstance(log_path, str) and Path(log_path).is_file():
            outputs[stream] = Path(log_path).read_text(encoding="utf-8", errors="replace")
        else:
            outputs[stream] = str(step.get(stream, ""))
    return outputs


def _request_image_repair(
    client: Any,
    package_dir: Path,
    config: dict[str, Any],
    image_build: dict[str, Any],
    reference_result: dict[str, Any],
    already_added: list[str],
    attempt_number: int,
    artifact_dir: Path,
) -> dict[str, Any]:
    """Ask Claude for a constrained environment-only repair based on full failure evidence."""
    source_requirements = package_dir / "authoring" / "image" / config["requirements_file"]
    previous_lock = ""
    lock_path = Path(str(image_build.get("dockerfile_path", ""))).parent / "requirements.lock"
    if lock_path.is_file():
        previous_lock = lock_path.read_text(encoding="utf-8")
    steps = []
    for step in reference_result.get("steps", []):
        steps.append({
            "label": step.get("label"),
            "command": step.get("command"),
            "returncode": step.get("returncode"),
            "timed_out": step.get("timed_out"),
            "stdout": _full_step_output(step)["stdout"],
            "stderr": _full_step_output(step)["stderr"],
        })
    evidence = {
        "attempt": attempt_number,
        "task_id": json.loads((package_dir / "manifest.json").read_text(encoding="utf-8")).get("task_id"),
        "reference_command": config["reference_solution_command"],
        "workspace_build_command": config["build_command"],
        "base_image_reference": image_build.get("base_image_reference"),
        "base_image_id": image_build.get("base_image_id"),
        "runtime_image": image_build.get("runtime_image"),
        "previous_dockerfile": image_build.get("dockerfile"),
        "previous_image_build_command": image_build.get("offline_build_command"),
        "previous_image_build_result": image_build.get("offline_build_result"),
        "declared_requirements": source_requirements.read_text(encoding="utf-8"),
        "previous_resolved_lockfile": previous_lock,
        "previously_added_requirements": already_added,
        "reference_result": reference_result,
        "complete_reference_step_outputs": steps,
    }
    system = (
        "You repair only the Docker environment for a generated Python task. The reference command failed to run; "
        "your job is to propose an environment that lets the unchanged reference and task commands execute. "
        "Do not propose changing task, workspace, reference, or grader files. Use only official python:<version> "
        "image tags and exact PyPI package pins. Return exactly one JSON object with keys decision, base_image, "
        "add_requirements, rationale. decision is 'repair' or 'triage'. Choose triage when the output indicates a "
        "reference/task defect or the cause is uncertain; such a candidate must continue to the remaining checks for "
        "triage, not be excluded as an environment failure. Choose repair only when the evidence points to an "
        "environment problem that your proposed image changes can address. base_image is an official tagged Python image "
        "or null to retain the current base. add_requirements is a list of at most 20 exact name==version pins; "
        "never include URLs, shell commands, build instructions, ranges, or unpinned packages. If the evidence does "
        "not support a safe environment-only fix, choose triage."
    )
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifact_dir / f"image-repair-attempt-{attempt_number}.json"
    artifact = {"model": MODEL_ID, "system_prompt": system, "user_evidence": evidence}
    _write_json(artifact_path, artifact)
    response = client.messages.create(
        model=MODEL_ID,
        max_tokens=4096,
        system=system,
        messages=[{"role": "user", "content": json.dumps(evidence, ensure_ascii=False)}],
    )
    text_parts = [block.text for block in response.content if getattr(block, "type", None) == "text"]
    raw = "\n".join(text_parts)
    artifact["raw_response"] = raw
    _write_json(artifact_path, artifact)
    try:
        repair = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StageJVerificationError("image repair response was not valid JSON", {"repair_response": raw}) from exc
    if not isinstance(repair, dict) or set(repair) != {"decision", "base_image", "add_requirements", "rationale"}:
        raise StageJVerificationError("image repair response did not match the required schema", {"repair_response": raw})
    if repair["decision"] not in {"repair", "triage"} or not isinstance(repair["rationale"], str):
        raise StageJVerificationError("image repair response contains invalid decision or rationale", {"repair_response": raw})
    base = repair["base_image"]
    if base is not None and (not isinstance(base, str) or not re.fullmatch(r"python:\d+\.\d+(?:\.\d+)?(?:-[a-zA-Z0-9.-]+)?", base)):
        raise StageJVerificationError("image repair proposed a base image outside the official Python tag format", {"repair_response": raw})
    additions = repair["add_requirements"]
    pin_pattern = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*==[A-Za-z0-9][A-Za-z0-9.+!_-]*$")
    if not isinstance(additions, list) or len(additions) > 20 or any(not isinstance(x, str) or not pin_pattern.fullmatch(x) for x in additions):
        raise StageJVerificationError("image repair proposed invalid or unpinned dependencies", {"repair_response": raw})
    if repair["decision"] == "repair" and base is None and not additions:
        raise StageJVerificationError("image repair selected repair without changing the image environment", {"repair_response": raw})
    repair["raw_response"] = raw
    usage = getattr(response, "usage", None)
    repair["usage"] = {
        "input_tokens": getattr(usage, "input_tokens", None),
        "output_tokens": getattr(usage, "output_tokens", None),
    }
    artifact["validated_repair_plan"] = {key: value for key, value in repair.items() if key not in {"raw_response", "usage"}}
    artifact["usage"] = repair["usage"]
    _write_json(artifact_path, artifact)
    repair["artifact"] = str(artifact_path.resolve())
    repair["artifact_sha256"] = _sha256_file(artifact_path)
    return repair


def _hash_tree(directory: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in directory.rglob("*") if item.is_file()):
        relative = path.relative_to(directory).as_posix().encode("utf-8")
        digest.update(relative + b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _private_file_overlap(package_dir: Path) -> list[dict[str, str]]:
    visible_hashes: dict[str, str] = {}
    for path in (package_dir / "workspace").rglob("*"):
        if path.is_file():
            visible_hashes[hashlib.sha256(path.read_bytes()).hexdigest()] = path.relative_to(package_dir / "workspace").as_posix()
    overlaps = []
    for role in ("grader", "reference"):
        for path in (package_dir / role).rglob("*"):
            if path.is_file():
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                if digest in visible_hashes:
                    overlaps.append({
                        "solver_visible_path": visible_hashes[digest],
                        "private_role": role,
                        "private_path": path.relative_to(package_dir / role).as_posix(),
                        "sha256": digest,
                    })
    return overlaps


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _wheel_metadata(wheel_path: Path) -> tuple[str, str]:
    with zipfile.ZipFile(wheel_path) as archive:
        metadata_files = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if len(metadata_files) != 1:
            raise ValueError(f"wheel must contain exactly one dist-info/METADATA file: {wheel_path.name}")
        metadata = archive.read(metadata_files[0]).decode("utf-8", errors="strict")
    name = next((line.partition(":")[2].strip() for line in metadata.splitlines() if line.startswith("Name:")), None)
    version = next((line.partition(":")[2].strip() for line in metadata.splitlines() if line.startswith("Version:")), None)
    if not name or not version:
        raise ValueError(f"wheel metadata is missing Name or Version: {wheel_path.name}")
    return name, version


def _acquire_wheels(
    package_dir: Path,
    config: dict[str, Any],
    output_dir: Path,
    inferred_requirements: list[str] | None = None,
) -> dict[str, Any]:
    """Download only binary wheels for exact requirements; never install or execute them here."""
    requirements_source = package_dir / "authoring" / "image" / config["requirements_file"]
    requirements = [
        line.strip() for line in requirements_source.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    inferred_requirements = inferred_requirements or []
    wheelhouse = output_dir / "dependencies" / "wheelhouse"
    wheelhouse.mkdir(parents=True, exist_ok=True)
    acquisition: dict[str, Any] = {
        "source": "https://pypi.org/simple",
        "requirements": requirements,
        "inferred_requirements": inferred_requirements,
        "binary_wheels_only": True,
        "downloads": [],
    }
    if requirements or inferred_requirements:
        # The base image was inspected before this call and is already local; Docker never pulls it.
        argv = [
            "docker", "run", "--pull=never", "--rm", "--network=bridge", "--read-only", "--cap-drop=ALL",
            "--security-opt=no-new-privileges", f"--pids-limit={config['pids_limit']}",
            f"--memory={config['memory_mb']}m", f"--cpus={config['cpus']}",
            "--tmpfs", f"/tmp:rw,nosuid,size={config['memory_mb']}m",
            "--mount", f"type=bind,src={requirements_source.resolve()},dst=/input/requirements.in,readonly",
            "--mount", f"type=bind,src={wheelhouse.resolve()},dst=/wheelhouse",
            "--entrypoint=python", config["base_image"], "-m", "pip", "download",
            "--disable-pip-version-check", "--no-input", "--only-binary=:all:",
            "--index-url", "https://pypi.org/simple", "--dest", "/wheelhouse",
            "--requirement", "/input/requirements.in", *inferred_requirements,
        ]
        download = _run_process(argv, config["timeout_seconds"])
        acquisition["download_command"] = argv
        acquisition["download_result"] = download
        if download["returncode"] != 0:
            _write_json(output_dir / "dependencies" / "acquisition.json", acquisition)
            raise StageJVerificationError(
                "dependency download failed; check exact version pins, Python/platform compatibility, and available wheels",
                {"dependency_acquisition": acquisition},
            )
    wheels = sorted(wheelhouse.glob("*.whl"))
    package_versions: dict[str, tuple[str, str]] = {}
    for wheel in wheels:
        name, version = _wheel_metadata(wheel)
        normalized = re.sub(r"[-_.]+", "-", name).casefold()
        if normalized in package_versions and package_versions[normalized][0] != version:
            raise ValueError(f"dependency acquisition produced conflicting versions of {name}")
        package_versions[normalized] = (version, _sha256_file(wheel))
        acquisition["downloads"].append({
            "filename": wheel.name,
            "name": name,
            "version": version,
            "sha256": _sha256_file(wheel),
            "size_bytes": wheel.stat().st_size,
        })
    if requirements and not wheels:
        raise StageJVerificationError("dependency download reported success but produced no wheels")
    lock_lines = [
        f"{name}=={version} --hash=sha256:{digest}"
        for name, (version, digest) in sorted(package_versions.items())
    ]
    lock_path = output_dir / "dependencies" / "requirements.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("\n".join(lock_lines) + ("\n" if lock_lines else ""), encoding="utf-8")
    acquisition["locked_packages"] = [
        {"name": name, "version": version, "sha256": digest}
        for name, (version, digest) in sorted(package_versions.items())
    ]
    acquisition["lockfile"] = str(lock_path.resolve())
    acquisition["lockfile_sha256"] = _sha256_file(lock_path)
    _write_json(output_dir / "dependencies" / "acquisition.json", acquisition)
    return acquisition


def _build_runtime_image(
    package_dir: Path,
    output_dir: Path,
    config: dict[str, Any],
    task_id: str,
    package_hash: str,
    repair_attempt: int,
    inferred_requirements: list[str],
) -> dict[str, Any]:
    base_inspect = _run_process(["docker", "image", "inspect", "--format={{.Id}}", config["base_image"]], 30)
    base_pull: dict[str, Any] | None = None
    if base_inspect["returncode"] != 0:
        base_pull = _run_process(["docker", "pull", config["base_image"]], config["build_timeout_seconds"])
        if base_pull["returncode"] != 0:
            raise StageJVerificationError(
                f"could not download base image {config['base_image']!r}",
                {"base_image_check": base_inspect, "base_image_pull": base_pull},
            )
        base_inspect = _run_process(["docker", "image", "inspect", "--format={{.Id}}", config["base_image"]], 30)
        if base_inspect["returncode"] != 0:
            raise StageJVerificationError(
                "base image pull succeeded but the image could not be inspected",
                {"base_image_check": base_inspect, "base_image_pull": base_pull},
            )
    base_image_id = base_inspect["stdout"].strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", base_image_id):
        raise StageJVerificationError("Docker returned an invalid base image ID")
    base_alias = f"taskforge/base:{base_image_id[7:19]}"
    tag_result = _run_process(["docker", "image", "tag", base_image_id, base_alias], 30)
    if tag_result["returncode"] != 0:
        raise StageJVerificationError("could not pin the local base image by immutable image ID")

    try:
        acquisition = _acquire_wheels(
            package_dir,
            {**config, "base_image": base_alias},
            output_dir,
            inferred_requirements,
        )
    except StageJVerificationError as exc:
        exc.details.setdefault("base_image_id", base_image_id)
        exc.details.setdefault("base_image_pull", base_pull)
        raise
    context = output_dir / "dependencies"
    wheelhouse = context / "wheelhouse"
    wheelhouse.mkdir(parents=True, exist_ok=True)
    if not any(wheelhouse.glob("*.whl")):
        (wheelhouse / ".keep").write_text("", encoding="utf-8")
    lock_path = context / "requirements.lock"
    dockerfile = (
        f"FROM {base_alias}\n"
        "ENTRYPOINT []\n"
        "COPY wheelhouse/ /opt/taskforge-wheelhouse/\n"
        "COPY requirements.lock /opt/taskforge-requirements.lock\n"
        "RUN if [ -s /opt/taskforge-requirements.lock ]; then "
        "python -m pip install --disable-pip-version-check --no-input --no-index "
        "--find-links=/opt/taskforge-wheelhouse --require-hashes "
        "-r /opt/taskforge-requirements.lock; fi && "
        "rm -rf /opt/taskforge-wheelhouse /opt/taskforge-requirements.lock\n"
    )
    (context / "Dockerfile").write_text(dockerfile, encoding="utf-8")
    runtime_tag = f"taskforge/{task_id}-stage-j:{package_hash[:8]}-r{repair_attempt}"
    build_command = [
        "docker", "build", "--no-cache", "--pull=false", "--network=none",
        "--tag", runtime_tag, str(context.resolve()),
    ]
    build_result = _run_process(build_command, config["build_timeout_seconds"])
    result: dict[str, Any] = {
        "base_image_reference": config["base_image"],
        "base_image_pull": base_pull,
        "base_image_id": base_image_id,
        "base_image_alias": base_alias,
        "dependency_acquisition": acquisition,
        "dockerfile_path": str((context / "Dockerfile").resolve()),
        "dockerfile": dockerfile,
        "repair_attempt": repair_attempt,
        "inferred_requirements": inferred_requirements,
        "offline_build_command": build_command,
        "offline_build_result": build_result,
        "build_context_sha256": _hash_tree(context),
    }
    if build_result["returncode"] != 0:
        raise StageJVerificationError("offline task runtime image build failed", result)
    inspect = _run_process(["docker", "image", "inspect", runtime_tag], 30)
    result["runtime_inspect"] = inspect
    if inspect["returncode"] != 0:
        raise StageJVerificationError("built task runtime image could not be inspected", {**result, "runtime_inspect": inspect})
    image_data = json.loads(inspect["stdout"])
    image_id = image_data[0]["Id"]
    result["runtime_image"] = {
        "tag": runtime_tag,
        "image_id": image_id,
        "size_bytes": image_data[0].get("Size"),
    }
    config["docker_image"] = runtime_tag
    return result


def _export_runtime_image(runtime_tag: str, output_dir: Path, timeout: int) -> dict[str, Any]:
    archive_path = output_dir / "runtime-image.tar"
    command = ["docker", "image", "save", "--output", str(archive_path.resolve()), runtime_tag]
    result = _run_process(command, timeout)
    export = {"command": command, **result}
    if result["returncode"] != 0:
        raise StageJVerificationError("pinned runtime image export failed", {"image_export": export})
    export.update({
        "archive": str(archive_path.resolve()),
        "archive_size_bytes": archive_path.stat().st_size,
        "archive_sha256": _sha256_file(archive_path),
    })
    return export


def _copy_workspace(source: Path, target: Path) -> None:
    shutil.copytree(source, target, symlinks=False)
    for path in target.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"workspace contains a symlink: {path.relative_to(target)}")


def _apply_case_files(workspace: Path, files: dict[str, str]) -> None:
    for relative, content in files.items():
        target = workspace.joinpath(*_safe_relative_path(relative).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _check_package_layout(package_dir: Path) -> None:
    for role_dir in ROLE_DIRS.values():
        path = package_dir / role_dir
        if not path.is_dir():
            raise ValueError(f"Stage I package is missing required role directory: {role_dir}")
    for path in package_dir.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"package contains a symlink: {path.relative_to(package_dir)}")


def run_stage_j(stage_i_output: Path, output_dir: Path | None = None,
                progress: Callable[[dict[str, Any]], None] | None = None) -> Path:
    """Verify one Stage I package; preserve it and write auditable Stage J artifacts."""
    package_dir = stage_i_output / "task-package"
    output_dir = output_dir or (stage_i_output / "stage-j")
    if output_dir.exists():
        raise FileExistsError(f"Stage J output already exists: {output_dir}")
    report: dict[str, Any] = {
        "stage": "I",
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "stage_i_output": str(stage_i_output.resolve()),
        "package": str(package_dir.resolve()),
        "status": "failed",
        "checks": [],
    }
    output_dir.mkdir(parents=True)
    try:
        if progress:
            progress({"stage": "J", "state": "working", "message": "validating task package and Stage J manifest"})
        status_path = stage_i_output / "status.json"
        status = json.loads(status_path.read_text(encoding="utf-8"))
        if status.get("status") != "implementation_generated_unverified":
            raise ValueError("Stage J requires a Stage I output with status implementation_generated_unverified")
        _check_package_layout(package_dir)
        config = _load_manifest(package_dir)
        report["manifest_sha256"] = hashlib.sha256(
            (package_dir / "authoring" / "stage-j.json").read_bytes()
        ).hexdigest()
        report["package_tree_sha256"] = _hash_tree(package_dir)
        report["possible_private_file_overlaps"] = _private_file_overlap(package_dir)
        task_manifest = json.loads((package_dir / "manifest.json").read_text(encoding="utf-8"))
        task_id = task_manifest.get("task_id") if isinstance(task_manifest, dict) else None
        if not isinstance(task_id, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", task_id):
            raise ValueError("task-package/manifest.json must contain a lowercase kebab-case task_id")
        report["sandbox_policy"] = {
            "base_image": config["base_image"],
            "base_image_network": "Docker registry only if the base image is missing locally",
            "dependency_download_network": "PyPI from a separate binary-wheel acquisition container",
            "task_execution_network": "disabled",
            "memory_mb": config["memory_mb"],
            "cpus": config["cpus"],
            "pids_limit": config["pids_limit"],
            "timeout_seconds": config["timeout_seconds"],
            "build_timeout_seconds": config["build_timeout_seconds"],
        }
        report["security_review"] = {
            "status": "pending_human_review",
            "automated_guards": [
                "solution and reference commands run without the grader mount",
                "grader commands receive the workspace read-only",
                "all task commands run with networking disabled and configured resource limits",
            ],
            "limitation": "Black-box tests and mount separation cannot prove that grader source contains no leaked answers or rewards shortcuts; inspect grader and task data during triage.",
        }
        if shutil.which("docker") is None:
            raise StageJVerificationError("Docker is required; Stage J has no local execution fallback")
        original_workspace = package_dir / "workspace"
        reference_case = {
            "name": "reference",
            "command": config["reference_solution_command"],
            "expected_pass": True,
            "reference": True,
        }
        reference_attempts: list[dict[str, Any]] = []
        image_build_attempts: list[dict[str, Any]] = []
        inferred_requirements: list[str] = []
        reference_ran = False
        repair_client: Any | None = None
        with tempfile.TemporaryDirectory(prefix="taskforge-stage-j-") as temp:
            temp_root = Path(temp)
            for repair_attempt in range(3):
                if progress:
                    progress({"stage": "J", "state": "working", "message": f"building offline runtime image (image attempt {repair_attempt + 1}/3)"})
                attempt_output = output_dir / f"image-attempt-{repair_attempt}"
                attempt_output.mkdir(parents=True, exist_ok=True)
                try:
                    image_build = _build_runtime_image(
                        package_dir,
                        attempt_output,
                        config,
                        task_id,
                        report["package_tree_sha256"],
                        repair_attempt,
                        inferred_requirements,
                    )
                except StageJVerificationError as exc:
                    image_build_attempts.append({
                        "repair_attempt": repair_attempt,
                        "passed": False,
                        "error": str(exc),
                        **exc.details,
                    })
                    break
                except Exception as exc:
                    image_build_attempts.append({
                        "repair_attempt": repair_attempt,
                        "passed": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    })
                    break
                image_build_attempts.append({"repair_attempt": repair_attempt, "passed": True, **image_build})
                config["docker_image"] = image_build["runtime_image"]["tag"]
                if progress:
                    progress({"stage": "J", "state": "working", "message": "runtime image built; running the reference solution and grader"})
                config["reference_log_dir"] = str(attempt_output / "reference-logs")
                reference_result = _run_case(
                    package_dir,
                    original_workspace,
                    temp_root,
                    config,
                    {**reference_case, "name": f"reference-attempt-{repair_attempt}"},
                )
                config.pop("reference_log_dir", None)
                reference_attempts.append({
                    "repair_attempt": repair_attempt,
                    "image_id": image_build["runtime_image"]["image_id"],
                    "result": reference_result,
                })
                if reference_result.get("solution_ran"):
                    reference_ran = True
                    reference_case = reference_result
                    image_build_attempts[-1]["reference_ran"] = True
                    image_build_attempts[-1]["reference_grader_passed"] = reference_result.get("grader_passed_reference")
                    break
                image_build_attempts[-1]["reference_ran"] = False
                if repair_attempt >= 2:
                    image_build_attempts[-1]["repair_decision"] = "two image repair attempts exhausted; stop and send to triage"
                    break
                try:
                    if repair_client is None:
                        if not os.environ.get("ANTHROPIC_API_KEY"):
                            image_build_attempts[-1]["repair_decision"] = "ANTHROPIC_API_KEY is unavailable; cannot request the allowed image repair"
                            break
                        try:
                            from anthropic import Anthropic
                        except ImportError as exc:
                            image_build_attempts[-1]["repair_decision"] = f"Anthropic SDK is unavailable: {exc}"
                            break
                        repair_client = Anthropic()
                    repair_plan = _request_image_repair(
                        repair_client,
                        package_dir,
                        config,
                        image_build,
                        reference_result,
                        inferred_requirements,
                        repair_attempt + 1,
                        attempt_output,
                    )
                except StageJVerificationError as exc:
                    image_build_attempts[-1]["repair_decision"] = str(exc)
                    image_build_attempts[-1]["repair_error_details"] = exc.details
                    break
                except Exception as exc:
                    image_build_attempts[-1]["repair_decision"] = f"image repair request failed: {type(exc).__name__}: {exc}"
                    break
                image_build_attempts[-1]["repair_plan"] = repair_plan
                if repair_plan["decision"] == "triage":
                    image_build_attempts[-1]["repair_decision"] = "Claude identified a likely task/reference defect or uncertain cause; continue checks for triage"
                    image_build_attempts[-1]["reference_failure_classification"] = repair_plan["rationale"]
                    reference_case = reference_result
                    reference_ran = True
                    break
                existing_lines = [
                    line.strip() for line in (package_dir / "authoring" / "image" / config["requirements_file"])
                    .read_text(encoding="utf-8").splitlines()
                    if line.strip() and not line.lstrip().startswith("#")
                ] + inferred_requirements
                existing = {line.split("==", 1)[0].casefold(): line for line in existing_lines}
                conflict = next((pin for pin in repair_plan["add_requirements"]
                                 if pin.split("==", 1)[0].casefold() in existing
                                 and existing[pin.split("==", 1)[0].casefold()] != pin), None)
                if conflict:
                    image_build_attempts[-1]["repair_decision"] = f"repair proposed a conflicting pin for an existing dependency: {conflict}"
                    break
                new_pins = [pin for pin in repair_plan["add_requirements"] if pin.split("==", 1)[0].casefold() not in existing]
                if repair_plan["base_image"] is None and not new_pins:
                    image_build_attempts[-1]["repair_decision"] = "repair did not change the image environment"
                    break
                if repair_plan["base_image"] is not None:
                    config["base_image"] = repair_plan["base_image"]
                inferred_requirements.extend(new_pins)
                image_build_attempts[-1]["repair_decision"] = "rebuild image with the validated environment changes"
                image_build_attempts[-1]["applied_base_image"] = config["base_image"]
                image_build_attempts[-1]["applied_requirements"] = list(inferred_requirements)

            report["image_build_attempts"] = image_build_attempts
            report["reference_gate"] = {
                "verification_gate_open": reference_ran,
                "reference_command_succeeded": bool(reference_case.get("solution_ran")) if reference_case else False,
                "remaining_checks_continued": reference_ran,
                "grader_passed": reference_case.get("grader_passed_reference") if reference_case else None,
                "repair_attempts_allowed": 2,
                "image_repair_attempts_applied": sum(
                    1 for item in image_build_attempts if item.get("repair_decision") == "rebuild image with the validated environment changes"
                ),
                "attempts": reference_attempts,
                "automatically_added_dependencies": inferred_requirements,
                "final_base_image": config["base_image"],
            }
            if reference_ran:
                report["image_build"] = image_build_attempts[-1]
                try:
                    if progress:
                        progress({"stage": "J", "state": "working", "message": "exporting the verified runtime image for Stage K"})
                    report["image_export"] = _export_runtime_image(
                        config["docker_image"], output_dir, config["build_timeout_seconds"]
                    )
                except StageJVerificationError as exc:
                    report["image_export"] = exc.details
                    raise
                final_image = image_build_attempts[-1]["runtime_image"]
                final_image["archive"] = report["image_export"]["archive"]
                final_image["archive_size_bytes"] = report["image_export"]["archive_size_bytes"]
                final_image["archive_sha256"] = report["image_export"]["archive_sha256"]
                run_results: list[dict[str, Any]] = [reference_case]
                cases: list[dict[str, Any]] = [
                    {"name": "untouched", "command": None, "expected_pass": False},
                    *({
                        "name": x["name"], "command": x["command"], "expected_pass": True,
                        "workspace_files": x["workspace_files"],
                    } for x in config["alternate_solutions"]),
                    *({
                        "name": x["name"], "command": None, "expected_pass": False,
                        "workspace_files": x["workspace_files"],
                        "expected_failed_checks": x["expected_failed_checks"],
                    } for x in config["negative_cases"]),
                ]
                for case in cases:
                    if progress:
                        progress({"stage": "J", "state": "working", "message": f"running artifact check: {case['name']}"})
                    run_results.append(_run_case(package_dir, original_workspace, temp_root, config, case))
                # Replay only after the reference gate has succeeded.
                if progress:
                    progress({"stage": "J", "state": "working", "message": "replaying the reference solution for determinism"})
                replay_case = _run_case(
                    package_dir,
                    original_workspace,
                    temp_root,
                    config,
                    {
                        "name": "reference-replay",
                        "command": config["reference_solution_command"],
                        "expected_pass": True,
                        "reference": True,
                    },
                )
                reference_grade = next(
                    (step.get("grader_result") for step in reference_case.get("steps", []) if step.get("label", "").startswith("grade:")),
                    None,
                )
                replay_grade = next(
                    (step.get("grader_result") for step in replay_case.get("steps", []) if step.get("label", "").startswith("grade:")),
                    None,
                )
                workspace_replay_matches = reference_case.get("workspace_sha256") == replay_case.get("workspace_sha256")
                grader_replay_matches = reference_grade == replay_grade
                report["replay"] = {
                    "result": replay_case,
                    "workspace_matches_first_reference": workspace_replay_matches,
                    "grader_result_matches_first_reference": grader_replay_matches,
                    "passed": bool(replay_case.get("solution_ran") and workspace_replay_matches and grader_replay_matches),
                }
                report["checks"] = run_results
            else:
                if image_build_attempts:
                    report["image_build"] = image_build_attempts[-1]
                report["image_build_attempts"] = image_build_attempts
                report["checks"] = []
                report["replay"] = {
                    "passed": False,
                    "skipped": True,
                    "reason": "the reference command could not be verified in a runnable image; see recorded image and repair attempts",
                }
        all_cases_pass = reference_ran and all(item["passed"] for item in report["checks"])
        exhausted_reference_repairs = (
            not reference_ran
            and len(reference_attempts) == 3
            and report["reference_gate"]["image_repair_attempts_applied"] == 2
        )
        report["reference_gate"]["excluded_after_two_repairs"] = exhausted_reference_repairs
        report["automated_status"] = "passed" if all_cases_pass and report["replay"]["passed"] else "failed"
        report["status"] = "needs_human_review" if report["automated_status"] == "passed" else "failed"
        report["candidate_disposition"] = (
            "eligible_for_human_review" if report["automated_status"] == "passed"
            else "exclude_candidate_reference_did_not_run" if exhausted_reference_repairs
            else "triage_required"
        )
        report["domain_review"] = {
            "status": "pending",
            "note": "Automated Stage J checks cannot establish domain correctness or identifiability where expert judgment is required.",
        }
        report["summary"] = (
            "Automated verification passed; domain and identifiability review remains pending."
            if report["automated_status"] == "passed"
            else "One or more automated verification checks failed; triage should review the recorded artifacts."
        )
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["summary"] = "Stage J could not complete verification; triage should review the recorded artifacts."
    report["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
    _write_json(output_dir / "report.json", report)
    _write_json(output_dir / "status.json", {
        "status": report["status"],
        "automated_status": report.get("automated_status", "not_completed"),
        "report": str((output_dir / "report.json").resolve()),
        "next_step": "triage",
        "completed_at_utc": report["completed_at_utc"],
    })
    (output_dir / "report.md").write_text(_render_report(report), encoding="utf-8")
    if progress:
        progress({"stage": "J", "state": "completed" if report["status"] == "needs_human_review" else "failed",
                  "message": report.get("summary", "Stage J finished"), "artifact": str(output_dir / "report.json")})
    return output_dir / "report.json"


def _render_report(report: dict[str, Any]) -> str:
    lines = [
        "# Stage J verification report",
        "",
        f"- Status: `{report.get('status')}`",
        f"- Automated status: `{report.get('automated_status', 'not completed')}`",
        f"- Package: `{report.get('package')}`",
        f"- Summary: {report.get('summary', report.get('error', 'No summary'))}",
        "",
        "## Check results",
        "",
    ]
    image_build = report.get("image_build")
    if image_build:
        lines.extend(["## Environment image build", ""])
        if image_build.get("base_image_reference"):
            lines.append(f"- Base image: `{image_build['base_image_reference']}` ({image_build.get('base_image_id', 'ID unavailable')})")
        acquisition = image_build.get("dependency_acquisition", {})
        if acquisition:
            lines.append(f"- Direct dependency pins: `{json.dumps(acquisition.get('requirements', []), ensure_ascii=False)}`")
            lines.append(f"- Downloaded binary wheels: {len(acquisition.get('downloads', []))}")
            if acquisition.get("lockfile_sha256"):
                lines.append(f"- Generated lockfile SHA-256: `{acquisition['lockfile_sha256']}`")
            download_result = acquisition.get("download_result")
            if download_result:
                lines.append(f"- Dependency download exit: `{download_result.get('returncode')}`; duration: {download_result.get('duration_seconds', 0)} seconds")
        if image_build.get("offline_build_command"):
            lines.append(f"- Offline image build command: `{json.dumps(image_build['offline_build_command'], ensure_ascii=False)}`")
            build_result = image_build.get("offline_build_result", {})
            lines.append(f"- Image build exit: `{build_result.get('returncode')}`; duration: {build_result.get('duration_seconds', 0)} seconds")
        runtime = image_build.get("runtime_image")
        if runtime:
            lines.append(f"- Runtime image ID: `{runtime.get('image_id')}`")
            lines.append(f"- Exported image archive: `{runtime.get('archive')}` ({runtime.get('archive_size_bytes')} bytes; SHA-256 `{runtime.get('archive_sha256')}`)")
        lines.append("")
        for key in ("dependency_acquisition", "offline_build_result", "export_result"):
            detail = acquisition.get("download_result") if key == "dependency_acquisition" else image_build.get(key)
            if detail and (detail.get("stdout") or detail.get("stderr")):
                lines.extend([f"### {key.replace('_', ' ').title()}", ""])
                if detail.get("stdout"):
                    lines.extend(["stdout:", "```text", detail["stdout"], "```"])
                if detail.get("stderr"):
                    lines.extend(["stderr:", "```text", detail["stderr"], "```"])
    for case in report.get("checks", []):
        lines.append(f"### {case['name']}: {'PASS' if case.get('passed') else 'FAIL'}")
        if case.get("error"):
            lines.append(f"\n{case['error']}")
        for step in case.get("steps", []):
            lines.extend([
                f"\n#### {step.get('label', 'step')}",
                f"\n- Command: `{json.dumps(step.get('command', []), ensure_ascii=False)}`",
                f"- Exit: `{step.get('returncode')}`; timed out: `{step.get('timed_out', False)}`",
                f"- Duration: {step.get('duration_seconds', 0)} seconds",
            ])
            if step.get("grader_protocol_error"):
                lines.append(f"- Grader protocol error: {step['grader_protocol_error']}")
            lines.extend(["\nstdout:\n", "```text", step.get("stdout", ""), "```", "\nstderr:\n", "```text", step.get("stderr", ""), "```"])
    if report.get("replay"):
        lines.extend(["", "## Reset and replay", "", json.dumps(report["replay"], indent=2, ensure_ascii=False)])
    if report.get("reference_gate"):
        lines.extend(["", "## Reference environment gate", "", json.dumps(report["reference_gate"], indent=2, ensure_ascii=False)])
    if report.get("error"):
        lines.extend(["", "## Error", "", report["error"]])
    return "\n".join(lines) + "\n"
