#!/usr/bin/env python
"""
Document to Markdown Converter API
Supports PDF, DOCX, PPTX with async job processing and optional LLM formatting.
"""

import os
import uuid
import shutil
import tempfile
import subprocess
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any
from enum import Enum

from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn
from dotenv import load_dotenv
import requests

# Document parsing
import PyPDF2
from docx import Document
from pptx import Presentation

load_dotenv()

# ============================================================================
# CONFIGURATION
# ============================================================================

# Model API settings
MODEL_API_URL = os.getenv("MODEL_API_URL", "http://llm-api.kt-application.svc.cluster.local:8000/model/api/completions")
MODEL_ID = os.getenv("MODEL_ID", "69d0a234e26c20b56b5fbe2d")
API_KEY = os.getenv("API_KEY", "")  # if needed

# Storage for jobs (in production, use Redis or a database)
jobs: Dict[str, Dict[str, Any]] = {}

# Temp directory
TEMP_DIR = Path("/tmp/document_converter")
TEMP_DIR.mkdir(exist_ok=True)

# ============================================================================
# FASTAPI APP
# ============================================================================

app = FastAPI(
    title="Document to Markdown Converter",
    description="""
    Convert PDF, DOCX, and PPTX documents to Markdown with optional AI-powered formatting.
    
    **Workflow:**
    1. POST `/process` – Upload file and start conversion (returns `job_id`)
    2. GET `/status/{job_id}` – Check processing status
    3. GET `/download/{job_id}` – Download the generated Markdown file
    
    **Features:**
    - 📄 PDF, DOCX, PPTX support
    - 🤖 Optional GPT-4o formatting for clean Markdown
    - ⚡ Asynchronous background processing
    - 📊 Job tracking with detailed status
    """,
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================================
# MODELS
# ============================================================================

class JobStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"

class ProcessResponse(BaseModel):
    job_id: str
    status: JobStatus
    message: str
    status_url: str
    download_url: Optional[str] = None

class StatusResponse(BaseModel):
    job_id: str
    status: JobStatus
    progress: float
    created_at: str
    updated_at: Optional[str] = None
    filename: str
    error: Optional[str] = None

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def extract_text_from_pdf(file_path: str) -> str:
    text = ""
    with open(file_path, "rb") as f:
        reader = PyPDF2.PdfReader(f)
        for page in reader.pages:
            text += page.extract_text() or ""
    return text

def extract_text_from_docx(file_path: str) -> str:
    doc = Document(file_path)
    return "\n".join([para.text for para in doc.paragraphs])

def extract_text_from_pptx(file_path: str) -> str:
    prs = Presentation(file_path)
    text = ""
    for slide in prs.slides:
        for shape in slide.shapes:
            if hasattr(shape, "text"):
                text += shape.text + "\n"
    return text

def extract_text(file_path: str, file_extension: str) -> str:
    ext = file_extension.lower()
    if ext == ".pdf":
        return extract_text_from_pdf(file_path)
    elif ext == ".docx":
        return extract_text_from_docx(file_path)
    elif ext == ".pptx":
        return extract_text_from_pptx(file_path)
    else:
        raise ValueError(f"Unsupported file type: {ext}")

def call_model_api(raw_text: str) -> str:
    """Call internal GPT-4o to structure text as Markdown."""
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"

    payload = {
        "model": MODEL_ID,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a document formatting assistant. "
                    "Convert the provided raw text into clean, well-structured Markdown. "
                    "Preserve all information, improve readability, and use appropriate headings, lists, and formatting."
                )
            },
            {"role": "user", "content": f"Document text:\n\n{raw_text}"}
        ],
        "temperature": 0.2,
        "max_tokens": 4000
    }

    response = requests.post(MODEL_API_URL, json=payload, headers=headers, timeout=60)
    response.raise_for_status()
    data = response.json()
    return data["choices"][0]["message"]["content"]

def process_document_job(job_id: str, file_path: str, original_filename: str, use_llm: bool):
    """Background task to process document."""
    try:
        # Update status to processing
        jobs[job_id]["status"] = JobStatus.PROCESSING
        jobs[job_id]["updated_at"] = datetime.utcnow().isoformat()

        # Extract text
        file_ext = Path(original_filename).suffix
        raw_text = extract_text(file_path, file_ext)

        # Generate Markdown
        if use_llm:
            markdown_content = call_model_api(raw_text)
        else:
            markdown_content = f"```text\n{raw_text}\n```"

        # Save Markdown file
        output_filename = Path(original_filename).stem + ".md"
        output_path = TEMP_DIR / job_id / output_filename
        output_path.parent.mkdir(exist_ok=True)
        output_path.write_text(markdown_content, encoding="utf-8")

        # Update job as completed
        jobs[job_id]["status"] = JobStatus.COMPLETED
        jobs[job_id]["progress"] = 100.0
        jobs[job_id]["updated_at"] = datetime.utcnow().isoformat()
        jobs[job_id]["output_file"] = str(output_path)
        jobs[job_id]["download_url"] = f"/download/{job_id}"

    except Exception as e:
        jobs[job_id]["status"] = JobStatus.FAILED
        jobs[job_id]["error"] = str(e)
        jobs[job_id]["updated_at"] = datetime.utcnow().isoformat()
    finally:
        # Clean up uploaded file
        if os.path.exists(file_path):
            os.remove(file_path)

