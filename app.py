import base64
import os
import shutil
import uuid
from typing import List

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
import json

from engine import build_paper_book, FORUM_CAPS
from pypdf import PdfReader

BASE = os.path.dirname(os.path.abspath(__file__))
SESSIONS = os.path.join(BASE, "sessions")
os.makedirs(SESSIONS, exist_ok=True)

app = FastAPI(title="Paper Book Compiler")

# Simple shared-password gate for when this is hosted somewhere reachable
# over the internet (e.g. Render), so a link isn't wide open to anyone who
# finds the URL. Locally, if PAPERBOOK_USER/PAPERBOOK_PASS aren't set,
# auth is skipped entirely — nothing changes for `uvicorn app:app` on your
# own machine.
BASIC_USER = os.environ.get("PAPERBOOK_USER")
BASIC_PASS = os.environ.get("PAPERBOOK_PASS")


class BasicAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if not BASIC_USER or not BASIC_PASS:
            return await call_next(request)
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("basic "):
            try:
                decoded = base64.b64decode(auth.split(" ", 1)[1]).decode()
                user, _, pw = decoded.partition(":")
                if user == BASIC_USER and pw == BASIC_PASS:
                    return await call_next(request)
            except Exception:
                pass
        return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Paper Book Compiler"'})


app.add_middleware(BasicAuthMiddleware)


def session_dir(sid: str) -> str:
    d = os.path.join(SESSIONS, sid)
    os.makedirs(d, exist_ok=True)
    return d


@app.post("/api/session")
def new_session():
    sid = uuid.uuid4().hex[:12]
    session_dir(sid)
    return {"session_id": sid}


@app.post("/api/upload")
async def upload(session_id: str = Form(...), files: List[UploadFile] = File(...)):
    d = os.path.join(session_dir(session_id), "annexures")
    os.makedirs(d, exist_ok=True)
    results = []
    for f in files:
        safe_name = f.filename.replace("/", "_")
        dest = os.path.join(d, safe_name)
        with open(dest, "wb") as out:
            shutil.copyfileobj(f.file, out)
        try:
            pages = PdfReader(dest).get_num_pages()
            ok = True
        except Exception as e:
            pages = 0
            ok = False
        results.append({
            "id": safe_name, "filename": f.filename,
            "page_count": pages, "ok": ok,
        })
    return {"files": results}


@app.post("/api/upload_signature")
async def upload_signature(session_id: str = Form(...), file: UploadFile = File(...)):
    d = session_dir(session_id)
    dest = os.path.join(d, "signature" + os.path.splitext(file.filename)[1])
    with open(dest, "wb") as out:
        shutil.copyfileobj(file.file, out)
    return {"path": dest}


@app.post("/api/generate")
async def generate(payload: dict):
    session_id = payload["session_id"]
    forum = payload["forum"]
    continuous = payload.get("continuous_numbering", True)
    annexures = payload["annexures"]  # list of {id, heading, particulars, order}

    d = session_dir(session_id)
    ax_dir = os.path.join(d, "annexures")
    manifest = []
    for a in annexures:
        manifest.append({
            "id": a["id"], "heading": a.get("heading", ""), "particulars": a["particulars"],
            "filepath": os.path.join(ax_dir, a["id"]), "order": int(a["order"]),
        })

    # ---- cause title ----
    cause_lines = [ln for ln in [
        payload.get("court_name", ""),
        payload.get("jurisdiction", ""),
        payload.get("case_no_line", ""),
    ] if ln.strip()]
    matter_heading = payload.get("matter_heading", "IN THE MATTER OF:") or "IN THE MATTER OF:"
    parties = []
    p1_name = payload.get("party1_name", "").strip()
    p1_desig = payload.get("party1_designation", "").strip()
    p2_name = payload.get("party2_name", "").strip()
    p2_desig = payload.get("party2_designation", "").strip()
    if p1_name:
        parties.append((p1_name, p1_desig))
    if p2_name:
        parties.append((p2_name, p2_desig))

    # ---- filing / counsel block ----
    counsel_name = payload.get("counsel_name", "").strip()
    counsel_address = payload.get("counsel_address", "")
    counsel_email = payload.get("counsel_email", "")
    counsel_phone = payload.get("counsel_phone", "").strip()
    counsel_lines = []
    if counsel_name:
        counsel_lines.append(counsel_name)
    for line in counsel_address.splitlines():
        if line.strip():
            counsel_lines.append(line.strip())
    for line in counsel_email.splitlines():
        if line.strip():
            counsel_lines.append(line.strip())
    if counsel_phone:
        counsel_lines.append(f"Mob: {counsel_phone}")

    case_info = {
        "cause_lines": cause_lines,
        "matter_heading": matter_heading,
        "parties": parties,
        "filing_block": {
            "filed_on": payload.get("filed_date", ""),
            "place": payload.get("filed_place", ""),
            "through_heading": "THROUGH",
            "counsel_lines": counsel_lines,
        },
    }

    sig_path = None
    for ext in (".png", ".jpg", ".jpeg"):
        cand = os.path.join(d, "signature" + ext)
        if os.path.exists(cand):
            sig_path = cand
            break

    out_dir = os.path.join(d, "output")
    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)

    result = build_paper_book(
        case_info=case_info, manifest=manifest, forum=forum,
        out_dir=out_dir, signature_path=sig_path,
        continuous_numbering=continuous,
    )

    qc_text = open(result["qc_report"]).read()
    return {
        "qc_pass": result["qc_pass"], "qc_text": qc_text,
        "volumes": result["volumes"],
        "download_url": f"/api/download/{session_id}",
    }


@app.get("/api/download/{session_id}")
def download(session_id: str):
    zip_path = os.path.join(session_dir(session_id), "output", "paper_book_output.zip")
    if not os.path.exists(zip_path):
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(zip_path, filename="paper_book_output.zip",
                         media_type="application/zip")


@app.get("/api/caps")
def caps():
    return FORUM_CAPS


app.mount("/", StaticFiles(directory=os.path.join(BASE, "static"), html=True), name="static")
