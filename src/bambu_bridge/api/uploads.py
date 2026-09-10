"""Bound the in-memory copy of a multipart upload before validation or transfer."""

from fastapi import HTTPException, UploadFile

from bambu_bridge.config import Settings


async def read_upload(file: UploadFile) -> bytes:
    limit = Settings().bridge_max_transfer_bytes
    if file.size is not None and file.size > limit:
        raise HTTPException(status_code=413, detail=f"File exceeds {limit} byte transfer limit")
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(status_code=413, detail=f"File exceeds {limit} byte transfer limit")
    return data
