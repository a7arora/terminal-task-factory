"""Tool-calling GLM solver adapter for Stage K."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable

MODEL_ID = "glm-5.3-flash"
API_URL = "https://api.z.ai/api/paas/v4/chat/completions"
SOLVER_LIMIT_SECONDS = 8 * 60 * 60
COMMAND_LIMIT_SECONDS = 10 * 60
MAX_OUTPUT_CHARS = 60_000
MAX_TOOL_CALLS = 2000


def _run(command: list[str], timeout: int, *, input_text: str | None = None) -> dict[str, Any]:
    try:
        result = subprocess.run(
            command,
            input=input_text,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        return {
            "returncode": result.returncode,
            "stdout": result.stdout[-MAX_OUTPUT_CHARS:],
            "stderr": result.stderr[-MAX_OUTPUT_CHARS:],
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "returncode": None,
            "stdout": (exc.stdout or "")[-MAX_OUTPUT_CHARS:] if isinstance(exc.stdout, str) else "",
            "stderr": (exc.stderr or "")[-MAX_OUTPUT_CHARS:] if isinstance(exc.stderr, str) else "",
            "timed_out": True,
        }


def _tool_schemas() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "code_exec",
                "description": "Run one bounded shell command in the isolated task workspace. File changes persist; shell state does not.",
                "parameters": {
                    "type": "object",
                    "properties": {"cmd": {"type": "string", "description": "Shell command to run."}},
                    "required": ["cmd"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "finish",
                "description": "Submit completed deliverable files. Paths must be absolute paths under /workspace/deliverable.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "summary": {"type": "string"},
                        "files": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["summary", "files"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "abandon",
                "description": "Use only when required inputs are missing, a hard dependency is unavailable, or the task is incoherent.",
                "parameters": {
                    "type": "object",
                    "properties": {"reason": {"type": "string"}},
                    "required": ["reason"],
                    "additionalProperties": False,
                },
            },
        },
    ]


class DockerSolverSandbox:
    """Keep model-requested shell execution inside a network-isolated task image."""

    def __init__(self, image_tag: str, image_archive: Path | None, workspace: Path, report_dir: Path, limits: dict[str, Any]):
        self.image_tag = image_tag
        self.image_archive = image_archive
        self.workspace = workspace.resolve()
        self.report_dir = report_dir
        self.memory_mb = int(limits.get("memory_mb", 4096))
        self.cpus = limits.get("cpus", 2)
        self.pids_limit = int(limits.get("pids_limit", 256))
        self.container_name = f"taskforge-stage-k-{uuid.uuid4().hex[:12]}"

    def start(self) -> dict[str, Any]:
        if shutil.which("docker") is None:
            raise RuntimeError("Stage K GLM execution requires Docker; no host-shell fallback is provided")
        inspect = _run(["docker", "image", "inspect", self.image_tag], 30)
        load_result = None
        if inspect["returncode"] != 0:
            if self.image_archive is None:
                pulled = _run(["docker", "pull", self.image_tag], 1800)
                if pulled["returncode"] != 0:
                    raise RuntimeError(f"could not pull sandbox image {self.image_tag!r}: {pulled['stderr'][-2000:]}")
                inspect = _run(["docker", "image", "inspect", self.image_tag], 30)
                if inspect["returncode"] != 0:
                    raise RuntimeError(f"pulled sandbox image is not available locally: {self.image_tag!r}")
            elif not self.image_archive.is_file():
                raise FileNotFoundError(f"Stage J runtime image archive is missing: {self.image_archive}")
            else:
                load_result = _run(["docker", "image", "load", "--input", str(self.image_archive.resolve())], 1800)
                if load_result["returncode"] != 0:
                    raise RuntimeError(f"could not load the Stage J runtime image: {load_result['stderr'][-2000:]}")
                inspect = _run(["docker", "image", "inspect", self.image_tag], 30)
                if inspect["returncode"] != 0:
                    raise RuntimeError(f"loaded Stage J image does not contain expected tag {self.image_tag!r}")

        for path in [self.workspace, *self.workspace.rglob("*")]:
            if path.is_symlink():
                raise ValueError(f"sandbox workspace must not contain symlinks: {path}")
            if path.is_dir():
                path.chmod(0o777)
            elif path.is_file():
                path.chmod(path.stat().st_mode | 0o666)

        command = [
            "docker", "run", "--detach", "--rm", "--name", self.container_name,
            "--network", "none", "--read-only", "--memory", f"{self.memory_mb}m",
            "--cpus", str(self.cpus), "--pids-limit", str(self.pids_limit),
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--env", "HOME=/tmp", "--env", "TMPDIR=/tmp", "--env", "PYTHONDONTWRITEBYTECODE=1",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=512m",
            "--mount", f"type=bind,source={self.workspace},target=/workspace",
            "--workdir", "/workspace", "--user", "1000:1000",
            "--entrypoint", "/bin/sh", self.image_tag,
            "-c", "while :; do sleep 3600; done",
        ]
        started = _run(command, 60)
        if started["returncode"] != 0:
            raise RuntimeError(f"could not start isolated solver container: {started['stderr'][-4000:]}")
        return {"command": command, "start": started, "image_tag": self.image_tag, "image_load": load_result}

    def execute(self, command_text: str, timeout: int) -> dict[str, Any]:
        if not isinstance(command_text, str) or not command_text.strip():
            return {"returncode": 2, "stdout": "", "stderr": "cmd must be a non-empty string", "timed_out": False}
        limit = min(COMMAND_LIMIT_SECONDS, max(1, timeout))
        # Enforce the deadline inside the container and kill the whole process group;
        # timing out only the host-side `docker exec` client could leave task processes running.
        runner = (
            "import os,signal,subprocess,sys; "
            "p=subprocess.Popen(['/bin/sh','-lc',sys.argv[2]],start_new_session=True); "
            "\ntry: p.wait(timeout=int(sys.argv[1]))\n"
            "except subprocess.TimeoutExpired:\n"
            " os.killpg(p.pid,signal.SIGTERM)\n"
            " try: p.wait(timeout=3)\n"
            " except subprocess.TimeoutExpired: os.killpg(p.pid,signal.SIGKILL); p.wait()\n"
            " sys.stderr.write('command timed out and its process group was terminated\\n'); sys.exit(124)\n"
            "sys.exit(p.returncode)"
        )
        return _run(
            ["docker", "exec", "--workdir", "/workspace", self.container_name,
             "python", "-c", runner, str(limit), command_text],
            limit + 10,
        )

    def stop(self) -> dict[str, Any]:
        return _run(["docker", "stop", "--time", "3", self.container_name], 15)


class GLMToolAgent:
    def __init__(self, output_dir: Path, sandbox: DockerSolverSandbox, *, timeout_seconds: int = SOLVER_LIMIT_SECONDS,
                 max_cost_usd: float = 1.50,
                 max_api_turns: int = 32,
                 progress: Callable[[dict[str, Any]], None] | None = None):
        self.output_dir = output_dir
        self.sandbox = sandbox
        self.timeout_seconds = timeout_seconds
        self.max_cost_usd = max_cost_usd
        self.max_api_turns = max_api_turns
        self.api_turns = 0
        self.estimated_cost_usd = 0.0
        self.progress = progress
        self.api_key = os.environ.get("GLM_API_KEY") or os.environ.get("ZAI_API_KEY")
        self.api_url = os.environ.get("GLM_API_URL", API_URL)
        self.model = os.environ.get("GLM_MODEL", MODEL_ID)
        if not self.api_key:
            raise RuntimeError("set GLM_API_KEY or ZAI_API_KEY before running Stage K")
        self.calls = 0
        self.transcript: list[dict[str, Any]] = []

    def _request(self, payload: dict[str, Any], remaining: float) -> dict[str, Any]:
        request = urllib.request.Request(
            self.api_url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        timeout = max(1, min(900, int(remaining)))
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[-4000:]
            raise RuntimeError(f"GLM API returned HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"GLM API request failed: {exc.reason}") from exc

    def _record_tool(self, name: str, arguments: dict[str, Any], result: dict[str, Any]) -> None:
        self.transcript.append({"type": "tool", "name": name, "arguments": arguments, "result": result})
        self.output_dir.mkdir(parents=True, exist_ok=True)
        with (self.output_dir / "tool-transcript.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(self.transcript[-1], ensure_ascii=False) + "\n")

    def run(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        started = time.monotonic()
        deadline = started + self.timeout_seconds
        history: list[dict[str, Any]] = [{"role": "user", "content": user_prompt}]
        tool_schemas = _tool_schemas()
        finish: dict[str, Any] | None = None
        abandon_reason: str | None = None
        error: str | None = None
        try:
            self.sandbox_result = self.sandbox.start()
            while self.calls < MAX_TOOL_CALLS and self.api_turns < self.max_api_turns:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    error = "GLM solver reached the 8-hour wall-clock limit"
                    break
                payload = {
                    "model": self.model,
                    "messages": [{"role": "system", "content": system_prompt}, *history],
                    "tools": tool_schemas,
                    "tool_choice": "auto",
                    "temperature": 1.0,
                    "top_p": 0.95,
                    "max_tokens": 16000,
                }
                input_bytes = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
                input_cost_ceiling = (input_bytes + 512) * 0.15 / 1_000_000
                remaining_output_budget = self.max_cost_usd - self.estimated_cost_usd - input_cost_ceiling
                allowed_output_tokens = min(16000, int(remaining_output_budget * 1_000_000 / 0.50))
                if allowed_output_tokens < 256:
                    error = f"GLM solver reached its ${self.max_cost_usd:.2f} estimated spend cap"
                    break
                payload["max_tokens"] = allowed_output_tokens
                if self.progress:
                    self.progress({"stage": "K/GLM", "state": "working", "message": f"waiting for GLM API response (tool calls so far: {self.calls})"})
                response = self._request(payload, remaining)
                self.api_turns += 1
                usage = response.get("usage") or {}
                input_tokens = usage.get("prompt_tokens")
                output_tokens = usage.get("completion_tokens")
                if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
                    error = "GLM response omitted token usage; stopped to preserve spend cap"
                    break
                request_cost = (input_tokens * 0.15 + output_tokens * 0.50) / 1_000_000
                self.estimated_cost_usd += request_cost
                choices = response.get("choices") or []
                if not choices:
                    error = "GLM API response contained no choices"
                    break
                message = choices[0].get("message") or {}
                tool_calls = message.get("tool_calls") or []
                assistant_message = {"role": "assistant", "content": message.get("content")}
                if tool_calls:
                    assistant_message["tool_calls"] = tool_calls
                history.append(assistant_message)
                self.transcript.append({
                    "type": "model_response",
                    "call_index": self.calls,
                    "content": message.get("content"),
                    "tool_calls": tool_calls,
                    "finish_reason": choices[0].get("finish_reason"),
                    "usage": usage,
                    "estimated_cost_usd": request_cost,
                    "estimated_cumulative_cost_usd": self.estimated_cost_usd,
                })
                for tool_call in tool_calls:
                    self.calls += 1
                    function = tool_call.get("function") or {}
                    name = function.get("name")
                    try:
                        arguments = json.loads(function.get("arguments") or "{}")
                        if not isinstance(arguments, dict):
                            raise ValueError("tool arguments must decode to an object")
                    except (ValueError, json.JSONDecodeError) as exc:
                        arguments = {}
                        result = {"error": f"invalid tool arguments: {exc}"}
                    else:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            result = {"error": "8-hour wall-clock limit reached before tool execution"}
                        elif name == "code_exec":
                            if self.progress:
                                self.progress({"stage": "K/GLM", "state": "working", "message": f"running GLM shell tool call {self.calls} (maximum 10 minutes)"})
                            result = self.sandbox.execute(str(arguments.get("cmd", "")), int(remaining))
                            if self.progress:
                                self.progress({"stage": "K/GLM", "state": "working", "message": f"shell call {self.calls} returned code {result.get('returncode')}"})
                        elif name == "finish":
                            summary = arguments.get("summary")
                            files = arguments.get("files")
                            if not isinstance(summary, str) or not summary.strip() or not isinstance(files, list) or not files:
                                result = {"error": "finish requires a non-empty summary and at least one file"}
                            else:
                                deliverable_root = (self.sandbox.workspace / "deliverable").resolve()
                                validated: list[str] = []
                                for file_value in files:
                                    if not isinstance(file_value, str) or not Path(file_value).is_absolute():
                                        result = {"error": "finish file paths must be absolute"}
                                        break
                                    file_path = Path(file_value).resolve()
                                    if not file_path.is_relative_to(deliverable_root) or not file_path.is_file():
                                        result = {"error": f"finish path must be an existing file under {deliverable_root}: {file_value}"}
                                        break
                                    validated.append(str(file_path))
                                else:
                                    finish = {"summary": summary, "files": validated}
                                    result = {"accepted": True, **finish}
                        elif name == "abandon":
                            reason = arguments.get("reason")
                            if not isinstance(reason, str) or not reason.strip():
                                result = {"error": "abandon requires a non-empty reason"}
                            else:
                                abandon_reason = reason
                                result = {"accepted": True, "status": "abandoned", "reason": reason}
                        else:
                            result = {"error": f"unknown tool: {name!r}"}
                    self._record_tool(str(name), arguments, result)
                    history.append({
                        "role": "tool",
                        "tool_call_id": tool_call.get("id", f"call-{self.calls}"),
                        "content": json.dumps(result, ensure_ascii=False),
                    })
                    if finish is not None or abandon_reason is not None:
                        break
                if finish is not None or abandon_reason is not None:
                    break
                if not tool_calls:
                    error = "GLM returned without using finish or abandon"
                    break
            else:
                if self.calls >= MAX_TOOL_CALLS:
                    error = f"GLM exceeded the {MAX_TOOL_CALLS} tool-call limit"
                else:
                    error = f"GLM exceeded the {self.max_api_turns} API-turn limit"
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        finally:
            sandbox_stop = self.sandbox.stop()
            self.output_dir.mkdir(parents=True, exist_ok=True)
            (self.output_dir / "tool-transcript.json").write_text(
                json.dumps(self.transcript, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
        completed = time.monotonic()
        return {
            "status": "solved" if finish else "abandoned" if abandon_reason else "failed",
            "model": self.model,
            "started_at_monotonic": started,
            "elapsed_seconds": completed - started,
            "time_limit_seconds": self.timeout_seconds,
            "tool_calls": self.calls,
            "api_turns": self.api_turns,
            "estimated_glm_cost_usd": self.estimated_cost_usd,
            "glm_cost_cap_usd": self.max_cost_usd,
            "finish": finish,
            "abandon_reason": abandon_reason,
            "error": error,
            "sandbox": getattr(self, "sandbox_result", None),
            "sandbox_stop": sandbox_stop,
            "transcript": str((self.output_dir / "tool-transcript.json").resolve()),
        }
