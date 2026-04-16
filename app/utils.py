import hashlib
from datetime import datetime

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def build_object_key(camera_id: str, ts_str: str) -> str:
    dt = datetime.strptime(ts_str, "%Y%m%d_%H%M")
    return (
        f"camera_id={camera_id}/"
        f"date={dt.strftime('%Y-%m-%d')}/"
        f"hour={dt.strftime('%H')}/"
        f"{camera_id}_{dt.strftime('%Y%m%dT%H%M00')}.jpg"
    )