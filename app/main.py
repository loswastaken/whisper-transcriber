"""HTTP routes. LAN-only by design: no auth, no CORS, no cookies."""

import logging
import os
import shutil
from contextlib import asynccontextmanager
from typing import Optional
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from python_multipart.multipart import MultipartParser, parse_options_header
from starlette.requests import ClientDisconnect

from . import urlcheck
from .config import settings
from .filenames import (content_disposition, download_filename, is_valid_job_id,
                        sanitize_display_name, upload_extension)
from .jobs import AlreadyFinished, Conflict, JobStore
from .transcribe import make_transcriber

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("transcriber")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
CHUNK = 1024 * 1024
MULTIPART_OVERHEAD = 64 * 1024

store = JobStore(settings, make_transcriber(settings))


@asynccontextmanager
async def lifespan(app: FastAPI):
    store.recover()
    store.start()
    yield
    store.stop()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    # Browsers mark cross-site requests; refuse them for writes so another
    # website cannot submit jobs through a user's browser.
    if request.method not in ("GET", "HEAD", "OPTIONS") and \
            request.headers.get("sec-fetch-site") == "cross-site":
        return JSONResponse({"detail": "cross-site request refused"}, status_code=403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; object-src 'none'; "
        "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    )
    return response


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse({"detail": "internal error"}, status_code=500)


def _job_or_404(job_id: str) -> dict:
    if not is_valid_job_id(job_id):
        raise HTTPException(404, "job not found")
    job = store.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return job


class UrlIn(BaseModel):
    url: str


@app.post("/api/jobs/url", status_code=202)
def create_url_job(body: UrlIn):
    try:
        url = urlcheck.check_url(body.url)
    except urlcheck.UrlError as exc:
        raise HTTPException(400, str(exc))
    job_id = store.new_id()
    host = urlsplit(url).hostname or "link"
    store.create(job_id, "url", sanitize_display_name(host, "link"), url=url)
    return {"id": job_id}


class _UploadSink:
    """Callbacks for python-multipart's streaming parser."""

    def __init__(self, job_dir: str, limit: int):
        self.job_dir = job_dir
        self.limit = limit
        self.size = 0
        self.file = None
        self.done = False
        self.stored_name: Optional[str] = None
        self.original_name: Optional[str] = None
        self.error: Optional[HTTPException] = None
        self._buf = bytearray()
        self._field = b""
        self._value = b""
        self._headers = {}
        self._active = False

    def callbacks(self):
        return {
            "on_part_begin": self._part_begin,
            "on_header_field": self._header_field,
            "on_header_value": self._header_value,
            "on_header_end": self._header_end,
            "on_headers_finished": self._headers_finished,
            "on_part_data": self._part_data,
            "on_part_end": self._part_end,
        }

    def _part_begin(self):
        self._headers = {}
        self._active = False

    def _header_field(self, data, start, end):
        self._field += data[start:end]

    def _header_value(self, data, start, end):
        self._value += data[start:end]

    def _header_end(self):
        self._headers[self._field.strip().lower()] = self._value.strip()
        self._field, self._value = b"", b""

    def _headers_finished(self):
        _, params = parse_options_header(self._headers.get(b"content-disposition", b""))
        if params.get(b"name") != b"file" or self.file is not None or self.done:
            return
        filename = params.get(b"filename", b"").decode("utf-8", "replace")
        ext = upload_extension(filename)
        if ext is None:
            raise HTTPException(415, "unsupported file type")
        self.original_name = filename
        self.stored_name = "upload" + ext
        self.file = open(os.path.join(self.job_dir, self.stored_name), "xb")
        self._active = True

    def _part_data(self, data, start, end):
        if not self._active:
            return
        self.size += end - start
        if self.size > self.limit:
            raise HTTPException(413, "file is too large")
        self._buf += data[start:end]
        if len(self._buf) >= CHUNK:
            self.file.write(self._buf)
            self._buf.clear()

    def _part_end(self):
        if self._active:
            self.file.write(self._buf)
            self._buf.clear()
            self.file.close()
            self._active = False
            self.done = True

    def close(self):
        if self.file is not None and not self.file.closed:
            self.file.close()


@app.post("/api/jobs/upload", status_code=202)
async def create_upload_job(request: Request):
    limit = settings.max_upload_bytes
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > limit + MULTIPART_OVERHEAD:
        raise HTTPException(413, "file is too large")
    ctype, params = parse_options_header(request.headers.get("content-type", ""))
    boundary = params.get(b"boundary")
    if ctype != b"multipart/form-data" or not boundary:
        raise HTTPException(400, "expected multipart/form-data")

    job_id = store.new_id()
    job_dir = store.job_dir(job_id)
    os.makedirs(job_dir)
    sink = _UploadSink(job_dir, limit)
    parser = MultipartParser(boundary, sink.callbacks())
    try:
        async for chunk in request.stream():
            parser.write(chunk)
        parser.finalize()
        if not sink.done:
            raise HTTPException(400, "no file in field 'file'")
    except HTTPException:
        sink.close()
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    except ClientDisconnect:
        sink.close()
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(400, "upload interrupted")
    except Exception as exc:
        sink.close()
        shutil.rmtree(job_dir, ignore_errors=True)
        log.warning("upload failed: %s", type(exc).__name__)
        raise HTTPException(400, "upload failed")
    finally:
        sink.close()
    name = sanitize_display_name(sink.original_name, "upload")
    await run_in_threadpool(store.create, job_id, "upload", name, None, sink.stored_name)
    return {"id": job_id}


@app.get("/api/jobs")
def list_jobs():
    return store.list()


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = _job_or_404(job_id)
    job["transcript"] = None
    if job["status"] == "done":
        path = os.path.join(store.job_dir(job_id), "transcript.txt")
        try:
            with open(path, "r", encoding="utf-8") as f:
                job["transcript"] = f.read()
        except OSError:
            pass
    return job


def _download(job_id: str, ext: str, media_type: str):
    job = _job_or_404(job_id)
    path = os.path.join(store.job_dir(job_id), "transcript" + ext)
    if job["status"] != "done" or not os.path.isfile(path) or os.path.islink(path):
        raise HTTPException(404, "transcript not available")
    filename = download_filename(job["name"], ext)
    return FileResponse(path, media_type=media_type,
                        headers={"Content-Disposition": content_disposition(filename)})


@app.get("/api/jobs/{job_id}/transcript.txt")
def download_txt(job_id: str):
    return _download(job_id, ".txt", "text/plain; charset=utf-8")


@app.get("/api/jobs/{job_id}/transcript.srt")
def download_srt(job_id: str):
    return _download(job_id, ".srt", "application/x-subrip; charset=utf-8")


@app.delete("/api/jobs/{job_id}", status_code=204)
def delete_job(job_id: str):
    if not is_valid_job_id(job_id):
        raise HTTPException(404, "job not found")
    try:
        if not store.delete(job_id):
            raise HTTPException(404, "job not found")
    except Conflict:
        raise HTTPException(409, "the running job cannot be deleted")
    return None


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    if not is_valid_job_id(job_id):
        raise HTTPException(404, "job not found")
    try:
        status = store.cancel(job_id)
    except AlreadyFinished:
        raise HTTPException(409, "job already finished")
    if status is None:
        raise HTTPException(404, "job not found")
    return JSONResponse({"status": status}, status_code=202 if status == "cancelling" else 200)


@app.get("/healthz")
def healthz():
    return store.health()


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
