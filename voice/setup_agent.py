#!/usr/bin/env python3
"""Create (or update) the ElevenLabs voice analyst for the Deal Qualification Agent.

Usage (from the repo root, needs ELEVENLABS_API_KEY in .env or the environment):
    python3 voice/setup_agent.py --dry-run          # print what would be sent
    python3 voice/setup_agent.py                    # create 5 client tools + the agent, print AGENT_ID
    python3 voice/setup_agent.py --update AGENT_ID  # update prompt, first message, LLM and voice of an existing agent

Optional environment:
    AGENT_LLM    default gemini-2.5-flash (fast, reliable tool calls). "grok-beta" is also available.
    AGENT_VOICE  ElevenLabs voice id; empty = ElevenLabs default voice
    AGENT_HOSTS  comma-separated allowed hostnames, default demo3.ai-vance.de,localhost
No third-party packages needed.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = os.getenv("ELEVENLABS_API_BASE", "https://api.elevenlabs.io")


def load_env():
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                v = v.split(" #")[0].strip().strip('"').strip("'")
                os.environ.setdefault(k.strip(), v)


def call(method, path, body=None):
    key = os.getenv("ELEVENLABS_API_KEY")
    if not key:
        sys.exit("ELEVENLABS_API_KEY is missing (put it in .env).")
    req = urllib.request.Request(
        f"{API}{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"xi-api-key": key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        sys.exit(f"{method} {path} failed: {e.code}\n{e.read().decode(errors='ignore')[:2000]}")


NUM = lambda d: {"type": "number", "description": d}

TOOLS = [
    {
        "type": "client",
        "name": "get_deal_report",
        "description": "Returns the deal report currently shown on screen: assets, verdict, and every figure with value, unit, source, source_type, page and status. Call before answering any question about the deal.",
        "expects_response": True,
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "client",
        "name": "highlight_figure",
        "description": "Scrolls to one figure in the report, highlights it and shows its source chain. Call whenever you talk about a specific figure.",
        "expects_response": True,
        "parameters": {
            "type": "object",
            "properties": {"metric": {"type": "string", "description": "Exact metric id from get_deal_report, e.g. irr, noi, yield_on_cost, studio_rate."}},
            "required": ["metric"],
        },
    },
    {
        "type": "client",
        "name": "open_source",
        "description": "Opens the source of one figure (document and page, MSCI deal or web page) and returns it. Use when the user asks where a number comes from or whether it is verified.",
        "expects_response": True,
        "parameters": {
            "type": "object",
            "properties": {"metric": {"type": "string", "description": "Exact metric id from get_deal_report."}},
            "required": ["metric"],
        },
    },
    {
        "type": "client",
        "name": "run_what_if",
        "description": "Recalculates gross income, NOI, yield on cost and the verdict with changed assumptions and shows the scenario on screen. Pass only the inputs the user changed; changes stack on the current scenario until reset_what_if is called.",
        "expects_response": True,
        "parameters": {
            "type": "object",
            "properties": {
                "occupancy": NUM("Occupancy as a fraction, e.g. 0.9 for 90 percent."),
                "studio_rate": NUM("Studio weekly rate in GBP."),
                "ensuite_rate": NUM("En-suite weekly rate in GBP."),
                "cluster_rate": NUM("Cluster room weekly rate in GBP."),
                "weeks": NUM("Tenancy length in weeks, usually 44 or 51."),
                "opex_ratio": NUM("Operating costs as a fraction of gross income, e.g. 0.35."),
                "exit_yield": NUM("Exit yield as a fraction, e.g. 0.055."),
            },
            "required": [],
        },
    },
    {
        "type": "client",
        "name": "reset_what_if",
        "description": "Removes the current scenario and returns the report to the base case.",
        "expects_response": True,
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
]

FIRST_MESSAGE = (
    "Hi, I'm your deal analyst. Ask me why this deal is a GO, where any figure comes from, "
    "or what happens if an assumption changes."
)

CLIENT_EVENTS = [
    "conversation_initiation_metadata", "audio", "interruption", "user_transcript",
    "agent_response", "agent_response_correction", "client_tool_call", "vad_score", "ping",
]


def agent_config(tool_ids):
    prompt = (ROOT / "voice" / "system_prompt.md").read_text()
    conv = {
        "agent": {
            "first_message": FIRST_MESSAGE,
            "language": "en",
            "prompt": {
                "prompt": prompt,
                "llm": os.getenv("AGENT_LLM", "gemini-2.5-flash"),
                "temperature": 0.2,
                "tool_ids": tool_ids,
            },
        },
        "asr": {"keywords": ["NOI", "IRR", "PBSA", "MSCI", "Stratford", "en-suite", "studio", "cluster", "yield", "occupancy", "Broadway"]},
        "conversation": {"client_events": CLIENT_EVENTS},
    }
    if os.getenv("AGENT_VOICE"):
        conv["tts"] = {"voice_id": os.getenv("AGENT_VOICE")}
    hosts = [h.strip() for h in os.getenv("AGENT_HOSTS", "demo3.ai-vance.de,localhost").split(",") if h.strip()]
    return {
        "name": "Deal Qualification Analyst (demo3)",
        "tags": ["hackathon", "aivance", "deal-qualification"],
        "conversation_config": conv,
        "platform_settings": {"auth": {"enable_auth": True, "allowlist": [{"hostname": h} for h in hosts]}},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--update", metavar="AGENT_ID")
    args = ap.parse_args()
    load_env()

    if args.dry_run:
        print(json.dumps({"tools": [{"tool_config": t} for t in TOOLS], "agent": agent_config(["<tool ids>"])}, indent=2))
        return

    if args.update:
        current = call("GET", f"/v1/convai/agents/{args.update}")
        tool_ids = current.get("conversation_config", {}).get("agent", {}).get("prompt", {}).get("tool_ids", [])
        cfg = agent_config(tool_ids)
        call("PATCH", f"/v1/convai/agents/{args.update}", {"conversation_config": cfg["conversation_config"], "platform_settings": cfg["platform_settings"]})
        print(f"Updated agent {args.update} (kept {len(tool_ids)} tools).")
        return

    tool_ids = []
    for t in TOOLS:
        res = call("POST", "/v1/convai/tools", {"tool_config": t})
        tool_ids.append(res["id"])
        print(f"tool {t['name']}: {res['id']}")
    res = call("POST", "/v1/convai/agents/create", agent_config(tool_ids))
    agent_id = res.get("agent_id")
    print("\nAgent created.")
    print(f"ELEVENLABS_AGENT_ID={agent_id}")
    print("Add this line to .env on the server, then: docker compose up -d --build api")


if __name__ == "__main__":
    main()
