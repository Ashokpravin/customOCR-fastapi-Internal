import os
import re
import secrets
import logging
import threading
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, List

from fastapi import FastAPI, UploadFile, File, HTTPException, Depends, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from dotenv import load_dotenv
import uvicorn
import aiofiles
import requests

# Document parsing
import PyPDF2
from docx import Document
from pptx import Presentation

load_dotenv()

# -----------------------------------------------------------------------------
# Proxy Root Path Detection
# -----------------------------------------------------------------------------
def get_root_path() -> str:
    route = os.getenv("ROUTE", "").strip()
    if route:
        if not route.startswith("/"):
            route = "/" + route
        if route.endswith("/"):
            route = route[:-1]
        return route
    return os.getenv("ROOT_PATH", "")

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "./outputs")).resolve()
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

MAX_UPLOAD_SIZE_MB = int(os.getenv("MAX_UPLOAD_SIZE_MB", "100"))
MAX_UPLOAD_SIZE_BYTES = MAX_UPLOAD_SIZE_MB * 1024 * 1024

ALLOWED_EXTENSIONS = {".pdf", ".docx", ".pptx"}

VALID_TOKENS = set()
for i in range(1, 11):
    token = os.getenv(f"AUTH_TOKEN_{i}", "").strip()
    if token:
        VALID_TOKENS.add(token)
legacy = os.getenv("API_AUTH_TOKEN", "").strip()
if legacy:
    VALID_TOKENS.add(legacy)

