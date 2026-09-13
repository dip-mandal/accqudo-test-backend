import asyncio
import time
import logging
from datetime import datetime, timezone
import redis.asyncio as aioredis
from sqlalchemy import select

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.attempt import TestAttempt, AttemptStatus
from app.services.attempt_service import AttemptService

logger = logging.getLogger("accqudo_worker")
logging.basicConfig(level=logging.INFO)


async def process_expired_attempts(r: aioredis.Redis):
    """Polls Redis for expired attempt IDs and grades them automatically in isolated sessions."""
    current_epoch = int(time.time())

    # Fetch attempts whose expiration timestamp <= current_epoch
    expired_ids = await r.zrangebyscore("attempt_expirations", 0, current_epoch)

    if not expired_ids:
        return

    logger.info(f"Found {len(expired_ids)} expired attempt(s) requiring auto-submission.")

    for attempt_id_raw in expired_ids:
        attempt_id_str = (
            attempt_id_raw.decode("utf-8")
            if isinstance(attempt_id_raw, bytes)
            else str(attempt_id_raw)
        )
        attempt_id = int(attempt_id_str)

        # Open a fresh isolated database session for each individual attempt
        async with AsyncSessionLocal() as db:
            try:
                stmt = select(TestAttempt).where(TestAttempt.id == attempt_id)
                attempt = (await db.execute(stmt)).scalar_one_or_none()

                if attempt and attempt.status == AttemptStatus.IN_PROGRESS:
                    # Submit attempt via AttemptService
                    evaluated_attempt = await AttemptService.submit_attempt(
                        db=db,
                        attempt_id=attempt.id,
                        user_id=attempt.user_id
                    )

                    # Mark specifically as AUTO_SUBMITTED
                    evaluated_attempt.status = AttemptStatus.AUTO_SUBMITTED
                    await db.commit()

                    logger.info(
                        f"Auto-submitted attempt #{attempt_id} with score {evaluated_attempt.total_score}."
                    )

                # Clean up processed entry from Redis
                await r.zrem("attempt_expirations", attempt_id_str)

            except Exception as e:
                await db.rollback()
                logger.error(f"Failed to auto-submit attempt #{attempt_id}: {str(e)}", exc_info=True)
                # Optionally remove stuck invalid IDs from Redis if they continuously error out
                # await r.zrem("attempt_expirations", attempt_id_str)


async def run_worker():
    logger.info(f"Starting accqudo Auto-Submission Worker with Redis: {settings.REDIS_URL}")
    r = aioredis.from_url(settings.REDIS_URL, decode_responses=False)

    try:
        await r.ping()
        logger.info("Connected to Redis successfully.")
    except Exception as e:
        logger.error(f"Failed to connect to Redis on startup: {e}")

    try:
        while True:
            try:
                await process_expired_attempts(r)
            except Exception as err:
                logger.error(f"Worker iteration encountered an error: {err}")
            await asyncio.sleep(5)
    finally:
        await r.aclose()


if __name__ == "__main__":
    asyncio.run(run_worker())