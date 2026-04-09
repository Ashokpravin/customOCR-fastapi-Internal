import os
import tempfile
from typing import Optional
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import FileResponse
import requests
from dotenv import load_dotenv
import uvicorn

# Document parsing libraries
import PyPDF2
from docx import Document
from pptx import Presentation

load_dotenv()

app = FastAPI(title="Document to Markdown Converter")

# Configuration from environment
MODEL_API_URL = os.getenv("MODEL_API_URL", "http://llm-api.kt-application.svc.cluster.local:8000/model/api/completions")
MODEL_ID = os.getenv("MODEL_ID", "69d0a234e26c20b56b5fbe2d")
API_KEY = os.getenv("API_KEY", "")  # if required by the platform

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

def call_model_api(prompt: str) -> str:
    """Call the internal GPT-4o model to format text as Markdown."""
    headers = {
        "Content-Type": "application/json",
        # If API key is needed, add "Authorization": f"Bearer {API_KEY}"
    }
    payload = {
        "model": MODEL_ID,
        "messages": [
            {
                "role": "system",
                "content": "You are a helpful assistant that converts raw document text into clean, well-structured Markdown. Preserve all factual information and improve readability."
            },
            {
                "role": "user",
                "content": f"Convert the following document text into Markdown format:\n\n{prompt}"
            }
        ],
        "temperature": 0.2,
        "max_tokens": 4000
    }

    response = requests.post(MODEL_API_URL, json=payload, headers=headers, timeout=60)
    response.raise_for_status()
    data = response.json()
    # Assume response format similar to OpenAI: choices[0].message.content
    return data["choices"][0]["message"]["content"]

@app.post("/convert")
async def convert_document_to_markdown(
    file: UploadFile = File(...),
    use_llm: Optional[bool] = True
):
    # Validate file extension
    allowed_extensions = {".pdf", ".docx", ".pptx"}
    file_ext = os.path.splitext(file.filename)[1].lower()
    if file_ext not in allowed_extensions:
        raise HTTPException(400, f"Only {', '.join(allowed_extensions)} files are supported.")

    # Save uploaded file temporarily
    with tempfile.NamedTemporaryFile(delete=False, suffix=file_ext) as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = tmp.name

    try:
        # Extract raw text
        raw_text = extract_text(tmp_path, file_ext)

        if use_llm:
            # Enhance with LLM to produce Markdown
            markdown_content = call_model_api(raw_text)
        else:
            # Fallback: wrap raw text in basic Markdown code block
            markdown_content = f"```text\n{raw_text}\n```"

        # Save to a temporary .md file
        md_filename = os.path.splitext(file.filename)[0] + ".md"
        md_path = os.path.join(tempfile.gettempdir(), md_filename)
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(markdown_content)

        return FileResponse(
            path=md_path,
            media_type="text/markdown",
            filename=md_filename
        )

    except Exception as e:
        raise HTTPException(500, f"Processing error: {str(e)}")
    finally:
        # Clean up the uploaded temporary file
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

@app.get("/health")
async def health_check():
    return {"status": "ok"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
