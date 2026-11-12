"""Outlook (Microsoft Graph), Gmail (Gmail API), Dealroom MCP and the Grok chat agent.

Browser sessions are identified by an httpOnly cookie. OAuth tokens never reach the browser:
they live in TOKEN_FILE (chmod 600) inside the api_cache volume.

GET  /integrations                     -> which sources are configured / connected
GET  /auth/outlook/start               -> redirect to Microsoft sign-in (Mail.Read)
GET  /auth/outlook/callback
GET  /auth/gmail/start                 -> redirect to Google sign-in (gmail.readonly)
GET  /auth/gmail/callback
GET  /auth/dealroom/start              -> OAuth 2.1 + PKCE + dynamic client registration
GET  /auth/dealroom/callback
POST /integrations/{name}/disconnect
GET  /outlook/deals?days=14            -> recent emails that look like real estate deals
POST /outlook/messages/{id}/analyze    -> run the deal pipeline on the email's PDF attachments (Gmail ids are prefixed "g:")
POST /chat                             -> {"messages": [...]} -> {"reply", "actions"}
"""
import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable, List, Tuple
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import RedirectResponse

log = logging.getLogger("dealt.integrations")

PUBLIC_BASE = os.getenv("PUBLIC_BASE_URL", "https://demo3.ai-vance.de").rstrip("/")
API_PREFIX = os.getenv("PUBLIC_API_PREFIX", "/api")  # Caddy strips /api before it reaches us
TOKEN_FILE = Path(os.getenv("TOKEN_FILE", Path(__file__).parent / "cache" / "tokens.json"))
COOKIE = "dq_sid"

MS_TENANT = os.getenv("MS_TENANT", "common")
MS_AUTHORITY = f"https://login.microsoftonline.com/{MS_TENANT}/oauth2/v2.0"
MS_SCOPES = "offline_access User.Read Mail.Read"
GRAPH = "https://graph.microsoft.com/v1.0"

G_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
G_TOKEN = "https://oauth2.googleapis.com/token"
G_SCOPES = "openid email https://www.googleapis.com/auth/gmail.readonly"
GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"

DR_MCP = os.getenv("DEALROOM_MCP_URL", "https://mcp.dealroom.co/mcp")
DR_ISSUER = os.getenv("DEALROOM_OAUTH_ISSUER", "https://mcp.dealroom.co")
DR_SCOPES = "openid read:current_user offline_access"

XAI_BASE = os.getenv("XAI_API_BASE", "https://api.x.ai/v1")
XAI_MODEL = os.getenv("XAI_MODEL", "grok-4.7")

StartJob = Callable[[List[Tuple[str, bytes]], str], Awaitable[str]]


