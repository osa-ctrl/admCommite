"""Small web front for ts_download.py.

Each download runs the existing CLI script as a subprocess; its stderr is
parsed for progress. Finished files are offered for download and removed
after JOB_TTL_MINUTES. Everything except /api/login needs the shared password.

Environment:
    APP_PASSWORD        shared password (required)
    SESSION_SECRET      key for signing the login cookie (default: derived from APP_PASSWORD)
    DATA_DIR            where job folders live (default: <tmp>/ts-web)
    MAX_ACTIVE_JOBS     downloads allowed at the same time (default: 2)
    JOB_TTL_MINUTES     how long a finished file is kept (default: 60)
    ALLOW_PRIVATE_URLS  set to 1 to allow URLs that resolve to private/local addresses
"""

import asyncio
import contextlib
import hashlib
import hmac
import ipaddress
import os
import re
import secrets
import shutil
import signal
import socket
import sys
import tempfile
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "ts_download.py"
STATIC = ROOT / "frontend" / "dist"

PASSWORD = os.environ.get("APP_PASSWORD", "")
if not PASSWORD:
    sys.exit("error: set APP_PASSWORD to the password people will log in with")
SECRET = os.environ.get("SESSION_SECRET") or hashlib.sha256(b"ts-web:" + PASSWORD.encode()).hexdigest()
DATA_DIR = Path(os.environ.get("DATA_DIR") or Path(tempfile.gettempdir()) / "ts-web")
MAX_ACTIVE = int(os.environ.get("MAX_ACTIVE_JOBS", "2"))
TTL = int(os.environ.get("JOB_TTL_MINUTES", "60")) * 60
ALLOW_PRIVATE = os.environ.get("ALLOW_PRIVATE_URLS") == "1"

COOKIE = "ts_session"
COOKIE_MAX_AGE = 30 * 24 * 3600
LOG_LINES = 40

PROGRESS_OF = re.compile(r"^(\d+)/(\d+) segments$")
PROGRESS_COUNT = re.compile(r"^(\d+) segments$")
PLAYLIST_TOTAL = re.compile(r"^playlist has (\d+) segment")
JOINING = re.compile(r"^downloaded (\d+) segment")


@dataclass
class Job:
    id: str
    url: str
    filename: str
    dir: Path
    status: str = "queued"  # queued, downloading, joining, done, error, cancelled
    done: int = 0
    total: Optional[int] = None
    error: Optional[str] = None
    log: list = field(default_factory=list)
    size: Optional[int] = None
    created: float = field(default_factory=time.time)
    finished: Optional[float] = None
    process: Optional[asyncio.subprocess.Process] = None

    @property
    def output(self):
        return self.dir / "output.mp4"

    @property
    def active(self):
        return self.status in ("queued", "downloading", "joining")

    def public(self):
        return {
            "id": self.id, "url": self.url, "filename": self.filename, "status": self.status,
            "done": self.done, "total": self.total, "error": self.error, "log": self.log[-LOG_LINES:],
            "size": self.size, "created": self.created, "finished": self.finished,
            "expires": self.finished + TTL if self.finished else None,
        }


jobs: dict = {}


