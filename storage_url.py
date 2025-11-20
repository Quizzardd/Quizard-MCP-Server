import urllib.parse

def parse_storage_url(file_url: str):
    u = urllib.parse.urlsplit(file_url)
    if u.scheme == "gs":
        bucket = u.netloc
        blob = u.path.lstrip("/")
        return bucket, blob
    host = u.netloc
    path = u.path.lstrip("/")
    parts = path.split("/") if path else []
    if host == "storage.googleapis.com" and len(parts) >= 2:
        bucket = parts[0]
        blob = "/".join(parts[1:])
        return bucket, blob
    if host == "storage.cloud.google.com" and len(parts) >= 2:
        bucket = parts[0]
        blob = "/".join(parts[1:])
        return bucket, blob
    if host == "firebasestorage.googleapis.com":
        p = parts
        if len(p) >= 5 and p[0] == "v0" and p[1] == "b" and p[3] == "o":
            bucket = p[2]
            blob = urllib.parse.unquote("/".join(p[4:]))
            return bucket, blob
    return None, None