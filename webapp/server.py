#!/usr/bin/env python3
"""
pravachan-web — a small self-hosted web app around pipeline.py.

Paste a Google Drive / YouTube link (or upload audio), pick a set name + title
style, and it queues a job that runs the same pipeline (download → transcribe →
multi-agent Claude typeset → PDF/DOCX) on THIS machine, using the client's own
ANTHROPIC_API_KEY / Claude auth. A dashboard shows live status and download links.

Run:   pravachan-web            # or:  python -m webapp.server
Env:   PRAVACHAN_OUT   output root (default ~/pravachan-output)
       ANTHROPIC_API_KEY  passed through to the Claude build stage
       PRAVACHAN_PORT  (default 8765)
"""
from __future__ import annotations
import json, os, re, sqlite3, subprocess, threading, queue, time, zipfile, io, shutil
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, StreamingResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
PIPELINE = REPO / "pipeline.py"
OUT_ROOT = Path(os.environ.get("PRAVACHAN_OUT", "~/pravachan-output")).expanduser()
DATA = OUT_ROOT / ".pravachan"
DB = DATA / "jobs.db"
OUT_ROOT.mkdir(parents=True, exist_ok=True); DATA.mkdir(parents=True, exist_ok=True)

templates = Jinja2Templates(directory=str(HERE / "templates"))
app = FastAPI(title="pravachan-web")
JOBQ: "queue.Queue[int]" = queue.Queue()

# ── db ────────────────────────────────────────────────────────────────────────
def db():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; return c

def init_db():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS jobs(
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, set_name TEXT, source TEXT,
            source_type TEXT, title_style TEXT, fmt TEXT, columns INT, size INT,
            portrait INT, model TEXT, workers INT, transcriber TEXT, nlm_profile TEXT,
            status TEXT, stage TEXT, out_dir TEXT)""")

def col(row, k, default=None): return row[k] if k in row.keys() else default

# ── worker ─────────────────────────────────────────────────────────────────────
def set_status(jid, **kw):
    with db() as c:
        cols = ", ".join(f"{k}=?" for k in kw)
        c.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*kw.values(), jid))

def logpath(jid): return DATA / f"job_{jid}.log"

def run_job(jid: int):
    with db() as c:
        j = c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    if not j: return
    out_dir = Path(j["out_dir"])
    cmd = ["python3", str(PIPELINE), j["source"], "--out", str(out_dir),
           "--source-type", j["source_type"], "--title-style", j["title_style"],
           "--format", j["fmt"], "--columns", str(j["columns"]), "--size", str(j["size"]),
           "--model", j["model"], "--workers", str(j["workers"]),
           "--transcriber", col(j, "transcriber", "notebooklm") or "notebooklm"]
    prof = col(j, "nlm_profile")
    if prof: cmd += ["--nlm-profile", prof]
    if j["portrait"]: cmd.append("--portrait")
    set_status(jid, status="running", stage="starting")
    with logpath(jid).open("w", encoding="utf-8") as lf:
        lf.write(f"$ {' '.join(cmd)}\n\n"); lf.flush()
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, cwd=str(REPO), env={**os.environ})
            for line in p.stdout:                       # stream stage progress into the log
                lf.write(line); lf.flush()
                m = re.search(r"(downloading|transcribing|building|compressing)", line, re.I)
                if m: set_status(jid, stage=m.group(1).lower())
            rc = p.wait()
        except Exception as e:
            lf.write(f"\n[server] job crashed: {e!r}\n"); rc = 1
    set_status(jid, status="done" if rc == 0 else "error", stage="finished" if rc == 0 else "error")

def worker_loop():
    while True:
        jid = JOBQ.get()
        try: run_job(jid)
        except Exception as e: print("[worker]", e)
        finally: JOBQ.task_done()

# ── helpers ─────────────────────────────────────────────────────────────────────
SAFE = re.compile(r"[^A-Za-z0-9_.-]+")
def slug(s): return SAFE.sub("_", s.strip()).strip("_") or "set"

def outputs(out_dir: Path):
    pdfs = sorted((out_dir / "pdfs").rglob("*.pdf")) if (out_dir / "pdfs").exists() else []
    docx = sorted((out_dir / "pdfs").rglob("*.docx")) if (out_dir / "pdfs").exists() else []
    return pdfs, docx

# ── routes ───────────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    with db() as c:
        jobs = [dict(r) for r in c.execute("SELECT * FROM jobs ORDER BY id DESC").fetchall()]
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    nlm_prof = os.environ.get("PRAVACHAN_NLM_PROFILE", os.path.expanduser("~/.pravachan-notebooklm-firefox"))
    nlm_ready = Path(nlm_prof).exists()
    return templates.TemplateResponse(request, "index.html",
        {"jobs": jobs, "title_styles": ["topic","numbered","granth","day","auto"],
         "has_key": has_key, "nlm_ready": nlm_ready, "nlm_profile": nlm_prof})

@app.post("/jobs")
async def create_job(
    set_name: str = Form(...), source: str = Form(""), source_type: str = Form("auto"),
    title_style: str = Form("auto"), fmt: str = Form("both"), columns: int = Form(2),
    size: int = Form(18), portrait: str = Form(""), model: str = Form("claude-opus-5"),
    workers: int = Form(4), transcriber: str = Form("notebooklm"), nlm_profile: str = Form(""),
    upload: UploadFile | None = File(None)):
    sname = slug(set_name); out_dir = OUT_ROOT / sname
    src = source.strip()
    if upload is not None and upload.filename:            # uploaded audio → local source
        adir = out_dir / "audio"; adir.mkdir(parents=True, exist_ok=True)
        dest = adir / Path(upload.filename).name
        with dest.open("wb") as f: shutil.copyfileobj(upload.file, f)
        src = str(adir); source_type = "local"
    if not src:
        raise HTTPException(400, "provide a Drive/YouTube URL or upload a file")
    with db() as c:
        cur = c.execute("""INSERT INTO jobs(ts,set_name,source,source_type,title_style,fmt,columns,
            size,portrait,model,workers,transcriber,nlm_profile,status,stage,out_dir)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (datetime.now().isoformat(timespec="seconds"), sname, src, source_type, title_style,
             fmt, columns, size, 1 if portrait else 0, model, workers, transcriber,
             nlm_profile.strip(), "queued", "queued", str(out_dir)))
        jid = cur.lastrowid
    JOBQ.put(jid)
    return RedirectResponse(f"/#job-{jid}", status_code=303)