# ============================================================================
# ENDPOINTS
# ============================================================================

@app.get("/")
async def root():
    return {
        "service": "Document to Markdown Converter",
        "version": "1.0.0",
        "endpoints": {
            "docs": "/docs",
            "process": "/process",
            "status": "/status/{job_id}",
            "download": "/download/{job_id}"
        }
    }

@app.post("/process", response_model=ProcessResponse)
async def process_document(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="Document to convert (PDF, DOCX, PPTX)"),
    use_llm: bool = Form(True, description="Use GPT-4o to format output as Markdown")
):
    """
    Upload a document and start conversion.
    
    - **file**: PDF, DOCX, or PPTX file
    - **use_llm**: If true, uses AI to produce clean Markdown; otherwise returns raw text in a code block.
    
    Returns a `job_id` to track progress and eventually download the result.
    """
    # Validate file extension
    allowed = {".pdf", ".docx", ".pptx"}
    ext = Path(file.filename).suffix.lower()
    if ext not in allowed:
        raise HTTPException(400, f"Only {', '.join(allowed)} files are supported.")

    # Create job
    job_id = str(uuid.uuid4())
    job_dir = TEMP_DIR / job_id
    job_dir.mkdir(exist_ok=True)

    # Save uploaded file
    temp_path = job_dir / f"input{ext}"
    content = await file.read()
    temp_path.write_bytes(content)

    # Initialize job record
    jobs[job_id] = {
        "job_id": job_id,
        "status": JobStatus.PENDING,
        "progress": 0.0,
        "created_at": datetime.utcnow().isoformat(),
        "updated_at": None,
        "filename": file.filename,
        "use_llm": use_llm,
        "error": None,
    }

    # Start background processing
    background_tasks.add_task(
        process_document_job,
        job_id,
        str(temp_path),
        file.filename,
        use_llm
    )

    base_url = str(app.url_path_for("root")).rstrip("/")
    return ProcessResponse(
        job_id=job_id,
        status=JobStatus.PENDING,
        message="Job queued successfully",
        status_url=f"{base_url}/status/{job_id}",
        download_url=None
    )

@app.get("/status/{job_id}", response_model=StatusResponse)
async def get_job_status(job_id: str):
    """
    Check the status of a conversion job.
    """
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")

    job = jobs[job_id]
    return StatusResponse(
        job_id=job["job_id"],
        status=job["status"],
        progress=job["progress"],
        created_at=job["created_at"],
        updated_at=job.get("updated_at"),
        filename=job["filename"],
        error=job.get("error")
    )

@app.get("/download/{job_id}")
async def download_result(job_id: str):
    """
    Download the generated Markdown file.
    Only available when job status is `completed`.
    """
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")

    job = jobs[job_id]
    if job["status"] != JobStatus.COMPLETED:
        raise HTTPException(400, f"Job not completed (current status: {job['status']})")

    output_path = job.get("output_file")
    if not output_path or not os.path.exists(output_path):
        raise HTTPException(404, "Output file not found")

    return FileResponse(
        path=output_path,
        media_type="text/markdown",
        filename=Path(job["filename"]).stem + ".md"
    )

@app.get("/jobs")
async def list_jobs():
    """List all jobs (for debugging)."""
    return {"jobs": list(jobs.values())}

# ============================================================================
# CLEANUP (Optional – can be run periodically)
# ============================================================================

@app.on_event("startup")
async def startup_event():
    print("🚀 Document to Markdown Converter started")
    print(f"📁 Temp directory: {TEMP_DIR}")
    print(f"🤖 Model API: {MODEL_API_URL}")
    print(f"🆔 Model ID: {MODEL_ID}")

@app.on_event("shutdown")
async def shutdown_event():
    print("🧹 Cleaning up temporary files...")
    try:
        shutil.rmtree(TEMP_DIR)
    except Exception as e:
        print(f"Cleanup error: {e}")

# ============================================================================
# MAIN
# ============================================================================

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8050"))
    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=port,
        reload=False,
        log_level="info"
    )