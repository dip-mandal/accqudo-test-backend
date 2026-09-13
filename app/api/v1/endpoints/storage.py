from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.core.security import get_current_user
from app.models.user import User, RoleEnum
from app.services.storage_service import StorageService


router = APIRouter(
    tags=["Storage & Media"],
)


class PresignedUploadRequest(BaseModel):
    file_extension: str
    content_type: str
    folder: str = "questions"


@router.post("/presigned-upload")
async def get_presigned_upload_url(
    payload: PresignedUploadRequest,
    current_user: User = Depends(get_current_user),
):
    """
    Generate a Cloudflare R2 presigned upload URL.

    Only ADMIN and SUPER_ADMIN users are allowed to upload
    question media files.
    """

    if current_user.role not in [
        RoleEnum.ADMIN,
        RoleEnum.SUPER_ADMIN,
    ]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin credentials required.",
        )

    return StorageService.generate_presigned_upload_url(
        file_extension=payload.file_extension,
        content_type=payload.content_type,
        folder=payload.folder,
    )