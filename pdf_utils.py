import io
from PyPDF2 import PdfReader

def extract_pdf_text(b: bytes) -> str:
    reader = PdfReader(io.BytesIO(b))
    out = []
    for p in reader.pages:
        t = p.extract_text() or ""
        out.append(t)
    return "\n\n".join(out).strip()