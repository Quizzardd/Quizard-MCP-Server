import requests
from google.cloud import storage
from storage_url import parse_storage_url
from pdf_utils import extract_pdf_text

def _is_pdf_bytes(b: bytes) -> bool:
    return len(b) >= 4 and b[:4] == b"%PDF"

def read_content_file_from_URL(file_url: str) -> str:
    bucket, blob_name = parse_storage_url(file_url)
    if bucket and blob_name:
        client = storage.Client()
        bucket_ref = client.bucket(bucket)
        blob = bucket_ref.blob(blob_name)
        data = blob.download_as_bytes()
        if _is_pdf_bytes(data) or file_url.lower().endswith(".pdf"):
            try:
                text = extract_pdf_text(data)
                if text:
                    return text
            except Exception:
                pass
        try:
            return data.decode("utf-8")
        except Exception:
            return data.decode("utf-8", errors="replace")
    else:
        resp = requests.get(file_url, timeout=30)
        resp.raise_for_status()
        content = resp.content
        ct = resp.headers.get("Content-Type", "").lower()
        if ct.startswith("application/pdf") or file_url.lower().endswith(".pdf") or _is_pdf_bytes(content):
            try:
                text = extract_pdf_text(content)
                if text:
                    return text
            except Exception:
                pass
        try:
            return content.decode("utf-8")
        except Exception:
            return content.decode("utf-8", errors="replace")