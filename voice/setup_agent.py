#!/usr/bin/env python3
"""Create (or update) the ElevenLabs voice analyst for dealt.

Usage (from the repo root, needs ELEVENLABS_API_KEY in .env or the environment):
    python3 voice/setup_agent.py --dry-run          # print what would be sent
    python3 voice/setup_agent.py                    # create 5 client tools + the agent, print AGENT_ID
    python3 voice/setup_agent.py --update AGENT_ID  # update prompt, first message, LLM and voice of an existing agent
    python3 voice/setup_agent.py --update AGENT_ID --sync-tools  # also create/update the client tools by name

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
    {
        "type": "client",
        "name": "check_inbox",
        "description": "Checks the user's connected Outlook inbox for new real estate deal emails (brochures, teasers, investment memoranda) and shows them in the chat. Returns a numbered list with subject, sender, date and PDF attachments. Use whenever the user asks about their inbox, emails, Outlook or new deals.",
        "expects_response": True,
        "response_timeout_secs": 45,
        "parameters": {"type": "object", "properties": {"days": NUM("How many days back to look, default 14, max 90.")}, "required": []},
    },
    {
        "type": "client",
        "name": "qualify_email",
        "description": "Starts the GO / NO-GO qualification of one deal email from the last check_inbox result: reads its PDF brochure, checks it against market data and opens the traced report when done (about a minute). Pass the email number from check_inbox.",
        "expects_response": True,
        "response_timeout_secs": 30,
        "parameters": {
            "type": "object",
            "properties": {
                "number": NUM("The email's number from the check_inbox result, e.g. 1 for the first."),
                "message_id": {"type": "string", "description": "Optional exact message_id from check_inbox instead of number."},
            },
            "required": [],
        },
    },
    {
        "type": "client",
        "name": "open_sample_report",
        "description": "Opens the Stratford student housing sample report (mock data) on screen. Use when the user wants a demo or an example deal.",
        "expects_response": True,
        "response_timeout_secs": 30,
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "client",
        "name": "connect_outlook",
        "description": "Starts the Microsoft sign-in so the user can connect their Outlook inbox. This leaves the page and ends the voice session. Only call it after the user agreed.",
        "expects_response": True,
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "client",
        "name": "ask_dealt",
        "description": "Hands a request to the dealt text agent, which has the same inbox and qualification tools plus market research and Dealroom (investors, companies, transactions). Use for anything your other tools cannot answer. Returns its answer.",
        "expects_response": True,
        "response_timeout_secs": 90,
        "parameters": {
            "type": "object",
            "properties": {"request": {"type": "string", "description": "The user's request in one clear sentence, with all details they gave."}},
            "required": ["request"],
        },
    },
]

FIRST_MESSAGE = (
    "Hi, I'm dealt. I can check your inbox for new deals, qualify a brochure, "
    "or walk you through a report. What should we look at?"
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
        "asr": {"keywords": ["dealt", "Outlook", "inbox", "NOI", "IRR", "PBSA", "MSCI", "Stratford", "en-suite", "studio", "cluster", "yield", "occupancy", "Broadway"]},
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
    ap.add_argument("--sync-tools", action="store_true", help="with --update: create missing tools and update existing ones by name")
    args = ap.parse_args()
    load_env()

    if args.dry_run:
        print(json.dumps({"tools": [{"tool_config": t} for t in TOOLS], "agent": agent_config(["<tool ids>"])}, indent=2))
        return

    if args.update:
        current = call("GET", f"/v1/convai/agents/{args.update}")
        tool_ids = current.get("conversation_config", {}).get("agent", {}).get("prompt", {}).get("tool_ids", [])
        if args.sync_tools:
            by_name = {}
            for tid in tool_ids:
                by_name[call("GET", f"/v1/convai/tools/{tid}").get("tool_config", {}).get("name")] = tid
            for t in TOOLS:
                if t["name"] in by_name:
                    call("PATCH", f"/v1/convai/tools/{by_name[t['name']]}", {"tool_config": t})
                    print(f"tool {t['name']}: updated")
                else:
                    tool_ids.append(call("POST", "/v1/convai/tools", {"tool_config": t})["id"])
                    print(f"tool {t['name']}: created")
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
