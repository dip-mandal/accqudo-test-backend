from datetime import datetime, timedelta
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

from app.models.test import Test, TestQuestion
from app.models.attempt import TestAttempt, AttemptQuestionSnapshot, AttemptStatus
from app.models.question import Question
from app.services.evaluation_service import EvaluationService
from app.services.leaderboard_service import LeaderboardService


class AttemptService:
    @staticmethod
    async def initialize_attempt(db: AsyncSession, user_id: int, test_id: int) -> TestAttempt:
        test_stmt = (
            select(Test)
            .options(
                selectinload(Test.test_questions).selectinload(TestQuestion.question)
            )
            .where(Test.id == test_id)
        )
        result = await db.execute(test_stmt)
        test = result.scalar_one_or_none()
        if not test:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Test not found"
            )

        count_stmt = select(func.count(TestAttempt.id)).where(
            TestAttempt.user_id == user_id,
            TestAttempt.test_id == test_id,
            TestAttempt.status.in_([AttemptStatus.SUBMITTED, AttemptStatus.AUTO_SUBMITTED])
        )
        completed_count = (await db.execute(count_stmt)).scalar() or 0
        attempt_number = completed_count + 1
        is_rank_eligible = (attempt_number == 1)

        now = datetime.utcnow()
        expires_at = now + timedelta(minutes=test.duration_minutes)

        attempt = TestAttempt(
            user_id=user_id,
            test_id=test_id,
            attempt_number=attempt_number,
            is_rank_eligible=is_rank_eligible,
            status=AttemptStatus.IN_PROGRESS,
            started_at=now,
            expires_at=expires_at
        )
        db.add(attempt)
        await db.flush()

        for tq in test.test_questions:
            q: Question = tq.question
            snapshot = AttemptQuestionSnapshot(
                attempt_id=attempt.id,
                question_id=q.id,
                order=tq.order,
                question_type=q.question_type.value,
                question_text={"raw": q.question_text},
                options=q.options,
                evaluation_data=q.evaluation_data,
                marks=tq.marks,
                negative_marks=tq.negative_marks,
                student_response=None,
                is_visited=False,
                is_marked_for_review=False
            )
            db.add(snapshot)

        await db.commit()
        await db.refresh(attempt)
        return attempt

    @staticmethod
    async def submit_attempt(db: AsyncSession, attempt_id: int, user_id: int) -> TestAttempt:
        stmt = (
            select(TestAttempt)
            .options(selectinload(TestAttempt.snapshots))
            .where(TestAttempt.id == attempt_id, TestAttempt.user_id == user_id)
        )
        result = await db.execute(stmt)
        attempt = result.scalar_one_or_none()
        if not attempt:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Attempt not found"
            )

        # Idempotent check: if already submitted, return the attempt gracefully
        if attempt.status in [AttemptStatus.SUBMITTED, AttemptStatus.AUTO_SUBMITTED]:
            return attempt

        now = datetime.utcnow()
        total_score = 0.0
        correct_count = 0
        incorrect_count = 0
        unanswered_count = 0

        for snap in attempt.snapshots:
            if snap.student_response is None or snap.student_response == "" or snap.student_response == []:
                unanswered_count += 1
                snap.obtained_marks = 0.0
                snap.is_correct = None
            else:
                is_correct, obtained = EvaluationService.evaluate_response(
                    question_type=snap.question_type,
                    evaluation_data=snap.evaluation_data,
                    student_response=snap.student_response,
                    marks=snap.marks,
                    negative_marks=snap.negative_marks
                )
                snap.obtained_marks = obtained
                snap.is_correct = is_correct
                total_score += obtained

                if is_correct:
                    correct_count += 1
                else:
                    incorrect_count += 1

        attempt.total_score = round(total_score, 2)
        attempt.correct_count = correct_count
        attempt.incorrect_count = incorrect_count
        attempt.unanswered_count = unanswered_count
        attempt.submitted_at = now
        attempt.status = AttemptStatus.SUBMITTED

        await db.commit()
        await db.refresh(attempt)

        if attempt.is_rank_eligible:
            try:
                await LeaderboardService.record_attempt_score(attempt)
            except Exception as e:
                print(f"[Leaderboard Sync Warning] Failed to update Redis rank: {e}", flush=True)

        return attempt