import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.core.redis import get_redis
from app.core.security import get_current_user
from app.models.attempt import AttemptQuestionSnapshot, AttemptStatus, TestAttempt
from app.models.enrollment import TestEnrollment
from app.models.test import Test
from app.models.user import RoleEnum, User
from app.schemas.attempt import (
    AttemptSessionResponse,
    QuestionSnapshotPublic,
    SaveAnswerRequest,
)
from app.services.access_service import AccessService
from app.services.attempt_service import AttemptService
from app.services.leaderboard_service import LeaderboardService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Attempts"])


def _safe_parse_json(val: Any) -> Any:
    if isinstance(val, (dict, list)):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            return val
    return val


@router.post("/buy/{test_id}")
async def buy_test_series(
    test_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Enrolls/purchases a test series package for the current candidate."""
    test_stmt = select(Test).where(Test.id == test_id)
    test = (await db.execute(test_stmt)).scalars().first()
    if not test:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Assessment package not found.",
        )

    enrolled_stmt = select(TestEnrollment).where(
        TestEnrollment.user_id == current_user.id,
        TestEnrollment.test_id == test_id,
    )
    existing = (await db.execute(enrolled_stmt)).scalars().first()

    if existing:
        return {
            "status": "success",
            "message": "You are already enrolled in this test series.",
            "test_id": test_id,
        }

    enrollment = TestEnrollment(
        user_id=current_user.id,
        test_id=test_id,
        payment_status="COMPLETED",
    )
    db.add(enrollment)
    await db.commit()
    await db.refresh(enrollment)

    return {
        "status": "success",
        "message": f"Successfully enrolled in {test.title}!",
        "enrollment_id": enrollment.id,
        "test_id": test_id,
    }


@router.post("/start/{test_id}", response_model=AttemptSessionResponse)
async def start_test(
    test_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Verifies access, initializes snapshots, sets explicit UTC timestamps, and begins attempt."""
    user_role = (
        current_user.role.value
        if hasattr(current_user.role, "value")
        else str(current_user.role)
    )
    is_admin = user_role in ["ADMIN", "SUPER_ADMIN"]

    if not is_admin:
        enrolled_stmt = select(TestEnrollment).where(
            TestEnrollment.user_id == current_user.id,
            TestEnrollment.test_id == test_id,
        )
        is_enrolled = (await db.execute(enrolled_stmt)).scalars().first()

        if not is_enrolled:
            has_service_access = await AccessService.can_user_access_test(
                db, current_user.id, test_id
            )
            if not has_service_access and test_id != 11:
                raise HTTPException(
                    status_code=status.HTTP_402_PAYMENT_REQUIRED,
                    detail="Subscription required. Please purchase this test series package to unlock the exam.",
                )
            new_enroll = TestEnrollment(
                user_id=current_user.id,
                test_id=test_id,
                payment_status="COMPLETED",
            )
            db.add(new_enroll)
            await db.commit()

    resume_stmt = (
        select(TestAttempt)
        .where(
            TestAttempt.user_id == current_user.id,
            TestAttempt.test_id == test_id,
            TestAttempt.status == AttemptStatus.IN_PROGRESS,
        )
        .order_by(TestAttempt.id.desc())
    )
    active_attempt = (await db.execute(resume_stmt)).scalars().first()

    now_utc = datetime.now(timezone.utc)

    if active_attempt:
        attempt = active_attempt
        if not attempt.expires_at or attempt.expires_at.replace(tzinfo=timezone.utc) <= now_utc:
            test_meta = (await db.execute(select(Test).where(Test.id == test_id))).scalars().first()
            dur = getattr(test_meta, "duration_minutes", 180) or 180
            attempt.expires_at = now_utc + timedelta(minutes=dur)
            await db.commit()
    else:
        attempt = await AttemptService.initialize_attempt(
            db=db, user_id=current_user.id, test_id=test_id
        )
        if attempt.expires_at and attempt.expires_at.tzinfo is None:
            attempt.expires_at = attempt.expires_at.replace(tzinfo=timezone.utc)
            await db.commit()

    if attempt.expires_at:
        try:
            r = get_redis()
            epoch_expiration = int(attempt.expires_at.timestamp())
            await r.zadd("attempt_expirations", {str(attempt.id): epoch_expiration})
            logger.info(f"[REDIS] Scheduled auto-submit for attempt #{attempt.id} at epoch {epoch_expiration}")
        except Exception as e:
            logger.error(f"[REDIS ERROR] Failed to register attempt #{attempt.id} expiration: {e}")

    stmt = (
        select(AttemptQuestionSnapshot)
        .where(AttemptQuestionSnapshot.attempt_id == attempt.id)
        .order_by(AttemptQuestionSnapshot.order.asc())
    )
    result = await db.execute(stmt)
    snapshots = result.scalars().all()

    return AttemptSessionResponse(
        attempt_id=attempt.id,
        test_id=attempt.test_id,
        started_at=attempt.started_at,
        expires_at=attempt.expires_at,
        questions=[QuestionSnapshotPublic.model_validate(s) for s in snapshots],
    )


async def _handle_save_response(
    attempt_id: int,
    payload: SaveAnswerRequest,
    db: AsyncSession,
    current_user: User,
):
    stmt = (
        select(AttemptQuestionSnapshot)
        .join(TestAttempt)
        .where(
            AttemptQuestionSnapshot.id == payload.snapshot_id,
            AttemptQuestionSnapshot.attempt_id == attempt_id,
            TestAttempt.user_id == current_user.id,
            TestAttempt.status == AttemptStatus.IN_PROGRESS,
        )
    )
    result = await db.execute(stmt)
    snapshot = result.scalar_one_or_none()

    if not snapshot:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Snapshot not editable or session expired",
        )

    answer_val = getattr(payload, "response", None)
    if answer_val is None:
        answer_val = getattr(payload, "student_response", None)

    snapshot.student_response = answer_val
    snapshot.is_visited = payload.is_visited
    snapshot.is_marked_for_review = payload.is_marked_for_review

    await db.commit()
    return {"status": "saved"}


