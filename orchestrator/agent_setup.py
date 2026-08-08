"""Provision and inspect the CMA agent and its sandbox environment.

    python -m orchestrator.agent_setup create        # first time only
    python -m orchestrator.agent_setup show          # inspect + preflight
    python -m orchestrator.agent_setup update        # new system-prompt version
    python -m orchestrator.agent_setup update --with-tools

`update` sends only the system prompt by default. Omitted fields are preserved
server-side, so an agent configured in the Console — with MCP servers this repo
knows nothing about — keeps them. `--with-tools` opts into replacing `tools`
and `mcp_servers` wholesale, which is destructive and will drop anything not
declared in this file.
"""

from __future__ import annotations

import argparse
import sys

import httpx

from orchestrator import config
from orchestrator.prompts import SYSTEM_PROMPT

# Every tool is always_allow: a permission gate that stalls mid-demo is a worse
# failure than the risk it mitigates here, and the agent is fenced by prompt,
# enforced by CI, and reviewed by a human before anything merges.
TOOLS = [
    {
        "type": "agent_toolset_20260401",
        "default_config": {
            "enabled": True,
            "permission_policy": {"type": "always_allow"},
        },
    }
]

MODEL = {"id": config.AGENT_MODEL, "effort": config.AGENT_EFFORT}


def _client():
    import anthropic

    return anthropic.Anthropic()


def create() -> int:
    client = _client()
    environment = client.beta.environments.create(
        name=config.ENVIRONMENT_NAME,
        description="Sandbox for the API drift auto-adapt agent.",
        config={"type": "cloud", "networking": {"type": "unrestricted"}},
    )
    agent = client.beta.agents.create(
        name=config.AGENT_NAME,
        model=MODEL,
        description=(
            "Maps a third-party API diff onto consumer code, fixes it, and "
            "verifies against a fenced contract test."
        ),
        system=SYSTEM_PROMPT,
        tools=TOOLS,
    )
    print("Add these to .env:\n")
    print(f"AGENT_ID={agent.id}")
    print(f"ENVIRONMENT_ID={environment.id}")
    print(f"\nagent version: {agent.version}")
    return 0


def update(with_tools: bool = False, drop_mcp: bool = False) -> int:
    if not config.AGENT_ID:
        print("AGENT_ID is not set; run `create` first.", file=sys.stderr)
        return 1

    client = _client()
    current = client.beta.agents.retrieve(config.AGENT_ID)
    remote_mcp = [getattr(m, "name", m) for m in (getattr(current, "mcp_servers", None) or [])]
    remote_tools = [getattr(t, "type", t) for t in (getattr(current, "tools", None) or [])]

    fields: dict = {"system": SYSTEM_PROMPT}

    if drop_mcp:
        # Clearing mcp_servers while tools still holds an mcp_toolset that
        # references one is rejected, so both must go in the same request.
        fields["tools"] = TOOLS
        fields["model"] = MODEL
        fields["mcp_servers"] = []
        print("Replacing the system prompt and removing MCP configuration.")
        print(f"  tools:        {remote_tools} -> {[t['type'] for t in TOOLS]}")
        print(f"  mcp_servers:  {remote_mcp or '(none)'} -> []")
        print(f"  model:        {MODEL}")
    elif with_tools:
        fields["tools"] = TOOLS
        fields["model"] = MODEL
        if remote_mcp:
            print(
                "REFUSING: --with-tools replaces `tools` wholesale, which would drop "
                f"the MCP toolsets this agent has configured ({', '.join(remote_mcp)}).\n"
                "Pass --drop-mcp if removing them is what you intend, or run without "
                "--with-tools to update only the system prompt.",
                file=sys.stderr,
            )
            return 1
    else:
        print("Updating the system prompt only.")
        print(f"  preserving tools:       {remote_tools}")
        print(f"  preserving mcp_servers: {remote_mcp or '(none)'}")

    if (getattr(current, "system", None) or "") == SYSTEM_PROMPT and not (with_tools or drop_mcp):
        print("Remote system prompt already matches this file; nothing to do.")
        return 0

    updated = client.beta.agents.update(config.AGENT_ID, version=current.version, **fields)
    print(f"\n{config.AGENT_ID}: version {current.version} -> {updated.version}")
    return 0


