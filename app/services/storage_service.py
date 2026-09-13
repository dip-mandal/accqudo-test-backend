import uuid

import boto3
from botocore.config import Config

from app.core.config import settings


class StorageService:
    """
    Cloudflare R2 storage service.

    Uses the S3-compatible R2 API and generates short-lived
    presigned PUT URLs for browser uploads.
    """

    @staticmethod
    def get_s3_client():
        """
        Create a Cloudflare R2 S3-compatible client.
        """

        account_id = str(settings.R2_ACCOUNT_ID or "").strip()
        access_key_id = str(settings.R2_ACCESS_KEY_ID or "").strip()
        secret_access_key = str(
            settings.R2_SECRET_ACCESS_KEY or ""
        ).strip()

        if not account_id:
            raise RuntimeError(
                "R2_ACCOUNT_ID is not configured."
            )

        if not access_key_id:
            raise RuntimeError(
                "R2_ACCESS_KEY_ID is not configured."
            )

        if not secret_access_key:
            raise RuntimeError(
                "R2_SECRET_ACCESS_KEY is not configured."
            )

        endpoint = (
            f"https://{account_id}.r2.cloudflarestorage.com"
        )

        return boto3.client(
            service_name="s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name="auto",
            config=Config(
                signature_version="s3v4"
            ),
        )

    @classmethod
    def generate_presigned_upload_url(
        cls,
        file_extension: str,
        content_type: str,
        folder: str = "questions",
    ) -> dict:
        """
        Generate a presigned PUT URL for uploading an object
        directly from the browser to Cloudflare R2.
        """

        # --------------------------------------------------
        # Validate configuration
        # --------------------------------------------------

        bucket = str(
            settings.R2_BUCKET_NAME or ""
        ).strip()

        if not bucket:
            raise RuntimeError(
                "R2_BUCKET_NAME is not configured."
            )

        public_domain = str(
            settings.R2_PUBLIC_DOMAIN or ""
        ).strip().rstrip("/")

        if not public_domain:
            raise RuntimeError(
                "R2_PUBLIC_DOMAIN is not configured."
            )

        # --------------------------------------------------
        # Normalize file extension
        # --------------------------------------------------

        ext = str(
            file_extension or "png"
        ).strip().lstrip(".").lower()

        if not ext:
            ext = "png"

        # --------------------------------------------------
        # Normalize content type
        # --------------------------------------------------

        normalized_content_type = str(
            content_type or "application/octet-stream"
        ).strip().lower()

        # --------------------------------------------------
        # Normalize folder
        # --------------------------------------------------

        normalized_folder = str(
            folder or "questions"
        ).strip().strip("/")

        if not normalized_folder:
            normalized_folder = "questions"

        # --------------------------------------------------
        # Generate unique object key
        # --------------------------------------------------

        filename = (
            f"{normalized_folder}/"
            f"{uuid.uuid4().hex}.{ext}"
        )

        # --------------------------------------------------
        # Create R2 client
        # --------------------------------------------------

        client = cls.get_s3_client()

        # --------------------------------------------------
        # Generate presigned PUT URL
        #
        # ContentType is intentionally included here.
        # The browser MUST send the exact same Content-Type
        # header when performing the PUT.
        # --------------------------------------------------

        upload_url = client.generate_presigned_url(
            ClientMethod="put_object",
            Params={
                "Bucket": bucket,
                "Key": filename,
                "ContentType": normalized_content_type,
            },
            ExpiresIn=300,
        )

        # --------------------------------------------------
        # Public URL
        #
        # R2_PUBLIC_DOMAIN should be your public/custom
        # domain for serving uploaded images.
        # --------------------------------------------------

        public_url = (
            f"{public_domain}/{filename}"
        )

        return {
            "upload_url": upload_url,
            "file_key": filename,
            "public_url": public_url,
        }