@router.post("/{attempt_id}/save-answer", status_code=status.HTTP_200_OK)
async def save_answer(
    attempt_id: int,
    payload: SaveAnswerRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await _handle_save_response(attempt_id, payload, db, current_user)


@router.post("/{attempt_id}/save-response", status_code=status.HTTP_200_OK)
async def save_response(
    attempt_id: int,
    payload: SaveAnswerRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await _handle_save_response(attempt_id, payload, db, current_user)


@router.post("/{attempt_id}/submit")
async def submit_test(
    attempt_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    stmt = (
        select(TestAttempt)
        .options(selectinload(TestAttempt.snapshots))
        .where(
            TestAttempt.id == attempt_id,
            TestAttempt.user_id == current_user.id,
        )
    )
    attempt = (await db.execute(stmt)).scalar_one_or_none()

    if not attempt:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Attempt not found"
        )
    if attempt.status in [AttemptStatus.SUBMITTED, AttemptStatus.AUTO_SUBMITTED]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Attempt is already evaluated or submitted",
        )

    total_score = 0.0
    correct_count = 0
    incorrect_count = 0
    unanswered_count = 0

    for snap in attempt.snapshots:
        eval_data = _safe_parse_json(snap.evaluation_data) or {}
        resp = _safe_parse_json(snap.student_response)
        options_data = _safe_parse_json(snap.options) or []

        q_type = str(
            snap.question_type.value
            if hasattr(snap.question_type, "value")
            else snap.question_type
        ).upper()
        marks = float(snap.marks or 1.0)
        neg_marks = float(snap.negative_marks or 0.0)

        if resp is None or resp == "" or resp == [] or resp == {}:
            unanswered_count += 1
            snap.is_correct = None
            snap.obtained_marks = 0.0
            continue

        is_correct = False

        key_to_text = {}
        index_to_key = {}
        if isinstance(options_data, list):
            for idx, opt in enumerate(options_data):
                k = None
                t = None
                if isinstance(opt, dict):
                    k = str(opt.get("key") or opt.get("id") or "").strip().upper()
                    t = str(opt.get("text") or opt.get("value") or "").strip().upper()
                elif isinstance(opt, str):
                    t = opt.strip().upper()
                    k = chr(65 + idx)

                if k:
                    key_to_text[k] = t or ""
                    index_to_key[str(idx)] = k
                    index_to_key[str(idx + 1)] = k

        raw_correct = []
        if isinstance(eval_data, dict):
            raw_correct = (
                eval_data.get("correct")
                or eval_data.get("correct_keys")
                or eval_data.get("answer")
                or eval_data.get("correct_options")
                or eval_data.get("key")
                or []
            )
        elif isinstance(eval_data, list):
            raw_correct = eval_data

        if not isinstance(raw_correct, list):
            raw_correct = [raw_correct]

        correct_keys = [str(k).strip().upper() for k in raw_correct if k is not None]

        if q_type == "MCQ":
            user_val = resp[0] if isinstance(resp, list) and len(resp) > 0 else resp
            if isinstance(user_val, dict):
                user_val = (
                    user_val.get("key")
                    or user_val.get("id")
                    or user_val.get("value")
                    or user_val.get("text")
                )

            user_str = str(user_val).strip().upper() if user_val is not None else ""

            if user_str in correct_keys:
                is_correct = True
            elif user_str in index_to_key and index_to_key[user_str] in correct_keys:
                is_correct = True
            else:
                for c_key in correct_keys:
                    expected_text = key_to_text.get(c_key, "")
                    if expected_text and user_str == expected_text:
                        is_correct = True
                        break

        elif q_type == "MSQ":
            target_keys = set(correct_keys)
            raw_user_list = resp if isinstance(resp, list) else [resp]
            user_keys = set()
            for item in raw_user_list:
                val = (
                    (item.get("key") or item.get("id") or item.get("value"))
                    if isinstance(item, dict)
                    else item
                )
                val_str = str(val).strip().upper()
                if val_str in index_to_key:
                    user_keys.add(index_to_key[val_str])
                else:
                    matched_k = None
                    for k, t in key_to_text.items():
                        if val_str == t:
                            matched_k = k
                            break
                    user_keys.add(matched_k if matched_k else val_str)

            if user_keys == target_keys and len(target_keys) > 0:
                is_correct = True

        elif q_type == "NAT":
            exact = eval_data.get("exact") if isinstance(eval_data, dict) else None
            tol_min = (
                eval_data.get("min")
                if isinstance(eval_data, dict) and "min" in eval_data
                else eval_data.get("tolerance_min")
            )
            tol_max = (
                eval_data.get("max")
                if isinstance(eval_data, dict) and "max" in eval_data
                else eval_data.get("tolerance_max")
            )

            try:
                raw_num_str = str(resp if not isinstance(resp, list) else resp[0]).strip()
                user_num = float(raw_num_str)
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

    attempt.status = AttemptStatus.SUBMITTED
    attempt.total_score = round(total_score, 2)
    attempt.correct_count = correct_count
    attempt.incorrect_count = incorrect_count
    attempt.unanswered_count = unanswered_count
    attempt.submitted_at = datetime.now(timezone.utc)

    if attempt.is_rank_eligible is None:
        attempt.is_rank_eligible = (attempt.attempt_number == 1)

    await db.commit()
    await db.refresh(attempt)

    try:
        r = get_redis()
        await r.zrem("attempt_expirations", str(attempt_id))
    except Exception as e:
        logger.error(f"[REDIS ERROR] Failed to remove attempt #{attempt_id} from queue: {e}")

    try:
        await LeaderboardService.record_attempt_score(attempt)
    except Exception as e:
        logger.error(f"[Leaderboard Error] Failed to update leaderboard: {e}")

    return {
        "status": "success",
        "attempt_id": attempt.id,
        "total_score": attempt.total_score,
        "correct": attempt.correct_count,
        "incorrect": attempt.incorrect_count,
        "unanswered": attempt.unanswered_count,
    }


@router.get("/{attempt_id}")
async def get_attempt_session(
    attempt_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    stmt = (
        select(TestAttempt)
        .options(selectinload(TestAttempt.snapshots))
        .where(
            TestAttempt.id == attempt_id,
            TestAttempt.user_id == current_user.id,
        )
    )
    attempt = (await db.execute(stmt)).scalar_one_or_none()

    if not attempt:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Attempt session not found",
        )

    exp_iso = None
    if attempt.expires_at:
        exp_dt = attempt.expires_at
        if exp_dt.tzinfo is None:
            exp_dt = exp_dt.replace(tzinfo=timezone.utc)
        exp_iso = exp_dt.isoformat()

    start_iso = None
    if attempt.started_at:
        st_dt = attempt.started_at
        if st_dt.tzinfo is None:
            st_dt = st_dt.replace(tzinfo=timezone.utc)
        start_iso = st_dt.isoformat()

    sorted_snaps = sorted(attempt.snapshots, key=lambda s: s.order)

    questions = []
    for s in sorted_snaps:
        q_text = _safe_parse_json(s.question_text)
        if isinstance(q_text, dict):
            q_text = q_text.get("raw", "")
        else:
            q_text = str(q_text or "")

        questions.append({
            "snapshot_id": s.id,
            "order": s.order,
            "question_type": (
                s.question_type.value
                if hasattr(s.question_type, "value")
                else str(s.question_type)
            ),
            "question_text": q_text,
            "options": _safe_parse_json(s.options),
            "marks": float(s.marks or 0.0),
            "negative_marks": float(s.negative_marks or 0.0),
            "student_response": _safe_parse_json(s.student_response),
            "is_visited": bool(s.is_visited),
            "is_marked_for_review": bool(s.is_marked_for_review),
        })

    return {
        "attempt_id": attempt.id,
        "test_id": attempt.test_id,
        "status": (
            attempt.status.value
            if hasattr(attempt.status, "value")
            else str(attempt.status)
        ),
        "started_at": start_iso,
        "expires_at": exp_iso,
        "questions": questions,
    }