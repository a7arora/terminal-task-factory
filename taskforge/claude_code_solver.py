"""Claude Code agent loop routed to GLM, with solver actions bridged to Docker."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Callable

from .provider_config import GLM_CLAUDE_CODE_MODEL, glm_claude_code_env, glm_upstream_config
from .glm_accounting_proxy import GLMAccountingProxy, GLMUsageMeter

class ClaudeCodeSolverAgent:
    def __init__(self, output_dir: Path, sandbox: Any, *, timeout_seconds: int,
                 max_budget_usd: float, max_turns: int = 32,
                 progress: Callable[[dict[str, Any]], None] | None = None):
        self.output_dir = output_dir
        self.sandbox = sandbox
        self.timeout_seconds = timeout_seconds
        self.max_budget_usd = max_budget_usd
        self.max_turns = max_turns
        self.progress = progress

    def run(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        return asyncio.run(self._run(system_prompt, user_prompt))

    async def _run(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        try:
            from claude_agent_sdk import (
                AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, ResultMessage,
                TextBlock, ToolUseBlock, create_sdk_mcp_server, tool,
            )
        except ImportError as exc:
            raise RuntimeError("install Claude Code support with: python -m pip install -e '.[stage-k]'") from exc

        started = time.monotonic()
        calls = 0
        finish: dict[str, Any] | None = None
        abandon_reason: str | None = None
        error: str | None = None
        transcript: list[dict[str, Any]] = []
        assistant_turn_usages: list[dict[str, Any]] = []
        self.output_dir.mkdir(parents=True, exist_ok=True)

        @tool("code_exec", "Run a bounded shell command inside the network-disabled Docker workspace.", {"cmd": str})
        async def code_exec(args: dict[str, Any]) -> dict[str, Any]:
            nonlocal calls
            calls += 1
            result = self.sandbox.execute(str(args.get("cmd", "")), self.timeout_seconds)
            transcript.append({"type": "tool", "name": "code_exec", "arguments": args, "result": result})
            if self.progress:
                self.progress({"stage": "K/GLM", "state": "working", "message": f"Claude Code shell tool {calls} returned code {result.get('returncode')}"})
            return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}

        @tool("finish", "Submit completed files under /workspace/deliverable.", {"summary": str, "files": list})
        async def finish_tool(args: dict[str, Any]) -> dict[str, Any]:
            nonlocal finish
            summary, files = args.get("summary"), args.get("files")
            root = (self.sandbox.workspace / "deliverable").resolve()
            if not isinstance(summary, str) or not summary.strip() or not isinstance(files, list) or not files:
                return {"content": [{"type": "text", "text": "finish needs a summary and at least one file"}], "isError": True}
            paths = []
            for value in files:
                path = Path(value).resolve() if isinstance(value, str) else Path("/")
                if not path.is_relative_to(root) or not path.is_file():
                    return {"content": [{"type": "text", "text": f"not an existing deliverable file: {value}"}], "isError": True}
                paths.append(str(path))
            finish = {"summary": summary, "files": paths}
            return {"content": [{"type": "text", "text": "Deliverables accepted."}]}

        @tool("abandon", "Stop only when a required input is missing or the task cannot be completed.", {"reason": str})
        async def abandon_tool(args: dict[str, Any]) -> dict[str, Any]:
            nonlocal abandon_reason
            abandon_reason = str(args.get("reason", ""))
            return {"content": [{"type": "text", "text": f"Stopped: {abandon_reason}"}]}

        server = create_sdk_mcp_server(name="taskforge_solver", version="1.0.0", tools=[code_exec, finish_tool, abandon_tool])
        result_data: dict[str, Any] = {}
        upstream, api_key = glm_upstream_config()
        meter = GLMUsageMeter(max_cost_usd=self.max_budget_usd)
        proxy = GLMAccountingProxy(upstream, api_key, meter)
        try:
            sandbox_info = self.sandbox.start()
            with proxy:
                options = ClaudeAgentOptions(
                    model=GLM_CLAUDE_CODE_MODEL,
                    cwd=str(self.sandbox.workspace),
                    system_prompt=system_prompt,
                    tools=[], mcp_servers={"taskforge_solver": server},
                    allowed_tools=["mcp__taskforge_solver__code_exec", "mcp__taskforge_solver__finish", "mcp__taskforge_solver__abandon"],
                    permission_mode="dontAsk", setting_sources=[], strict_mcp_config=True,
                    max_turns=self.max_turns, max_budget_usd=None,
                    env=glm_claude_code_env(proxy_url=proxy.base_url, max_output_tokens=32_000),
                )
                async def drive_agent() -> None:
                    nonlocal error, result_data
                    async with ClaudeSDKClient(options=options) as client:
                        await client.query(user_prompt)
                        async for message in client.receive_response():
                            if isinstance(message, AssistantMessage):
                                usage = getattr(message, "usage", None)
                                if isinstance(usage, dict):
                                    assistant_turn_usages.append(usage)
                                    transcript.append({"type": "model_usage", "usage": usage})
                                for block in message.content:
                                    if isinstance(block, ToolUseBlock):
                                        transcript.append({"type": "tool_request", "name": block.name, "input": block.input})
                                    elif isinstance(block, TextBlock):
                                        transcript.append({"type": "assistant_text", "text": block.text})
                            elif isinstance(message, ResultMessage):
                                result_data = {
                                    "session_id": getattr(message, "session_id", None),
                                    "num_turns": getattr(message, "num_turns", None),
                                    "sdk_total_cost_usd_untrusted": getattr(message, "total_cost_usd", None),
                                    "usage": getattr(message, "usage", None),
                                    "assistant_turn_usages": assistant_turn_usages,
                                    "model_usage": getattr(message, "model_usage", None),
                                    "is_error": getattr(message, "is_error", False),
                                    "errors": getattr(message, "errors", None),
                                    "glm_accounting": meter.status(),
                                }
                                if self.progress:
                                    budget_status = meter.status()
                                    spent = budget_status["estimated_spent_usd"]
                                    remaining = budget_status["estimated_remaining_usd"]
                                    self.progress({
                                        "stage": "K/GLM", "state": "working",
                                        "message": (f"GLM estimated spend ${spent:.4f}; remaining ${remaining:.4f}"
                                                    if spent is not None else "GLM provider usage unavailable"),
                                    })
                                if result_data["is_error"]:
                                    error = str(result_data["errors"] or "Claude Code agent failed")
                await asyncio.wait_for(drive_agent(), timeout=self.timeout_seconds)
        except TimeoutError:
            error = f"Claude Code solver exceeded {self.timeout_seconds}s"
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        finally:
            stopped = self.sandbox.stop()
            (self.output_dir / "tool-transcript.json").write_text(json.dumps(transcript, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

        accounting = meter.status()
        cost = accounting["estimated_spent_usd"]
        result = {
            "status": "solved" if finish and not error else "abandoned" if abandon_reason else "failed",
            "model": GLM_CLAUDE_CODE_MODEL, "harness": "claude-agent-sdk", "elapsed_seconds": time.monotonic() - started,
            "time_limit_seconds": self.timeout_seconds, "tool_calls": calls, "finish": finish,
            "abandon_reason": abandon_reason, "error": error, "estimated_glm_cost_usd": cost,
            "usage": result_data.get("usage"), "model_usage": result_data.get("model_usage"),
            "glm_accounting": accounting,
            "assistant_turn_usage_count": len(assistant_turn_usages),
            "usage_tracking_source": "zai_final_response_usage_via_local_proxy",
            "agent_result": result_data, "sandbox": locals().get("sandbox_info"), "sandbox_stop": stopped,
            "transcript": str((self.output_dir / "tool-transcript.json").resolve()),
        }
        (self.output_dir / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return result
