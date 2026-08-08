"""Configuration, read once from the environment / .env."""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


def _flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# --- Claude Managed Agents -------------------------------------------------
AGENT_ID = os.getenv("AGENT_ID", "").strip()
ENVIRONMENT_ID = os.getenv("ENVIRONMENT_ID", "").strip()
AGENT_MODEL = os.getenv("AGENT_MODEL", "claude-opus-5").strip()
AGENT_EFFORT = os.getenv("AGENT_EFFORT", "xhigh").strip()
AGENT_NAME = os.getenv("AGENT_NAME", "api-drift-fixer").strip()
ENVIRONMENT_NAME = os.getenv("ENVIRONMENT_NAME", "api-drift-sandbox").strip()
WORKSPACE_ID = os.getenv("ANTHROPIC_WORKSPACE_ID", "default").strip() or "default"

# --- Repository the agent edits -------------------------------------------
GITHUB_REPO_URL = os.getenv("GITHUB_REPO_URL", "").strip()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GITHUB_MOUNT_PATH = os.getenv("GITHUB_MOUNT_PATH", "/workspace/repo").strip()
GITHUB_BASE_BRANCH = os.getenv("GITHUB_BASE_BRANCH", "main").strip()

# --- Voice ----------------------------------------------------------------
MAYA_API_URL = os.getenv("MAYA_API_URL", "").strip()
MAYA_API_KEY = os.getenv("MAYA_API_KEY", "").strip()
MAYA_MODEL = os.getenv("MAYA_MODEL", "Maya 2 Native").strip()
MAYA_VOICE = os.getenv("MAYA_VOICE", "").strip()
MAYA_LANGUAGE = os.getenv("MAYA_LANGUAGE", "en").strip()

# --- Slack ----------------------------------------------------------------
# Bot token (xoxb-...) with chat:write and files:write. Not the MCP connection:
# MCP is only reachable from inside an agent session, and the detection alert
# has to go out before the agent starts.
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "").strip()
SLACK_CHANNEL_ID = os.getenv("SLACK_CHANNEL_ID", "").strip()
# How long to wait for CI to open the PR before posting the resolution message.
SLACK_PR_WAIT_SECONDS = int(os.getenv("SLACK_PR_WAIT_SECONDS", "45"))

# --- Behaviour ------------------------------------------------------------
# DEMO_MODE replays a scripted run instead of driving a real CMA session. It is
# the fallback for judging, and it is labelled as simulated in the UI so a
# simulated run can never be mistaken for a real one.
DEMO_MODE = _flag("DEMO_MODE", default=False)
SESSION_TIMEOUT_SECONDS = int(os.getenv("SESSION_TIMEOUT_SECONDS", "1800"))

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUDIO_DIR = os.path.join(REPO_ROOT, "interface", "static", "audio")


def console_url(session_id: str) -> str:
    return f"https://platform.claude.com/workspaces/{WORKSPACE_ID}/sessions/{session_id}"


def live_mode_blockers() -> list[str]:
    """Why a real run cannot start. Empty list means we are good to go."""
    missing = []
    if not (os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")):
        missing.append("ANTHROPIC_API_KEY")
    if not AGENT_ID:
        missing.append("AGENT_ID")
    if not ENVIRONMENT_ID:
        missing.append("ENVIRONMENT_ID")
    if not GITHUB_REPO_URL:
        missing.append("GITHUB_REPO_URL")
    if not GITHUB_TOKEN:
        missing.append("GITHUB_TOKEN")
    return missing
