"""dealt API (contract from README.md).

POST /analyze             multipart: files[] (PDF), comment -> {"job_id": "..."}
GET  /analysis/{job_id}   -> {"status": "running|done|error", "steps": [...], "report": {...}}

Until the real pipeline (V7 Go / Grok Bot / MSCI / Parallel ...) is plugged in,
run_pipeline() walks through the steps and returns mock/report.json, flagged "mock": true.
Replace run_pipeline() with the real agent; keep the response shape.
"""
import asyncio
import contextlib
import json
import os
import uuid
from pathlib import Path
from typing import List, Optional

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from integrations import build_router
import mcp_app
from mcp_app import mcp

BASE = Path(__file__).parent
UPLOADS = Path(os.getenv("UPLOAD_DIR", BASE / "cache" / "uploads"))
MOCK_REPORT = BASE / "mock" / "report.json"
if not MOCK_REPORT.exists():  # running from api/ in the repo
    MOCK_REPORT = BASE.parent / "mock" / "report.json"

STEPS = [
    "Extracting brochure",
    "Location & demographics",
    "Room-rate comps",
    "Market yields (MSCI)",
    "Rates (SONIA)",
    "NOI build-up",
    "Returns",
    "Verdict",
]

mcp_http = mcp.streamable_http_app()  # route /mcp, served at https://demo3.ai-vance.de/api/mcp behind Caddy


@contextlib.asynccontextmanager
async def lifespan(_app):
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="dealt", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",") if o.strip()],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)

JOBS: dict = {}  # in-memory; fine for a one-night demo with one API container


async def start_job(files, comment=None) -> str:
    """Save (name, bytes) pairs and start the pipeline. Used by uploads and by Outlook emails."""
    job_id = uuid.uuid4().hex[:12]
    job_dir = UPLOADS / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    for name, data in files:
        name = Path(name or "upload.pdf").name
        (job_dir / name).write_bytes(data)
        saved.append(name)
    JOBS[job_id] = {"status": "running", "steps": [], "report": None, "files": saved, "comment": comment}
    asyncio.get_running_loop().create_task(run_pipeline(job_id))
    return job_id


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/analyze")
async def analyze(
    files: List[UploadFile] = File(..., alias="files[]"),
    comment: Optional[str] = Form(None),
):
    job_id = await start_job([(f.filename, await f.read()) for f in files], comment)
    return {"job_id": job_id}


@app.get("/analysis/{job_id}")
def analysis(job_id: str):
    job = JOBS.get(job_id)
    if job is None and (UPLOADS / Path(job_id).name / "report.json").exists():
        return {"job_id": job_id, "status": "done", "steps": [], "report": json.loads((UPLOADS / Path(job_id).name / "report.json").read_text(encoding="utf-8"))}
    if job is None and job_id == "demo-stratford":
        data = json.loads(MOCK_REPORT.read_text())
        data.setdefault("report", {})["mock"] = data.get("mock", True)
        return data
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job_id")
    return {"job_id": job_id, "status": job["status"], "steps": job["steps"], "report": job["report"]}


async def run_pipeline(job_id: str):
    """Real pipeline (engine/pipeline.py). Falls back to the mock report if it fails."""
    job = JOBS[job_id]
    paths = [str(UPLOADS / job_id / name) for name in job["files"]]
    try:
        from engine.pipeline import analyze
        report = await asyncio.to_thread(analyze, paths, job.get("comment") or "", job["steps"].append)
        report["mock"] = False
        report["input_files"] = job["files"]
        job["report"] = report
        job["status"] = "done"
        (UPLOADS / job_id / "report.json").write_text(json.dumps(report, default=str), encoding="utf-8")
    except Exception as exc:  # fall back to the mock so the demo never breaks
        job["steps"].append(f"Real pipeline failed ({exc}); showing sample report")
        mock = json.loads(MOCK_REPORT.read_text())
        report = mock["report"]
        report["mock"] = True
        report["input_files"] = job["files"]
        report["pipeline_error"] = str(exc)
        job["report"] = report
        job["status"] = "done"


# ---------- ElevenLabs voice: short-lived credentials for the browser ----------
# The API key never reaches the browser. The UI asks for a WebRTC token first, then a WebSocket signed URL.
ELEVEN_BASE = os.getenv("ELEVENLABS_API_BASE", "https://api.elevenlabs.io")


def _eleven_creds():
    key, agent = os.getenv("ELEVENLABS_API_KEY"), os.getenv("ELEVENLABS_AGENT_ID")
    if not key or not agent:
        raise HTTPException(status_code=503, detail="Voice not configured: set ELEVENLABS_API_KEY and ELEVENLABS_AGENT_ID in .env")
    return key, agent


@app.get("/voice/token")
async def voice_token():
    key, agent = _eleven_creds()
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{ELEVEN_BASE}/v1/convai/conversation/token", params={"agent_id": agent}, headers={"xi-api-key": key})
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"ElevenLabs token request failed ({r.status_code})")
    return {"token": r.json().get("token")}


@app.get("/voice/signed-url")
async def voice_signed_url():
    key, agent = _eleven_creds()
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{ELEVEN_BASE}/v1/convai/conversation/get-signed-url", params={"agent_id": agent}, headers={"xi-api-key": key})
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"ElevenLabs signed URL request failed ({r.status_code})")
    return {"signed_url": r.json().get("signed_url")}


# ElevenLabs server tools (stubs): POST https://demo3.ai-vance.de/api/explain_figure etc.
@app.post("/explain_figure")
def explain_figure(payload: dict):
    return {"metric": payload.get("metric"), "chain": [], "status": "not_implemented"}


@app.post("/get_verdict")
def get_verdict(payload: dict):
    job = JOBS.get(payload.get("job_id", ""))
    if not job or not job["report"]:
        return {"status": "unknown_job"}
    return {"verdicts": [a.get("verdict") for a in job["report"].get("assets", [])]}


@app.post("/what_if")
def what_if(payload: dict):
    return {"change": payload.get("change"), "status": "not_implemented"}


app.include_router(build_router(start_job))
app.router.routes.extend(mcp_http.routes)
mcp_app.HOOKS.update(start_job=start_job, job=lambda job_id: JOBS.get(job_id) or (
    {"status": "done", **analysis(job_id)} if (UPLOADS / Path(job_id).name / "report.json").exists() or job_id == "demo-stratford" else None))  # MCP for Claude / Grok / ChatGPT (see mcp_app.py)
