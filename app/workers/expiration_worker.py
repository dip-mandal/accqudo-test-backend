import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.database import AsyncSessionLocal
from app.core.redis import get_redis
from app.models.attempt import AttemptStatus, TestAttempt
from app.services.leaderboard_service import LeaderboardService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("accqudo_worker")


def _safe_json(val: Any) -> Any:
    if isinstance(val, (dict, list)):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            return val
    return val


async def evaluate_and_finalize_attempt(attempt_id: int):
    async with AsyncSessionLocal() as db:
        stmt = (
            select(TestAttempt)
            .options(selectinload(TestAttempt.snapshots))
            .where(TestAttempt.id == attempt_id)
        )
        attempt = (await db.execute(stmt)).scalar_one_or_none()

        if not attempt or attempt.status in [
            AttemptStatus.SUBMITTED,
            AttemptStatus.AUTO_SUBMITTED,
        ]:
            return

        total_score = 0.0
        correct_count = 0
        incorrect_count = 0
        unanswered_count = 0

        for snap in attempt.snapshots:
            eval_data = _safe_json(snap.evaluation_data) or {}
            resp = _safe_json(snap.student_response)

            q_type = str(
                snap.question_type.value
                if hasattr(snap.question_type, "value")
                else snap.question_type
            ).upper()
            marks = float(snap.marks if snap.marks is not None else 1.0)
            neg_marks = float(snap.negative_marks if snap.negative_marks is not None else 0.0)

            # Check if unanswered
            if resp is None or resp == "" or resp == [] or resp == {}:
                unanswered_count += 1
                snap.is_correct = None
                snap.obtained_marks = 0.0
                continue

            is_correct = False

            if q_type == "MCQ":
                raw_keys = eval_data.get("correct_keys") or eval_data.get("correct_options") or []
                correct_keys = [str(k).strip().upper() for k in raw_keys] if isinstance(raw_keys, list) else [str(raw_keys).strip().upper()]
                user_val = resp[0] if isinstance(resp, list) and len(resp) > 0 else resp
                if str(user_val).strip().upper() in correct_keys:
                    is_correct = True

            elif q_type == "MSQ":
                raw_keys = eval_data.get("correct_keys") or eval_data.get("correct_options") or []
                correct_keys = set(str(k).strip().upper() for k in raw_keys) if isinstance(raw_keys, list) else {str(raw_keys).strip().upper()}
                raw_user_keys = resp if isinstance(resp, list) else [resp]
                user_keys = set(str(k).strip().upper() for k in raw_user_keys)
                if user_keys == correct_keys and len(correct_keys) > 0:
                    is_correct = True

            elif q_type == "NAT":
                exact = eval_data.get("exact") if isinstance(eval_data, dict) else None
                tol_min = eval_data.get("tolerance_min") or eval_data.get("range_min") if isinstance(eval_data, dict) else None
                tol_max = eval_data.get("tolerance_max") or eval_data.get("range_max") if isinstance(eval_data, dict) else None
                try:
                    user_num = float(resp if not isinstance(resp, list) else resp[0])
                    if exact is not None and abs(user_num - float(exact)) < 1e-4:
                        is_correct = True
                    elif (
                        tol_min is not None
                        and tol_max is not None
                        and float(tol_min) <= user_num <= float(tol_max)
                    ):
                        is_correct = True
                except (ValueError, TypeError):
                    is_correct = False

            if is_correct:
                snap.is_correct = True
                snap.obtained_marks = marks
                total_score += marks
                correct_count += 1
            else:
                snap.is_correct = False
                penalty = neg_marks if q_type == "MCQ" else 0.0
                snap.obtained_marks = -penalty
                total_score -= penalty
                incorrect_count += 1

        attempt.status = AttemptStatus.AUTO_SUBMITTED
        attempt.total_score = round(total_score, 2)
        attempt.correct_count = correct_count
        attempt.incorrect_count = incorrect_count
        attempt.unanswered_count = unanswered_count
        attempt.submitted_at = datetime.now(timezone.utc)

        if attempt.is_rank_eligible is None:
            attempt.is_rank_eligible = (attempt.attempt_number == 1)

        await db.commit()
        await db.refresh(attempt)

        if attempt.is_rank_eligible:
            try:
                await LeaderboardService.record_attempt_score(attempt)
            except Exception as e:
                logger.error(f"Leaderboard record failed for Attempt #{attempt.id}: {e}")

        logger.info(
            f"Successfully auto-submitted Attempt #{attempt.id} | "
            f"Score: {attempt.total_score} | Correct: {attempt.correct_count} | "
            f"Incorrect: {attempt.incorrect_count}"
        )


async def monitor_attempt_expirations():
    logger.info("Expiration worker daemon initialized. Polling Redis 'attempt_expirations'...")

    while True:
        try:
            r = get_redis()
            now_epoch = int(datetime.now(timezone.utc).timestamp())

            expired_ids = await r.zrangebyscore(
                "attempt_expirations", min="-inf", max=now_epoch
            )

            if expired_ids:
                for attempt_id_str in expired_ids:
                    attempt_id = int(attempt_id_str)
                    logger.info(f"Attempt #{attempt_id} reached deadline. Triggering grading pipeline...")
                    await evaluate_and_finalize_attempt(attempt_id)
                    await r.zrem("attempt_expirations", attempt_id_str)

        except Exception as e:
            logger.error(f"Worker iteration exception: {e}", exc_info=True)

        await asyncio.sleep(3)


if __name__ == "__main__":
    asyncio.run(monitor_attempt_expirations())