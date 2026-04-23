import hashlib
from datetime import datetime

def sha256_bytes(data: bytes) -> str:
    """
    Compute the SHA-256 hash of the given byte data and return it as a hexadecimal string.
    Args:
        data (bytes): The input data to hash. 
    Returns:
        str: The hexadecimal representation of the SHA-256 hash.
    """
    return hashlib.sha256(data).hexdigest()

def build_object_key(camera_id: str, ts_str: str) -> str:
    """
    Build an S3 object key for a given camera ID and timestamp string.
    The timestamp string is expected to be in the format "YYYYMMDD_HHMM".
    Args:
        camera_id (str): The ID of the camera.
        ts_str (str): The timestamp string in the format "YYYYMMDD_HHMM".
    Returns:
        str: The constructed S3 object key.

    """
    dt = datetime.strptime(ts_str, "%Y%m%d_%H%M")
    return (
        f"camera_id={camera_id}/"
        f"date={dt.strftime('%Y-%m-%d')}/"
        f"hour={dt.strftime('%H')}/"
        f"{camera_id}_{dt.strftime('%Y%m%dT%H%M00')}.jpg"
    )