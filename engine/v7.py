"""V7 Go extraction: upload a brochure into a V7 Go agent and read back the extracted fields.

Setup in V7 Go (UI, 5 min):
  1. Create an agent (project) with a File property, slug `brochure`.
  2. Add one AI property per field, input = `brochure`, e.g. slugs:
     address, asset_type, tenure, gia_sqft, beds, passing_rent, erv, asking_price, net_initial_yield, tenants
     (prompt each: "From the brochure, extract X. Answer with the value only, plus the page number.")
  3. Copy workspace id + agent (project) id from the URL, create an API key.
.env (repo root, gitignored):
  V7_API_KEY=...  V7_WORKSPACE_ID=...  V7_PROJECT_ID=...
  V7_FILE_PROPERTY=brochure   V7_FIELDS=address,beds,passing_rent,...   V7_HOST=https://go.v7labs.com
"""
import os, pathlib, re, time
import requests

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _env():
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    # server .env uses V7_GO_* names; non-secret defaults for this hackathon workflow
    for a, b in (("V7_API_KEY", "V7_GO_API_KEY"), ("V7_WORKSPACE_ID", "V7_GO_WORKSPACE_ID"), ("V7_PROJECT_ID", "V7_GO_PROJECT_ID")):
        if not os.environ.get(a) and os.environ.get(b):
            os.environ[a] = os.environ[b]
    os.environ.setdefault("V7_WORKSPACE_ID", "01a1ca22-d3b8-7a84-903e-fc33d57e24bd")
    os.environ.setdefault("V7_PROJECT_ID", "01a1d171-042f-71f8-9e29-ed9a0b6591ba")
    os.environ.setdefault("V7_FILE_PROPERTY", "01a1d171-0491-7554-a5f9-2cfaf158f3ff")
    os.environ.setdefault("V7_FIELDS", "address,asset_type,tenure,gia_sqft,beds,passing_rent,erv,asking_price,net_initial_yield,tenants")
    return os.environ


def configured():
    e = _env()
    return all(e.get(k) for k in ("V7_API_KEY", "V7_WORKSPACE_ID", "V7_PROJECT_ID"))


def extract(pdf_path, timeout=180):
    e = _env()
    host = e.get("V7_HOST", "https://go.v7labs.com")
    base = f"{host}/api/workspaces/{e['V7_WORKSPACE_ID']}/projects/{e['V7_PROJECT_ID']}"
    h = {"X-API-KEY": e["V7_API_KEY"]}
    file_prop = e.get("V7_FILE_PROPERTY")  # property ID (slug gives 404 on upload)
    file_slug = e.get("V7_FILE_SLUG", "file1")
    fields = [f.strip() for f in e.get("V7_FIELDS", "").split(",") if f.strip()]

    # 1. create an empty entity (row)
    r = requests.post(f"{base}/entities", json={"fields": {}}, headers=h, timeout=60)
    r.raise_for_status()
    ent = r.json()
    eid = ent.get("id") or ent.get("entity", {}).get("id")

    # 2. upload the PDF into the file property
    pdf = pathlib.Path(pdf_path)
    r = requests.post(f"{base}/entities/{eid}/properties/{file_prop}/start_file_upload",
                      json={"filename": pdf.name}, headers=h, timeout=60)
    r.raise_for_status()
    up = r.json()
    requests.put(up["file_upload_url"], data=pdf.read_bytes(), timeout=300).raise_for_status()
    requests.post(up["confirm_upload_url"], headers=h, timeout=60).raise_for_status()

    # 3. poll until the AI properties are computed
    t0, data = time.time(), {}
    while time.time() - t0 < timeout:
        r = requests.get(f"{base}/entities/{eid}", headers=h, timeout=60)
        r.raise_for_status()
        data = r.json()
        f = data.get("fields", {})
        todo = [k for k in (fields or f) if k not in (file_prop, file_slug, "ai") and f.get(k, {}).get("status") not in ("complete", "failed")]
        if not todo:
            break
        time.sleep(4)

    out = []
    for slug, v in data.get("fields", {}).items():
        if slug in (file_prop, file_slug, "ai") or (fields and slug not in fields):
            continue
        val = (v.get("manual_value") or {}).get("value") or (v.get("tool_value") or {}).get("value")
        pages = sorted({int(x) for x in re.findall(r"p\.?\s*(\d+)", str(val or ""))})
        clean = re.sub(r"\s*\((?:p\.?\s*\d+[,;\s]*)+\)", "", str(val)).strip() if val is not None else None
        out.append({"metric": slug, "label": slug.replace("_", " ").capitalize(), "value": clean, "unit": None,
                    "status": None,
                    "source": {"type": "brochure", "ref": pdf.name, "page": pages[0] if pages else None, "pages": pages,
                               "v7_entity": eid,
                               "raw": (v.get("tool_value") or {}).get("raw_text")}})
    return {"source_file": pdf.name, "v7_entity": eid, "facts": out, "mock": False}