@app.get("/api/jobs")
def api_jobs():
    with db() as c:
        rows = [dict(r) for r in c.execute("SELECT id,set_name,status,stage,source_type,title_style FROM jobs ORDER BY id DESC").fetchall()]
    for r in rows:
        p, d = outputs(Path(OUT_ROOT / r["set_name"]))
        r["pdfs"], r["docx"] = len(p), len(d)
    return JSONResponse(rows)

@app.get("/api/jobs/{jid}/log", response_class=HTMLResponse)
def api_log(jid: int):
    import html as _html
    lp = logpath(jid)
    text = lp.read_text(encoding="utf-8") if lp.exists() else "(no log yet)"
    return HTMLResponse(f"<pre>{_html.escape(text)}</pre>")

@app.get("/jobs/{jid}/files")
def job_files(jid: int):
    with db() as c:
        j = c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    if not j: raise HTTPException(404)
    pdfs, docx = outputs(Path(j["out_dir"]))
    root = Path(j["out_dir"])
    return JSONResponse({
        "pdfs": [str(p.relative_to(root)) for p in pdfs],
        "docx": [str(p.relative_to(root)) for p in docx]})

@app.get("/download/{jid}")
def download(jid: int, rel: str):
    with db() as c:
        j = c.execute("SELECT out_dir FROM jobs WHERE id=?", (jid,)).fetchone()
    if not j: raise HTTPException(404)
    root = Path(j["out_dir"]).resolve()
    target = (root / rel).resolve()
    if not str(target).startswith(str(root)) or not target.exists():   # path-traversal guard
        raise HTTPException(404)
    return FileResponse(str(target), filename=target.name)

@app.get("/zip/{jid}")
def zip_pdfs(jid: int):
    with db() as c:
        j = c.execute("SELECT out_dir,set_name FROM jobs WHERE id=?", (jid,)).fetchone()
    if not j: raise HTTPException(404)
    pdfs, docx = outputs(Path(j["out_dir"]))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in pdfs + docx: z.write(f, arcname=f.name)
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{j["set_name"]}.zip"'})

def main():
    import uvicorn
    init_db()
    threading.Thread(target=worker_loop, daemon=True).start()
    # requeue anything left 'queued' from a previous run
    with db() as c:
        for r in c.execute("SELECT id FROM jobs WHERE status IN('queued','running')").fetchall():
            set_status(r["id"], status="queued", stage="queued"); JOBQ.put(r["id"])
    port = int(os.environ.get("PRAVACHAN_PORT", "8765"))
    print(f"pravachan-web → http://127.0.0.1:{port}   (output root: {OUT_ROOT})")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")

if __name__ == "__main__":
    main()
