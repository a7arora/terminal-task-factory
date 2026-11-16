"""Provider configuration shared by the GLM-backed Claude Code agents."""
from __future__ import annotations

import os

GLM_ANTHROPIC_BASE_URL = "https://api.z.ai/api/anthropic"
GLM_CLAUDE_CODE_MODEL = "glm-5.3-flash"


def glm_upstream_config() -> tuple[str, str]:
    """Return Z.ai's Anthropic-compatible endpoint and API key for the proxy."""
    api_key = os.environ.get("GLM_API_KEY") or os.environ.get("ZAI_API_KEY")
    if not api_key:
        raise RuntimeError("set GLM_API_KEY or ZAI_API_KEY in .env or the environment")
    return os.environ.get("GLM_ANTHROPIC_BASE_URL", GLM_ANTHROPIC_BASE_URL), api_key


def glm_claude_code_env(*, proxy_url: str, max_output_tokens: int = 64_000) -> dict[str, str]:
    """Configure Claude Code to use the local metering proxy."""
    if not proxy_url:
        raise RuntimeError("GLM Claude Code requires the TaskForge accounting proxy")
    return {
        "ANTHROPIC_AUTH_TOKEN": "taskforge-local-accounting-proxy",
        "ANTHROPIC_BASE_URL": proxy_url,
        "ANTHROPIC_MODEL": os.environ.get("GLM_CLAUDE_CODE_MODEL", GLM_CLAUDE_CODE_MODEL),
        "ANTHROPIC_DEFAULT_OPUS_MODEL": os.environ.get("GLM_CLAUDE_CODE_MODEL", GLM_CLAUDE_CODE_MODEL),
        "ANTHROPIC_DEFAULT_SONNET_MODEL": os.environ.get("GLM_CLAUDE_CODE_MODEL", GLM_CLAUDE_CODE_MODEL),
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": os.environ.get("GLM_CLAUDE_CODE_MODEL", GLM_CLAUDE_CODE_MODEL),
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(max_output_tokens),
    }