MODEL_API_URL = os.getenv(
    "MODEL_API_URL",
    "http://llm-api.kt-application.svc.cluster.local:8000/model/api/completions"
)
MODEL_ID = os.getenv("MODEL_ID", "69d0a234e26c20b56b5fbe2d")
MODEL_API_KEY = os.getenv("MODEL_API_KEY", "")

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# FastAPI App
# -----------------------------------------------------------------------------
app = FastAPI(
    title="Document to Markdown Converter",
    description="Upload PDF, DOCX, PPTX → get Markdown (powered by GPT-4o). Asynchronous job processing with status tracking.",
    version="2.0.0",
    root_path=get_root_path()
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

security = HTTPBearer(auto_error=False)

# -----------------------------------------------------------------------------
# Auth
# -----------------------------------------------------------------------------
def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if not VALID_TOKENS:
        raise HTTPException(503, "Server authentication not configured")
    if not credentials:
        raise HTTPException(401, "Bearer token required")
    token = credentials.credentials
    for valid in VALID_TOKENS:
        if secrets.compare_digest(token, valid):
            return token
    raise HTTPException(401, "Invalid token")

# -----------------------------------------------------------------------------
# Job Management (in-memory store with thread-safe locking)
# -----------------------------------------------------------------------------
class JobStatus:
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"

class Job(BaseModel):
    id: str
    filename: str
    status: str = JobStatus.PENDING
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    result_filename: Optional[str] = None
    download_url: Optional[str] = None
    error_message: Optional[str] = None

class JobStore:
    def __init__(self):
        self._jobs: Dict[str, Job] = {}
        self._lock = threading.Lock()

    def create(self, filename: str) -> Job:
        job_id = secrets.token_hex(16)  # 32-character hex string
        job = Job(
            id=job_id,
            filename=filename,
            created_at=datetime.utcnow()
        )
        with self._lock:
            self._jobs[job_id] = job
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def update(self, job_id: str, **kwargs):
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                for key, value in kwargs.items():
                    setattr(job, key, value)

    def list_jobs(self) -> List[Job]:
        with self._lock:
            return list(self._jobs.values())

job_store = JobStore()

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def sanitize_filename(filename: str) -> str:
    name = Path(filename).stem
    ext = Path(filename).suffix
    name = re.sub(r'[<>:"/\\|?*]', '_', name)[:200]
    if not name:
        name = f"upload_{secrets.token_hex(4)}"
    return f"{name}{ext}"

def extract_text_from_pdf(path: Path) -> str:
    text = ""
    with open(path, "rb") as f:
        reader = PyPDF2.PdfReader(f)
        for page in reader.pages:
            text += page.extract_text() or ""
    return text

def extract_text_from_docx(path: Path) -> str:
    doc = Document(path)
    return "\n".join(p.text for p in doc.paragraphs)

def extract_text_from_pptx(path: Path) -> str:
    prs = Presentation(path)
    lines = []
    for slide in prs.slides:
        for shape in slide.shapes:
            if hasattr(shape, "text"):
                lines.append(shape.text)
    return "\n".join(lines)

def extract_text(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".pdf":
        return extract_text_from_pdf(path)
    elif ext == ".docx":
        return extract_text_from_docx(path)
    elif ext == ".pptx":
        return extract_text_from_pptx(path)
    else:
        raise ValueError(f"Unsupported file type: {ext}")

def call_model_api(raw_text: str, use_llm: bool = True) -> str:
    if not use_llm:
        return f"```text\n{raw_text}\n```"

    headers = {"Content-Type": "application/json"}

    if os.getenv("API_KEY"):
        headers["Authorization"] = f"Bearer {os.getenv('API_KEY')}"

    # Clean text
    clean = re.sub(r"\s+", " ", raw_text).strip()[:15000]

    # Build prompt
    prompt = f"""
Convert the following document into a clean, well-structured Markdown document.

Use headings, bullet points, and proper formatting.

DOCUMENT:
{clean}
"""

    payload = {
        "model_id": MODEL_ID,
        "data": {
            "query": prompt,
            "input": clean
        },
        "temperature": 0.2,
        "max_tokens": 4000
    }

    try:
        resp = requests.post(
            MODEL_API_URL,
            json=payload,
            headers=headers,
            timeout=90
        )

        print("STATUS:", resp.status_code)
        print("RESPONSE:", resp.text[:1000])

        resp.raise_for_status()
        data = resp.json()

        if isinstance(data, dict):
            output = (
                data.get("output")
                or data.get("response")
                or data.get("text")
            )

            if not output:
                raise Exception(f"Empty response: {data}")

            if "please provide the text" in output.lower():
                raise Exception("LLM did not receive document")

            return output

        return str(data)

    except Exception as e:
        raise Exception(f"LLM API Error: {resp.text}")

# -----------------------------------------------------------------------------
# Background Processing Function
# -----------------------------------------------------------------------------
def process_document_background(job_id: str, temp_path: Path, original_filename: str, use_llm: bool):
    try:
        job_store.update(job_id, status=JobStatus.PROCESSING, started_at=datetime.utcnow())
        logger.info(f"Job {job_id}: Started processing {original_filename}")

        # Extract text
        raw_text = extract_text(temp_path)

        # Call LLM
        markdown = call_model_api(raw_text, use_llm)

        # Save Markdown file
        md_filename = f"{Path(original_filename).stem}.md"
        md_path = OUTPUT_DIR / md_filename
        md_path.write_text(markdown, encoding="utf-8")

        # Build download URL
        download_url = f"{get_root_path()}/download/{md_filename}"

        # Update job as completed
        job_store.update(
            job_id,
            status=JobStatus.COMPLETED,
            completed_at=datetime.utcnow(),
            result_filename=md_filename,
            download_url=download_url
        )
        logger.info(f"Job {job_id}: Completed successfully. Output: {md_filename}")

    except Exception as e:
        logger.exception(f"Job {job_id}: Processing failed")
        job_store.update(
            job_id,
            status=JobStatus.FAILED,
            completed_at=datetime.utcnow(),
            error_message=str(e)
        )
    finally:
        # Clean up temporary file
        if temp_path.exists():
            temp_path.unlink()

# -----------------------------------------------------------------------------
# API Models
# -----------------------------------------------------------------------------
class ProcessResponse(BaseModel):
    job_id: str
    status_url: str
    message: str

class JobStatusResponse(BaseModel):
    job: Job

class JobListResponse(BaseModel):
    jobs: List[Job]
    count: int

# -----------------------------------------------------------------------------
# Endpoints
# -----------------------------------------------------------------------------
@app.get("/")
def root():
    return {
        "service": "Document to Markdown Converter API",
        "docs": f"{get_root_path()}/docs",
        "health": f"{get_root_path()}/health",
        "jobs": f"{get_root_path()}/jobs"
    }

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/process", response_model=ProcessResponse)
async def process_document(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    use_llm: bool = True,
    token: str = Depends(verify_token)
):
    """
    Upload a document for conversion to Markdown.
    Returns immediately with a job_id. Use /job/{job_id} to check status.
    """
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"Unsupported file type: {ext}")

    safe_name = sanitize_filename(file.filename)
    temp_path = OUTPUT_DIR / f"temp_{secrets.token_hex(8)}_{safe_name}"

    total_size = 0
    try:
        async with aiofiles.open(temp_path, "wb") as out:
            while chunk := await file.read(1024 * 1024):
                total_size += len(chunk)
                if total_size > MAX_UPLOAD_SIZE_BYTES:
                    raise HTTPException(413, f"File exceeds {MAX_UPLOAD_SIZE_MB}MB")
                await out.write(chunk)
    except Exception as e:
        if temp_path.exists():
            temp_path.unlink()
        raise HTTPException(400, f"Upload failed: {e}")
    finally:
        await file.close()

    if total_size == 0:
        temp_path.unlink(missing_ok=True)
        raise HTTPException(400, "Empty file")

    # Create job entry
    job = job_store.create(file.filename)

    # Schedule background processing
    background_tasks.add_task(
        process_document_background,
        job.id,
        temp_path,
        file.filename,
        use_llm
    )

    logger.info(f"Job {job.id} created for {file.filename}")

    return ProcessResponse(
        job_id=job.id,
        status_url=f"{get_root_path()}/job/{job.id}",
        message="Document queued for processing. Check status at the provided URL."
    )

@app.get("/job/{job_id}", response_model=JobStatusResponse)
async def get_job_status(job_id: str, token: str = Depends(verify_token)):
    """
    Retrieve the current status and details of a processing job.
    """
    job = job_store.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return JobStatusResponse(job=job)

@app.get("/jobs", response_model=JobListResponse)
async def list_jobs(token: str = Depends(verify_token)):
    """
    List all jobs (both completed and in-progress).
    """
    jobs = job_store.list_jobs()
    # Sort by creation time descending (newest first)
    jobs.sort(key=lambda j: j.created_at, reverse=True)
    return JobListResponse(jobs=jobs, count=len(jobs))

@app.get("/job/{job_id}/download")
async def download_by_job_id(job_id: str, token: str = Depends(verify_token)):
    """
    Download the generated Markdown file for a completed job.
    """
    job = job_store.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job.status != JobStatus.COMPLETED:
        raise HTTPException(409, f"Job is not completed (status: {job.status})")
    if not job.result_filename:
        raise HTTPException(500, "Job completed but no result file recorded")

    file_path = OUTPUT_DIR / job.result_filename
    if not file_path.exists():
        raise HTTPException(404, "Result file missing on server")

    return FileResponse(
        path=file_path,
        media_type="text/markdown",
        filename=job.result_filename
    )

@app.get("/download/{filename}")
async def download_file(filename: str, token: str = Depends(verify_token)):
    """
    Direct download by filename (legacy endpoint, still supported).
    """
    safe_name = sanitize_filename(filename)
    file_path = OUTPUT_DIR / safe_name
    if not file_path.exists():
        raise HTTPException(404, "File not found")
    return FileResponse(
        path=file_path,
        media_type="text/markdown",
        filename=safe_name
    )

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8050"))
    uvicorn.run(app, host="0.0.0.0", port=port)