def show() -> int:
    ok = True
    print("=== local configuration ===")
    print(f"agent id:       {config.AGENT_ID or '(unset)'}")
    print(f"environment id: {config.ENVIRONMENT_ID or '(unset)'}")
    print(f"repo:           {config.GITHUB_REPO_URL or '(unset)'}")
    print(f"base branch:    {config.GITHUB_BASE_BRANCH}")
    print(f"github token:   {'set' if config.GITHUB_TOKEN else '(unset)'}")
    print(f"demo mode:      {config.DEMO_MODE}")
    blockers = config.live_mode_blockers()
    print(f"live blockers:  {', '.join(blockers) if blockers else 'none'}")
    if blockers:
        ok = False

    if not config.AGENT_ID:
        return 1

    print("\n=== agent, as stored by Anthropic ===")
    try:
        client = _client()
        agent = client.beta.agents.retrieve(config.AGENT_ID)
    except Exception as exc:
        print(f"could not fetch agent: {exc}", file=sys.stderr)
        return 1

    model = getattr(agent, "model", None)
    print(f"name:     {agent.name}")
    print(f"version:  {agent.version}")
    print(f"model:    {getattr(model, 'id', model)}  effort={getattr(getattr(model, 'effort', None), 'type', None)}")
    tools = [getattr(t, "type", t) for t in (getattr(agent, "tools", None) or [])]
    mcp = [getattr(m, "name", m) for m in (getattr(agent, "mcp_servers", None) or [])]
    print(f"tools:    {tools}")
    print(f"mcp:      {mcp or '(none)'}")

    system = getattr(agent, "system", None) or ""
    fence_checks = {
        "names test_contract.py": "test_contract.py" in system,
        "restricts edits to consumer/": "consumer/" in system,
        "forbids inventing a branch name": "branch" in system.lower(),
        "bounds correction attempts": "attempt" in system.lower(),
    }
    print(f"system:   {len(system)} chars")
    for label, passed in fence_checks.items():
        print(f"  [{'x' if passed else ' '}] {label}")
    if not all(fence_checks.values()):
        ok = False

    if system != SYSTEM_PROMPT:
        print(
            "  note: the stored prompt differs from orchestrator/prompts.py "
            "(expected if it was authored in the Console)"
        )

    print("\n=== environment ===")
    if config.ENVIRONMENT_ID:
        try:
            env = client.beta.environments.retrieve(config.ENVIRONMENT_ID)
            env_config = getattr(env, "config", None)
            print(f"name: {getattr(env, 'name', '?')}")
            print(f"type: {getattr(env_config, 'type', '?')}  "
                  f"networking={getattr(getattr(env_config, 'networking', None), 'type', '?')}")
        except Exception as exc:
            print(f"could not fetch environment: {exc}", file=sys.stderr)
            ok = False

    # github_repository is NOT part of the agent config, so no amount of
    # inspecting the agent can confirm repo access. It is a per-session resource
    # supplied at sessions.create() time, so the real preflight is: can this
    # token actually reach that repo, on that branch?
    print("\n=== repo access (session resource, not an agent tool) ===")
    if not (config.GITHUB_REPO_URL and config.GITHUB_TOKEN):
        print("skipped: GITHUB_REPO_URL and GITHUB_TOKEN must both be set")
        ok = False
    else:
        slug = config.GITHUB_REPO_URL.rstrip("/").removesuffix(".git").split("github.com/")[-1]
        headers = {
            "Authorization": f"Bearer {config.GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
        }
        try:
            repo = httpx.get(f"https://api.github.com/repos/{slug}", headers=headers, timeout=30)
            if repo.status_code == 404:
                print(f"[ ] repo {slug} — 404. It does not exist, or this token cannot see it.")
                print("    A fine-grained PAT scoped to selected repositories will 404 on a")
                print("    repo created after the token, until you add it to the token's access list.")
                ok = False
            elif repo.status_code >= 400:
                print(f"[ ] repo {slug} — HTTP {repo.status_code}: {repo.text[:160]}")
                ok = False
            else:
                data = repo.json()
                perms = data.get("permissions") or {}
                print(f"[x] repo {slug} reachable (private={data.get('private')})")
                print(f"    permissions: {perms}")
                if not perms.get("push"):
                    print("[ ] token lacks push — the agent's final git push WILL fail")
                    ok = False
                else:
                    print("[x] token can push (Contents: Read and write)")

                branch = httpx.get(
                    f"https://api.github.com/repos/{slug}/branches/{config.GITHUB_BASE_BRANCH}",
                    headers=headers,
                    timeout=30,
                )
                if branch.status_code == 200:
                    print(f"[x] base branch '{config.GITHUB_BASE_BRANCH}' exists")
                    tree = httpx.get(
                        f"https://api.github.com/repos/{slug}/contents/consumer/test_contract.py",
                        headers=headers,
                        params={"ref": config.GITHUB_BASE_BRANCH},
                        timeout=30,
                    )
                    if tree.status_code == 200:
                        print("[x] consumer/test_contract.py present on the base branch")
                    else:
                        print("[ ] consumer/test_contract.py NOT on the base branch — the agent")
                        print("    would clone a repo with nothing to fix and no test to run")
                        ok = False
                else:
                    print(f"[ ] base branch '{config.GITHUB_BASE_BRANCH}' not found "
                          f"(HTTP {branch.status_code}) — repo may be empty")
                    ok = False
        except Exception as exc:
            print(f"[ ] repo preflight failed: {type(exc).__name__}: {exc}")
            ok = False

    print(f"\n=== ready for a real run: {'YES' if ok else 'NO'} ===")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("create", "update", "show"))
    parser.add_argument(
        "--with-tools",
        action="store_true",
        help="also replace tools/model (destructive; drops undeclared MCP servers)",
    )
    parser.add_argument(
        "--drop-mcp",
        action="store_true",
        help="replace tools/model AND clear mcp_servers (explicit MCP removal)",
    )
    args = parser.parse_args(argv)
    if args.action == "update":
        return update(with_tools=args.with_tools, drop_mcp=args.drop_mcp)
    return {"create": create, "show": show}[args.action]()


if __name__ == "__main__":
    raise SystemExit(main())