@contextlib.asynccontextmanager
async def lifespan(app):
    # Jobs live in memory, so folders left by a previous run belong to nobody.
    shutil.rmtree(DATA_DIR, ignore_errors=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cleaner = asyncio.create_task(cleanup_loop())
    yield
    cleaner.cancel()


app = FastAPI(title="TS downloader", docs_url=None, redoc_url=None, lifespan=lifespan)


# --- auth -------------------------------------------------------------------

def session_token():
    return hmac.new(SECRET.encode(), b"ts-web-session", hashlib.sha256).hexdigest()


def require_login(request: Request):
    if not hmac.compare_digest(request.cookies.get(COOKIE, ""), session_token()):
        raise HTTPException(401, "login required")


class Login(BaseModel):
    password: str


@app.post("/api/login")
async def login(body: Login, request: Request, response: Response):
    if not hmac.compare_digest(body.password.encode(), PASSWORD.encode()):
        await asyncio.sleep(1)  # slow down guessing
        raise HTTPException(401, "wrong password")
    https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(COOKIE, session_token(), max_age=COOKIE_MAX_AGE, httponly=True,
                        samesite="strict", secure=https)
    return {"ok": True}


@app.post("/api/logout")
async def logout(response: Response):
    response.delete_cookie(COOKIE)
    return {"ok": True}


@app.get("/api/me", dependencies=[Depends(require_login)])
async def me():
    return {"ok": True}


# --- jobs -------------------------------------------------------------------

class NewJob(BaseModel):
    url: str = Field(max_length=4000)
    filename: str = Field(default="video.mp4", max_length=200)
    headers: str = Field(default="", max_length=8000)
    workers: int = Field(default=8, ge=1, le=32)
    variant: Optional[int] = Field(default=None, ge=1, le=50)


def check_url(url):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise HTTPException(400, "URL must start with http:// or https://")
    if ALLOW_PRIVATE:
        return
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or 443)
    except socket.gaierror:
        raise HTTPException(400, f"cannot resolve host {parts.hostname}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise HTTPException(400, "URL points to a private or local address")


def safe_filename(name):
    name = re.sub(r"[^\w.\- ()\[\]]+", "_", name).strip(" .") or "video"
    return name if name.lower().endswith(".mp4") else name + ".mp4"


def header_args(text):
    args = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        name, sep, value = line.partition(":")
        if not sep or not name.strip():
            raise HTTPException(400, f"bad header line {line!r}, expected 'Name: value'")
        args += ["-H", f"{name.strip()}: {value.strip()}"]
    return args


@app.post("/api/jobs", dependencies=[Depends(require_login)])
async def create_job(body: NewJob):
    url = body.url.strip()
    check_url(url)
    extra = header_args(body.headers)
    if sum(job.active for job in jobs.values()) >= MAX_ACTIVE:
        raise HTTPException(429, f"already {MAX_ACTIVE} download(s) running, wait for one to finish")

    job_id = secrets.token_urlsafe(8)
    job_dir = DATA_DIR / job_id
    job_dir.mkdir(parents=True)
    job = Job(id=job_id, url=url, filename=safe_filename(body.filename), dir=job_dir)
    jobs[job_id] = job

    cmd = [sys.executable, str(SCRIPT), url, "-o", str(job.output), "-j", str(body.workers), *extra]
    if body.variant:
        cmd += ["--variant", str(body.variant)]
    asyncio.create_task(run_job(job, cmd))
    return job.public()


async def run_job(job, cmd):
    job.status = "downloading"
    try:
        job.process = await asyncio.create_subprocess_exec(
            *cmd, cwd=job.dir, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "TMPDIR": str(job.dir)}, start_new_session=True)
        buffer = ""
        while chunk := await job.process.stderr.read(4096):
            buffer += chunk.decode("utf-8", "replace")
            *lines, buffer = re.split(r"[\r\n]", buffer)
            for line in lines:
                handle_line(job, line.strip())
        handle_line(job, buffer.strip())
        code = await job.process.wait()
        if job.status == "cancelled":
            return
        if code == 0 and job.output.exists():
            job.status, job.size = "done", job.output.stat().st_size
        else:
            job.status = "error"
            job.error = job.error or f"script exited with code {code}"
    except Exception as err:  # keep the server alive whatever happens in one job
        job.status, job.error = "error", str(err)
    finally:
        job.finished = time.time()
        job.process = None
        if job.status != "done":
            remove_files(job)


def handle_line(job, line):
    if not line:
        return
    line = line.replace(str(job.dir) + os.sep, "")  # keep server paths out of the page
    if m := PROGRESS_OF.match(line):
        job.done, job.total = int(m[1]), int(m[2])
        return  # progress lines would flood the log
    if m := PROGRESS_COUNT.match(line):
        job.done = int(m[1])
        return
    if m := PLAYLIST_TOTAL.match(line):
        job.total = int(m[1])
    elif m := JOINING.match(line):
        job.done, job.status = int(m[1]), "joining"
    elif line.startswith("error:"):
        job.error = line[len("error:"):].strip()
    job.log.append(line)
    del job.log[:-LOG_LINES]


def remove_files(job):
    shutil.rmtree(job.dir, ignore_errors=True)


def get_job(job_id):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "no such job (it may have expired)")
    return job


@app.get("/api/jobs", dependencies=[Depends(require_login)])
async def list_jobs():
    return [job.public() for job in sorted(jobs.values(), key=lambda j: j.created, reverse=True)]


@app.get("/api/jobs/{job_id}", dependencies=[Depends(require_login)])
async def job_status(job_id: str):
    return get_job(job_id).public()


@app.get("/api/jobs/{job_id}/file", dependencies=[Depends(require_login)])
async def job_file(job_id: str):
    job = get_job(job_id)
    if job.status != "done" or not job.output.exists():
        raise HTTPException(409, "file is not ready")
    return FileResponse(job.output, media_type="video/mp4", filename=job.filename)


@app.delete("/api/jobs/{job_id}", dependencies=[Depends(require_login)])
async def delete_job(job_id: str):
    job = get_job(job_id)
    if job.process and job.process.returncode is None:
        job.status = "cancelled"
        with contextlib.suppress(ProcessLookupError):
            os.killpg(job.process.pid, signal.SIGKILL)  # the script and its ffmpeg
    else:
        remove_files(job)
        del jobs[job_id]
    return {"ok": True}


# --- housekeeping -----------------------------------------------------------

async def cleanup_loop():
    while True:
        now = time.time()
        for job in list(jobs.values()):
            if job.finished and now - job.finished > TTL:
                remove_files(job)
                jobs.pop(job.id, None)
        await asyncio.sleep(60)


@app.get("/api/health")
async def health():
    return {"ok": True, "ffmpeg": bool(shutil.which("ffmpeg"))}


if STATIC.is_dir():
    app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