# ---------------------------------------------------------------- token store
def _load() -> dict:
    try:
        return json.loads(TOKEN_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(data: dict) -> None:
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = TOKEN_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    os.chmod(tmp, 0o600)
    tmp.replace(TOKEN_FILE)


def _session(sid: str) -> dict:
    return _load().get("sessions", {}).get(sid, {})


def _update_session(sid: str, key: str, value) -> None:
    data = _load()
    sess = data.setdefault("sessions", {}).setdefault(sid, {})
    if value is None:
        sess.pop(key, None)
    else:
        sess[key] = value
    _save(data)


def _sid(request: Request) -> str | None:
    sid = request.cookies.get(COOKIE)
    return sid if sid and re.fullmatch(r"[A-Za-z0-9_-]{20,64}", sid) else None


def _ensure_sid(request: Request, response: Response) -> str:
    sid = _sid(request)
    if not sid:
        sid = secrets.token_urlsafe(24)
    response.set_cookie(COOKIE, sid, max_age=60 * 60 * 24 * 30, httponly=True, secure=PUBLIC_BASE.startswith("https"), samesite="lax", path="/")
    return sid


def _pkce() -> Tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _callback(name: str) -> str:
    return f"{PUBLIC_BASE}{API_PREFIX}/auth/{name}/callback"


def _back_to_app(**params) -> RedirectResponse:
    return RedirectResponse(f"{PUBLIC_BASE}/?{urlencode(params)}", status_code=302)


# ---------------------------------------------------------------- Outlook / Microsoft Graph
def _ms_configured() -> bool:
    return bool(os.getenv("MS_CLIENT_ID") and os.getenv("MS_CLIENT_SECRET"))


async def _ms_token_request(form: dict) -> dict:
    form = {**form, "client_id": os.getenv("MS_CLIENT_ID"), "client_secret": os.getenv("MS_CLIENT_SECRET"), "scope": MS_SCOPES}
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(f"{MS_AUTHORITY}/token", data=form)
    if r.status_code != 200:
        raise HTTPException(502, f"Microsoft token request failed ({r.status_code})")
    tok = r.json()
    tok["expires_at"] = time.time() + int(tok.get("expires_in", 3600)) - 60
    return tok


async def _ms_access(sid: str) -> str:
    tok = _session(sid).get("outlook")
    if not tok:
        raise HTTPException(409, "Outlook is not connected")
    if time.time() > tok.get("expires_at", 0):
        new = await _ms_token_request({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
        new.setdefault("refresh_token", tok["refresh_token"])
        new["account"] = tok.get("account")
        _update_session(sid, "outlook", new)
        tok = new
    return tok["access_token"]


NOISE = r"newsletter|unsubscribe|abmelden|webinar|invoice|rechnung|receipt|password|verify your|security alert"
KEYWORDS = {
    25: r"investment memorandum|offering memorandum|information memorandum|sales? brochure|verkaufsexpos[eé]|expos[eé]|teaser|off[- ]market|for sale|zum verkauf|kaufangebot",
    15: r"guide price|asking price|kaufpreis|net initial yield|\bNIY\b|cap rate|\bNOI\b|rendite|faktor|portfolio|mehrfamilienhaus|wohn- und gesch[aä]ftshaus|gewerbe",
    8: r"hotel|\bPBSA\b|student accommodation|office|b[uü]ro|logistics|logistik|retail|einzelhandel|residential|wohnen|immobilie|objekt|property|real estate|freehold|leasehold|sq ?ft|sq ?m|m²|acres|tenant|mieter|wault",
}


def score_deal_email(subject: str, preview: str, sender: str, attachments: List[str]) -> Tuple[int, List[str]]:
    text = f"{subject}\n{preview}\n{sender}"
    score, reasons = 0, []
    for weight, pattern in KEYWORDS.items():
        hits = {m.group(0).lower() for m in re.finditer(pattern, text, re.I)}
        if hits:
            score += weight * min(len(hits), 3)
            reasons += sorted(hits)[:3]
    pdfs = [a for a in attachments if a.lower().endswith(".pdf")]
    if pdfs:
        score += 15
        reasons.append(f"{len(pdfs)} PDF")
        if any(re.search(r"brochure|\bIM\b|teaser|expos|memorandum|particulars|deck", a, re.I) for a in pdfs):
            score += 20
    if re.search(NOISE, text, re.I):
        score -= 30
    return max(0, min(100, score)), reasons


async def outlook_deals(sid: str, days: int = 14, limit: int = 12) -> List[dict]:
    token = await _ms_access(sid)
    since = (datetime.now(timezone.utc) - timedelta(days=max(1, min(days, 90)))).strftime("%Y-%m-%dT%H:%M:%SZ")
    params = {
        "$filter": f"receivedDateTime ge {since}",
        "$orderby": "receivedDateTime desc",
        "$top": "50",
        "$select": "id,subject,from,receivedDateTime,bodyPreview,hasAttachments,webLink",
    }
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10)) as c:
        try:
            r = await c.get(f"{GRAPH}/me/mailFolders/inbox/messages", params=params, headers=headers)
        except httpx.HTTPError:
            raise HTTPException(504, "Outlook did not respond in time, try again")
        if r.status_code != 200:
            raise HTTPException(502, f"Outlook request failed ({r.status_code})")
        messages = r.json().get("value", [])
        # Attachment names only (no content), fetched in parallel for mails that have any.
        sem = asyncio.Semaphore(8)

        async def att_names(mid: str) -> List[str]:
            async with sem:
                try:
                    a = await c.get(f"{GRAPH}/me/messages/{mid}/attachments", params={"$select": "name"}, headers=headers, timeout=15)
                    return [x.get("name", "") for x in a.json().get("value", [])] if a.status_code == 200 else []
                except httpx.HTTPError:
                    return []

        att = await asyncio.gather(*[att_names(m["id"]) if m.get("hasAttachments") else asyncio.sleep(0, result=[]) for m in messages])
    out = []
    for m, names in zip(messages, att):
        sender = (m.get("from") or {}).get("emailAddress", {})
        score, reasons = score_deal_email(m.get("subject") or "", m.get("bodyPreview") or "", f"{sender.get('name', '')} {sender.get('address', '')}", names)
        if score < 25:
            continue
        out.append({
            "id": m["id"], "subject": m.get("subject") or "(no subject)", "from": sender.get("name") or sender.get("address"),
            "received": m.get("receivedDateTime"), "preview": (m.get("bodyPreview") or "")[:240],
            "pdfs": [n for n in names if n.lower().endswith(".pdf")], "score": score, "reasons": reasons, "web_link": m.get("webLink"),
        })
    out.sort(key=lambda e: (e["score"], e["received"] or ""), reverse=True)
    return out[:limit]


async def outlook_pdfs(sid: str, message_id: str) -> Tuple[str, List[Tuple[str, bytes]]]:
    if not re.fullmatch(r"[A-Za-z0-9=_+/-]{10,400}", message_id):
        raise HTTPException(400, "Invalid message id")
    token = await _ms_access(sid)
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=60) as c:
        msg = await c.get(f"{GRAPH}/me/messages/{message_id}", params={"$select": "subject"}, headers=headers)
        att = await c.get(f"{GRAPH}/me/messages/{message_id}/attachments", headers=headers)
    if msg.status_code != 200 or att.status_code != 200:
        raise HTTPException(502, "Could not read the email from Outlook")
    files = []
    for a in att.json().get("value", []):
        name = Path(a.get("name") or "attachment.pdf").name
        if a.get("@odata.type") == "#microsoft.graph.fileAttachment" and (name.lower().endswith(".pdf") or a.get("contentType") == "application/pdf"):
            files.append((name, base64.b64decode(a.get("contentBytes") or "")))
    return msg.json().get("subject") or "", files


