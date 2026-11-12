"""Deal Qualification Agent API (contract from README.md).

POST /analyze             multipart: files[] (PDF), comment -> {"job_id": "..."}
GET  /analysis/{job_id}   -> {"status": "running|done|error", "steps": [...], "report": {...}}

Until the real pipeline (V7 Go / Grok Bot / MSCI / Parallel ...) is plugged in,
run_pipeline() walks through the steps and returns mock/report.json, flagged "mock": true.
Replace run_pipeline() with the real agent; keep the response shape.
"""
import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import List, Optional

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

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

app = FastAPI(title="Deal Qualification Agent")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",") if o.strip()],
    allow_methods=["*"],
    allow_headers=["*"],
)

JOBS: dict = {}  # in-memory; fine for a one-night demo with one API container


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/analyze")
async def analyze(
    background: BackgroundTasks,
    files: List[UploadFile] = File(..., alias="files[]"),
    comment: Optional[str] = Form(None),
):
    job_id = uuid.uuid4().hex[:12]
    job_dir = UPLOADS / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    for f in files:
        name = Path(f.filename or "upload.pdf").name
        (job_dir / name).write_bytes(await f.read())
        saved.append(name)
    JOBS[job_id] = {"status": "running", "steps": [], "report": None, "files": saved, "comment": comment}
    background.add_task(run_pipeline, job_id)
    return {"job_id": job_id}


@app.get("/analysis/{job_id}")
def analysis(job_id: str):
    job = JOBS.get(job_id)
    if job is None and job_id == "demo-stratford":
        return json.loads(MOCK_REPORT.read_text())
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job_id")
    return {"job_id": job_id, "status": job["status"], "steps": job["steps"], "report": job["report"]}


async def run_pipeline(job_id: str):
    """Placeholder pipeline: replace with the real agent calls, one per step."""
    job = JOBS[job_id]
    try:
        for step in STEPS:
            job["steps"].append(step)
            await asyncio.sleep(0.6)
        mock = json.loads(MOCK_REPORT.read_text())
        report = mock["report"]
        report["mock"] = True
        report["input_files"] = job["files"]
        job["report"] = report
        job["status"] = "done"
    except Exception as exc:  # surface errors to the UI instead of hanging
        job["status"] = "error"
        job["report"] = {"error": str(exc)}


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
