"""Batch upload router.

Three endpoints that power the direct-to-R2 upload flow:

  POST /batch-init    – generate presigned PUT URLs + create video records
  POST /batch-confirm – mark files as uploaded, enqueue for AI analysis
  GET  /batch/{batch_id}/status – poll processing progress
"""

from fastapi import APIRouter, Depends, HTTPException, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from app.database import get_db
from app.dependencies import verify_api_key
from app.services.batch_upload_service import BatchUploadService

router = APIRouter(
    prefix="/api/v1/channels/{channel_id}/videos",
    tags=["batch-upload"],
    dependencies=[Depends(verify_api_key)],
)


def get_batch_service(db: AsyncIOMotorDatabase = Depends(get_db)) -> BatchUploadService:
    from app.main import gemini_service, r2_service

    return BatchUploadService(db=db, r2=r2_service, gemini=gemini_service)


# ── Schemas ────────────────────────────────────────────────────────────────


class BatchFileEntry(BaseModel):
    filename: str
    size_bytes: int = Field(0, ge=0)
    channels: list[str] = Field(default_factory=list)


class BatchInitRequest(BaseModel):
    files: list[BatchFileEntry]
    scheduled_at: str | None = None


class BatchConfirmRequest(BaseModel):
    batch_id: str
    confirmed_file_ids: list[str]


# ── Endpoints ──────────────────────────────────────────────────────────────


@router.post("/batch-init")
async def batch_init(
    channel_id: str,
    body: BatchInitRequest,
    service: BatchUploadService = Depends(get_batch_service),
):
    """Generate presigned PUT URLs and create video DB records.

    The client uses the returned ``upload_url`` values to PUT each file
    directly to R2 (browser → R2, no server memory used).
    After all uploads succeed the client calls ``/batch-confirm``.
    """
    if not body.files:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="files list is empty")
    if not service.r2:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="R2 not initialised")
    try:
        return await service.batch_init(
            channel_id,
            [f.dict() for f in body.files],
            body.scheduled_at,
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.post("/batch-confirm")
async def batch_confirm(
    channel_id: str,
    body: BatchConfirmRequest,
    service: BatchUploadService = Depends(get_batch_service),
):
    """Signal that the listed files have been successfully uploaded to R2.

    The server updates their status to ``queued`` and enqueues them for
    sequential AI analysis.
    """
    try:
        return await service.batch_confirm(body.batch_id, body.confirmed_file_ids)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.get("/batch/{batch_id}/status")
async def get_batch_status(
    channel_id: str,
    batch_id: str,
    service: BatchUploadService = Depends(get_batch_service),
):
    """Poll the analysis progress for a batch.

    Clients should poll this every 10–20 seconds until all items reach
    ``completed`` or ``failed``.
    """
    return await service.get_batch_status(batch_id)