# ---------------------------------------------------------------- Gmail
def _g_configured() -> bool:
    return bool(os.getenv("GOOGLE_CLIENT_ID") and os.getenv("GOOGLE_CLIENT_SECRET"))


async def _g_token_request(form: dict) -> dict:
    form = {**form, "client_id": os.getenv("GOOGLE_CLIENT_ID"), "client_secret": os.getenv("GOOGLE_CLIENT_SECRET")}
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(G_TOKEN, data=form)
    if r.status_code != 200:
        raise HTTPException(502, f"Google token request failed ({r.status_code})")
    tok = r.json()
    tok["expires_at"] = time.time() + int(tok.get("expires_in", 3600)) - 60
    return tok


async def _g_access(sid: str) -> str:
    tok = _session(sid).get("gmail")
    if not tok:
        raise HTTPException(409, "Gmail is not connected")
    if time.time() > tok.get("expires_at", 0):
        new = await _g_token_request({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
        new.setdefault("refresh_token", tok["refresh_token"])
        new["account"] = tok.get("account")
        _update_session(sid, "gmail", new)
        tok = new
    return tok["access_token"]


def _g_parts(payload: dict) -> List[dict]:
    out, stack = [], [payload or {}]
    while stack:
        part = stack.pop()
        if part.get("filename"):
            out.append(part)
        stack += part.get("parts") or []
    return out


async def gmail_deals(sid: str, days: int = 14, limit: int = 12) -> List[dict]:
    headers = {"Authorization": f"Bearer {await _g_access(sid)}"}
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(f"{GMAIL}/messages", params={"q": f"in:inbox newer_than:{max(1, min(days, 90))}d", "maxResults": 40}, headers=headers)
        if r.status_code != 200:
            raise HTTPException(502, f"Gmail request failed ({r.status_code})")
        ids = [m["id"] for m in r.json().get("messages", [])]
        msgs = await asyncio.gather(*[c.get(f"{GMAIL}/messages/{i}", params={"format": "full"}, headers=headers) for i in ids])
    out = []
    for m in (x.json() for x in msgs if x.status_code == 200):
        h = {k["name"].lower(): k["value"] for k in m.get("payload", {}).get("headers", [])}
        names = [p["filename"] for p in _g_parts(m.get("payload"))]
        score, reasons = score_deal_email(h.get("subject", ""), m.get("snippet", ""), h.get("from", ""), names)
        if score < 25:
            continue
        received = datetime.fromtimestamp(int(m.get("internalDate", 0)) / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        out.append({
            "id": f"g:{m['id']}", "subject": h.get("subject") or "(no subject)", "from": re.sub(r"\s*<.*>", "", h.get("from", "")) or h.get("from"),
            "received": received, "preview": (m.get("snippet") or "")[:240], "source": "gmail",
            "pdfs": [n for n in names if n.lower().endswith(".pdf")], "score": score, "reasons": reasons,
            "web_link": f"https://mail.google.com/mail/u/0/#inbox/{m['id']}",
        })
    out.sort(key=lambda e: (e["score"], e["received"] or ""), reverse=True)
    return out[:limit]


async def gmail_pdfs(sid: str, message_id: str) -> Tuple[str, List[Tuple[str, bytes]]]:
    if not re.fullmatch(r"[A-Za-z0-9]{8,64}", message_id):
        raise HTTPException(400, "Invalid message id")
    headers = {"Authorization": f"Bearer {await _g_access(sid)}"}
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.get(f"{GMAIL}/messages/{message_id}", params={"format": "full"}, headers=headers)
        if r.status_code != 200:
            raise HTTPException(502, "Could not read the email from Gmail")
        m = r.json()
        subject = next((k["value"] for k in m.get("payload", {}).get("headers", []) if k["name"].lower() == "subject"), "")
        files = []
        for part in _g_parts(m.get("payload")):
            name = Path(part["filename"]).name
            if not (name.lower().endswith(".pdf") or part.get("mimeType") == "application/pdf"):
                continue
            body = part.get("body") or {}
            data = body.get("data")
            if not data and body.get("attachmentId"):
                a = await c.get(f"{GMAIL}/messages/{message_id}/attachments/{body['attachmentId']}", headers=headers)
                data = a.json().get("data") if a.status_code == 200 else None
            if data:
                files.append((name, base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))))
    return subject, files


# ---------------------------------------------------------------- Dealroom MCP (OAuth 2.1)
async def _dr_metadata() -> dict:
    data = _load()
    if data.get("dealroom_meta"):
        return data["dealroom_meta"]
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(f"{DR_ISSUER}/.well-known/oauth-authorization-server")
    r.raise_for_status()
    meta = r.json()
    data["dealroom_meta"] = meta
    _save(data)
    return meta


async def _dr_client_id() -> str:
    data = _load()
    reg = data.get("dealroom_client")
    if reg and reg.get("redirect_uri") == _callback("dealroom"):
        return reg["client_id"]
    meta = await _dr_metadata()
    body = {
        "client_name": "dealt",
        "redirect_uris": [_callback("dealroom")],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "scope": DR_SCOPES,
    }
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(meta["registration_endpoint"], json=body)
    if r.status_code not in (200, 201):
        if "invalid_redirect_uri" in r.text:
            # Dealroom only accepts redirect hosts on its MCP client allowlist; ask Dealroom to add this domain.
            raise HTTPException(403, "dealroom_host_not_allowed")
        raise HTTPException(502, f"Dealroom client registration failed ({r.status_code})")
    client_id = r.json()["client_id"]
    data = _load()
    data["dealroom_client"] = {"client_id": client_id, "redirect_uri": _callback("dealroom")}
    _save(data)
    return client_id


async def _dr_token_request(form: dict) -> dict:
    meta = await _dr_metadata()
    form = {**form, "client_id": await _dr_client_id(), "resource": DR_MCP}
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(meta["token_endpoint"], data=form)
    if r.status_code != 200:
        raise HTTPException(502, f"Dealroom token request failed ({r.status_code})")
    tok = r.json()
    tok["expires_at"] = time.time() + int(tok.get("expires_in", 3600)) - 60
    return tok


async def _dr_access(sid: str) -> str:
    tok = _session(sid).get("dealroom")
    if not tok:
        raise HTTPException(409, "Dealroom is not connected")
    if time.time() > tok.get("expires_at", 0):
        if not tok.get("refresh_token"):
            _update_session(sid, "dealroom", None)
            raise HTTPException(409, "Dealroom session expired, connect again")
        new = await _dr_token_request({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
        new.setdefault("refresh_token", tok["refresh_token"])
        _update_session(sid, "dealroom", new)
        tok = new
    return tok["access_token"]


class MCPClient:
    """Minimal MCP client over Streamable HTTP (JSON or SSE responses)."""

    def __init__(self, url: str, token: str):
        self.url, self.token, self.session_id, self._id = url, token, None, 0

    def _headers(self) -> dict:
        h = {"Authorization": f"Bearer {self.token}", "Accept": "application/json, text/event-stream", "Content-Type": "application/json", "MCP-Protocol-Version": "2025-06-18"}
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    async def _post(self, c: httpx.AsyncClient, method: str, params: dict | None = None, notify: bool = False):
        body = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        if not notify:
            self._id += 1
            body["id"] = self._id
        r = await c.post(self.url, json=body, headers=self._headers())
        if r.status_code == 401:
            raise HTTPException(409, "Dealroom rejected the sign-in (Premium seat needed?)")
        if r.status_code >= 400:
            raise HTTPException(502, f"Dealroom MCP error ({r.status_code})")
        self.session_id = r.headers.get("mcp-session-id", self.session_id)
        if notify:
            return None
        if r.headers.get("content-type", "").startswith("text/event-stream"):
            msg = None
            for line in r.text.splitlines():
                if line.startswith("data:"):
                    try:
                        cand = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    if cand.get("id") == body["id"]:
                        msg = cand
            if msg is None:
                raise HTTPException(502, "Empty MCP response")
        else:
            msg = r.json()
        if "error" in msg:
            raise HTTPException(502, f"Dealroom: {msg['error'].get('message', 'error')}")
        return msg.get("result")

    async def __aenter__(self):
        self._c = httpx.AsyncClient(timeout=90)
        await self._post(self._c, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "dealt", "version": "1.0"}})
        await self._post(self._c, "notifications/initialized", notify=True)
        return self

    async def __aexit__(self, *exc):
        await self._c.aclose()

    async def list_tools(self) -> List[dict]:
        return (await self._post(self._c, "tools/list", {}) or {}).get("tools", [])

    async def call_tool(self, name: str, arguments: dict) -> str:
        res = await self._post(self._c, "tools/call", {"name": name, "arguments": arguments}) or {}
        parts = [p.get("text", "") for p in res.get("content", []) if p.get("type") == "text"]
        if not parts and res.get("structuredContent") is not None:
            parts = [json.dumps(res["structuredContent"])]
        return "\n".join(parts)[:24000]


_DR_TOOLS_CACHE: dict = {}


async def dealroom_tools(sid: str) -> List[dict]:
    if _DR_TOOLS_CACHE.get("tools") and time.time() - _DR_TOOLS_CACHE["at"] < 3600:
        return _DR_TOOLS_CACHE["tools"]
    async with MCPClient(DR_MCP, await _dr_access(sid)) as mcp:
        tools = await mcp.list_tools()
    _DR_TOOLS_CACHE.update(tools=tools, at=time.time())
    return tools


# ---------------------------------------------------------------- chat agent (Grok)
SYSTEM = """You are dealt, a deal qualification agent for commercial real estate investors.
You help the user triage inbound deals (sale brochures, investment memoranda, teasers) and qualify them into a traced GO / NO-GO.
Tools:
- check_inbox: scan the user's connected Outlook inbox for new real estate deal emails.
- analyze_email: run the full qualification pipeline on the PDF brochure(s) attached to one email. The report opens on screen.
- load_sample_report: open the 78-102 Broadway, Stratford sample report.
- dealroom__*: Dealroom.co data (companies, investors, transactions, fundings). Only available when Dealroom is listed under connected sources; otherwise never mention Dealroom.
Rules: never invent figures. Every number you state must come from a tool result; say where it came from. If a source is not connected, tell the user to connect it with the + button.
Be concise. Use short markdown: bullet lists, **bold** for the key verdict or number."""

BASE_TOOLS = [
    {"type": "function", "function": {"name": "check_inbox", "description": "List recent emails in the connected Outlook and/or Gmail inbox that look like real estate deals.", "parameters": {"type": "object", "properties": {"days": {"type": "integer", "description": "How many days back, default 14"}}}}},
    {"type": "function", "function": {"name": "analyze_email", "description": "Qualify the deal in one email: runs the pipeline on its PDF attachments and opens the report.", "parameters": {"type": "object", "properties": {"message_id": {"type": "string"}}, "required": ["message_id"]}}},
    {"type": "function", "function": {"name": "load_sample_report", "description": "Open the Stratford sample deal report.", "parameters": {"type": "object", "properties": {}}}},
]


def _clean_schema(schema: dict | None) -> dict:
    schema = dict(schema or {"type": "object", "properties": {}})
    schema.pop("$schema", None)
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    return schema


def build_router(start_job: StartJob) -> APIRouter:
    router = APIRouter()

    @router.get("/integrations")
    async def integrations(request: Request, response: Response):
        sid = _ensure_sid(request, response)
        sess = _session(sid)
        return {
            "outlook": {"configured": _ms_configured(), "connected": "outlook" in sess, "account": (sess.get("outlook") or {}).get("account")},
            "gmail": {"configured": _g_configured(), "connected": "gmail" in sess, "account": (sess.get("gmail") or {}).get("account")},
            "dealroom": {"configured": True, "connected": "dealroom" in sess},
            "llm": {"configured": bool(os.getenv("XAI_API_KEY")), "model": XAI_MODEL},
        }

    @router.post("/integrations/{name}/disconnect")
    async def disconnect(name: str, request: Request):
        sid = _sid(request)
        if name not in ("outlook", "gmail", "dealroom"):
            raise HTTPException(404, "Unknown integration")
        if sid:
            _update_session(sid, name, None)
        return {"ok": True}

    # ----- Outlook OAuth
    @router.get("/auth/outlook/start")
    async def outlook_start(request: Request):
        if not _ms_configured():
            return _back_to_app(error="outlook_not_configured")
        state, (verifier, challenge) = secrets.token_urlsafe(24), _pkce()
        resp = RedirectResponse(f"{MS_AUTHORITY}/authorize?" + urlencode({
            "client_id": os.getenv("MS_CLIENT_ID"), "response_type": "code", "redirect_uri": _callback("outlook"),
            "response_mode": "query", "scope": MS_SCOPES, "state": state, "code_challenge": challenge,
            "code_challenge_method": "S256", "prompt": "select_account",
        }), status_code=302)
        sid = _ensure_sid(request, resp)
        _update_session(sid, "outlook_pending", {"state": state, "verifier": verifier, "at": time.time()})
        return resp

    @router.get("/auth/outlook/callback")
    async def outlook_callback(request: Request, code: str = "", state: str = "", error: str = ""):
        sid = _sid(request)
        pending = _session(sid).get("outlook_pending") if sid else None
        if error or not code or not pending or not secrets.compare_digest(state, pending["state"]):
            return _back_to_app(error="outlook_auth_failed")
        _update_session(sid, "outlook_pending", None)
        tok = await _ms_token_request({"grant_type": "authorization_code", "code": code, "redirect_uri": _callback("outlook"), "code_verifier": pending["verifier"]})
        async with httpx.AsyncClient(timeout=10) as c:
            me = await c.get(f"{GRAPH}/me", params={"$select": "mail,userPrincipalName,displayName"}, headers={"Authorization": f"Bearer {tok['access_token']}"})
        if me.status_code == 200:
            tok["account"] = me.json().get("mail") or me.json().get("userPrincipalName")
        _update_session(sid, "outlook", tok)
        return _back_to_app(connected="outlook")

    # ----- Gmail OAuth
    @router.get("/auth/gmail/start")
    async def gmail_start(request: Request):
        if not _g_configured():
            return _back_to_app(error="gmail_not_configured")
        state, (verifier, challenge) = secrets.token_urlsafe(24), _pkce()
        resp = RedirectResponse(f"{G_AUTH}?" + urlencode({
            "client_id": os.getenv("GOOGLE_CLIENT_ID"), "response_type": "code", "redirect_uri": _callback("gmail"),
            "scope": G_SCOPES, "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
            "access_type": "offline", "prompt": "consent select_account", "include_granted_scopes": "true",
        }), status_code=302)
        sid = _ensure_sid(request, resp)
        _update_session(sid, "gmail_pending", {"state": state, "verifier": verifier, "at": time.time()})
        return resp

    @router.get("/auth/gmail/callback")
    async def gmail_callback(request: Request, code: str = "", state: str = "", error: str = ""):
        sid = _sid(request)
        pending = _session(sid).get("gmail_pending") if sid else None
        if error or not code or not pending or not secrets.compare_digest(state, pending["state"]):
            return _back_to_app(error="gmail_auth_failed")
        _update_session(sid, "gmail_pending", None)
        tok = await _g_token_request({"grant_type": "authorization_code", "code": code, "redirect_uri": _callback("gmail"), "code_verifier": pending["verifier"]})
        async with httpx.AsyncClient(timeout=10) as c:
            prof = await c.get(f"{GMAIL}/profile", headers={"Authorization": f"Bearer {tok['access_token']}"})
        if prof.status_code == 200:
            tok["account"] = prof.json().get("emailAddress")
        _update_session(sid, "gmail", tok)
        return _back_to_app(connected="gmail")

    # ----- Dealroom OAuth 2.1
    @router.get("/auth/dealroom/start")
    async def dealroom_start(request: Request):
        try:
            meta = await _dr_metadata()
            client_id = await _dr_client_id()
        except HTTPException as e:
            return _back_to_app(error=e.detail if e.detail == "dealroom_host_not_allowed" else "dealroom_unavailable")
        except httpx.HTTPError:
            return _back_to_app(error="dealroom_unavailable")
        state, (verifier, challenge) = secrets.token_urlsafe(24), _pkce()
        resp = RedirectResponse(meta["authorization_endpoint"] + "?" + urlencode({
            "client_id": client_id, "response_type": "code", "redirect_uri": _callback("dealroom"), "scope": DR_SCOPES,
            "state": state, "code_challenge": challenge, "code_challenge_method": "S256", "resource": DR_MCP,
        }), status_code=302)
        sid = _ensure_sid(request, resp)
        _update_session(sid, "dealroom_pending", {"state": state, "verifier": verifier, "at": time.time()})
        return resp

    @router.get("/auth/dealroom/callback")
    async def dealroom_callback(request: Request, code: str = "", state: str = "", error: str = ""):
        sid = _sid(request)
        pending = _session(sid).get("dealroom_pending") if sid else None
        if error or not code or not pending or not secrets.compare_digest(state, pending["state"]):
            return _back_to_app(error="dealroom_auth_failed")
        _update_session(sid, "dealroom_pending", None)
        tok = await _dr_token_request({"grant_type": "authorization_code", "code": code, "redirect_uri": _callback("dealroom"), "code_verifier": pending["verifier"]})
        _update_session(sid, "dealroom", tok)
        return _back_to_app(connected="dealroom")

    # ----- Outlook data
    @router.get("/outlook/deals")
    async def deals(request: Request, days: int = 14):
        sid = _sid(request)
        if not sid:
            raise HTTPException(409, "Outlook is not connected")
        return {"emails": await outlook_deals(sid, days)}

    async def _analyze_email(sid: str, message_id: str) -> dict:
        gmail = message_id.startswith("g:")
        subject, files = await (gmail_pdfs(sid, message_id[2:]) if gmail else outlook_pdfs(sid, message_id))
        if not files:
            return {"error": "This email has no PDF attachment to analyse.", "subject": subject}
        job_id = await start_job(files, f"From {'Gmail' if gmail else 'Outlook'}: {subject}")
        return {"job_id": job_id, "subject": subject, "files": [f[0] for f in files]}

    @router.post("/outlook/messages/{message_id}/analyze")
    async def analyze_email(message_id: str, request: Request):
        sid = _sid(request)
        if not sid:
            raise HTTPException(409, "Outlook is not connected")
        return await _analyze_email(sid, message_id)

    # ----- chat
    @router.post("/chat")
    async def chat(payload: dict, request: Request, response: Response):
        sid = _ensure_sid(request, response)
        msgs = [m for m in (payload.get("messages") or [])[-20:] if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str)]
        if not msgs:
            raise HTTPException(400, "No message")
        sess = _session(sid)
        actions: List[dict] = []

        async def run_tool(name: str, args: dict) -> str:
            try:
                if name == "check_inbox":
                    if "outlook" not in sess and "gmail" not in sess:
                        return "No inbox is connected. Ask the user to connect Outlook or Gmail with the + button."
                    days = int(args.get("days") or 14)
                    emails = (await outlook_deals(sid, days) if "outlook" in sess else []) + (await gmail_deals(sid, days) if "gmail" in sess else [])
                    emails.sort(key=lambda e: (e["score"], e["received"] or ""), reverse=True)
                    actions.append({"type": "emails", "emails": emails})
                    return json.dumps([{k: e[k] for k in ("id", "subject", "from", "received", "pdfs", "score", "preview")} for e in emails])
                if name == "analyze_email":
                    res = await _analyze_email(sid, str(args.get("message_id", "")))
                    if res.get("job_id"):
                        actions.append({"type": "analyze", "job_id": res["job_id"], "subject": res["subject"]})
                    return json.dumps(res)
                if name == "load_sample_report":
                    actions.append({"type": "sample"})
                    return "The Stratford sample report is now open on screen (mock data)."
                if name.startswith("dealroom__"):
                    async with MCPClient(DR_MCP, await _dr_access(sid)) as mcp:
                        return await mcp.call_tool(name[len("dealroom__"):], args)
                return f"Unknown tool {name}"
            except HTTPException as e:
                return f"Tool error: {e.detail}"
            except Exception as e:  # never let a slow or broken source crash the chat
                log.exception("tool %s failed", name)
                return f"Tool error: {type(e).__name__}, the source did not respond. Tell the user and suggest trying again."

        if not os.getenv("XAI_API_KEY"):
            return await _fallback_chat(msgs[-1]["content"], sess, run_tool, actions)

        tools = list(BASE_TOOLS)
        if "dealroom" in sess:
            try:
                for t in await dealroom_tools(sid):
                    tools.append({"type": "function", "function": {"name": f"dealroom__{t['name']}", "description": (t.get("description") or "")[:900], "parameters": _clean_schema(t.get("inputSchema"))}})
            except HTTPException:
                pass
        connected = [n for n in ("outlook", "gmail", "dealroom") if n in sess]
        convo = [{"role": "system", "content": SYSTEM + f"\nConnected sources: {', '.join(connected) or 'none'}."}] + msgs
        async with httpx.AsyncClient(timeout=120) as c:
            for _ in range(6):
                r = await c.post(f"{XAI_BASE}/chat/completions", headers={"Authorization": f"Bearer {os.getenv('XAI_API_KEY')}"},
                                 json={"model": XAI_MODEL, "messages": convo, "tools": tools, "temperature": 0.2})
                if r.status_code != 200:
                    raise HTTPException(502, f"Grok request failed ({r.status_code})")
                msg = r.json()["choices"][0]["message"]
                calls = msg.get("tool_calls") or []
                if not calls:
                    return {"reply": msg.get("content") or "", "actions": actions}
                convo.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
                for call in calls:
                    try:
                        args = json.loads(call["function"].get("arguments") or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    actions.append({"type": "tool", "name": call["function"]["name"]})
                    convo.append({"role": "tool", "tool_call_id": call["id"], "content": await run_tool(call["function"]["name"], args)})
        return {"reply": "I stopped after several tool calls. Try a more specific question.", "actions": actions}

    return router


async def _fallback_chat(text: str, sess: dict, run_tool, actions: List[dict]) -> dict:
    """Keyword routing when no LLM key is configured, so the demo still works."""
    t = text.lower()
    if re.search(r"inbox|e-?mail|outlook|mail|posteingang", t):
        if "outlook" not in sess and "gmail" not in sess:
            return {"reply": "No inbox is connected yet. Click **+** → **Outlook** or **Gmail** and I'll scan it for new deals.", "actions": [{"type": "connect", "name": "outlook"}, {"type": "connect", "name": "gmail"}]}
        await run_tool("check_inbox", {})
        n = len(actions[-1]["emails"]) if actions else 0
        return {"reply": f"I found **{n}** email{'s' if n != 1 else ''} that look like real estate deals." + (" Pick one to qualify it." if n else ""), "actions": actions}
    if re.search(r"sample|stratford|demo|beispiel", t):
        await run_tool("load_sample_report", {})
        return {"reply": "Opened the **78-102 Broadway, Stratford** sample report.", "actions": actions}
    return {"reply": "The chat model isn't configured on the server yet (`XAI_API_KEY`). Meanwhile I can **check your inbox**, open the **Stratford sample**, or qualify a **PDF brochure** you drop here.", "actions": actions}
