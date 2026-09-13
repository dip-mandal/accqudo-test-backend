from typing import Dict, Any, List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from fastapi import HTTPException, status

from app.models.attempt import TestAttempt, AttemptStatus
from app.models.question import Question
from app.models.exam import Topic, Chapter, Subject


class AnalyticsService:
    @staticmethod
    async def get_attempt_analytics(db: AsyncSession, attempt_id: int, user_id: int) -> Dict[str, Any]:
        # 1. Fetch Attempt with snapshots
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

        if attempt.status not in [AttemptStatus.SUBMITTED, AttemptStatus.AUTO_SUBMITTED]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, 
                detail="Attempt is still in progress"
            )

        # 2. Query Hierarchy Details and Solution Text via Explicit Joins
        q_ids = [s.question_id for s in attempt.snapshots if s.question_id is not None]
        hierarchy_map: Dict[int, Dict[str, Any]] = {}

        if q_ids:
            h_stmt = (
                select(
                    Question.id,
                    Topic.name.label("topic_name"),
                    Chapter.name.label("chapter_name"),
                    Subject.name.label("subject_name"),
                    Question.solution_text
                )
                .outerjoin(Topic, Question.topic_id == Topic.id)
                .outerjoin(Chapter, Topic.chapter_id == Chapter.id)
                .outerjoin(Subject, Chapter.subject_id == Subject.id)
                .where(Question.id.in_(q_ids))
            )
            rows = (await db.execute(h_stmt)).all()
            for r in rows:
                hierarchy_map[r[0]] = {
                    "topic_name": r[1] or "General Theory",
                    "chapter_name": r[2] or "Core Concepts",
                    "subject_name": r[3] or "GATE CSE Core",
                    "solution_text": r[4]
                }

        # 3. Aggregate Subject & Topic Breakdowns and Build Question Reviews
        topic_stats: Dict[str, Dict[str, Any]] = {}
        subject_stats: Dict[str, Dict[str, Any]] = {}
        question_reviews: List[Dict[str, Any]] = []

        # Sort snapshots by question order for consistent student review
        sorted_snapshots = sorted(attempt.snapshots, key=lambda s: s.order)

        for snap in sorted_snapshots:
            meta = hierarchy_map.get(snap.question_id, {
                "topic_name": "General Theory",
                "chapter_name": "Core Concepts",
                "subject_name": "GATE CSE Core",
                "solution_text": None
            })
            t_name = meta["topic_name"]
            s_name = meta["subject_name"]
            c_name = meta["chapter_name"]

            # Initialize topic bucket
            if t_name not in topic_stats:
                topic_stats[t_name] = {
                    "topic_name": t_name,
                    "chapter_name": c_name,
                    "subject_name": s_name,
                    "total_questions": 0,
                    "correct": 0,
                    "incorrect": 0,
                    "unanswered": 0,
                    "marks_obtained": 0.0,
                    "total_possible_marks": 0.0,
                }

            # Initialize subject bucket
            if s_name not in subject_stats:
                subject_stats[s_name] = {
                    "subject_name": s_name,
                    "total_questions": 0,
                    "correct": 0,
                    "incorrect": 0,
                    "unanswered": 0,
                    "marks_obtained": 0.0,
                    "total_possible_marks": 0.0,
                }

            marks = float(snap.marks or 0.0)
            obtained = float(snap.obtained_marks or 0.0)

            t = topic_stats[t_name]
            s = subject_stats[s_name]

            t["total_questions"] += 1
            t["total_possible_marks"] += marks
            t["marks_obtained"] += obtained

            s["total_questions"] += 1
            s["total_possible_marks"] += marks
            s["marks_obtained"] += obtained

            if snap.is_correct is True:
                t["correct"] += 1
                s["correct"] += 1
            elif snap.is_correct is False:
                t["incorrect"] += 1
                s["incorrect"] += 1
            else:
                t["unanswered"] += 1
                s["unanswered"] += 1

            # Extract raw question string from JSON snapshot
            raw_text = snap.question_text
            if isinstance(raw_text, dict):
                raw_text = raw_text.get("raw", "")

            question_reviews.append({
                "snapshot_id": snap.id,
                "order": snap.order,
                "question_type": snap.question_type,
                "question_text": raw_text,
                "options": snap.options,
                "student_response": snap.student_response,
                "evaluation_data": snap.evaluation_data,
                "solution_text": meta["solution_text"],
                "marks": marks,
                "negative_marks": float(snap.negative_marks or 0.0),
                "obtained_marks": snap.obtained_marks,
                "is_correct": snap.is_correct
            })

        # 4. Calculate Accuracy and Identify Weak Areas
        weak_topics: List[Dict[str, Any]] = []
        for t in topic_stats.values():
            attempted = t["correct"] + t["incorrect"]
            acc = round((t["correct"] / attempted) * 100, 2) if attempted > 0 else 0.0
            t["accuracy_percent"] = acc

            if acc < 60.0 or t["unanswered"] == t["total_questions"]:
                weak_topics.append({
                    "topic": t["topic_name"],
                    "chapter": t["chapter_name"],
                    "subject": t["subject_name"],
                    "accuracy": acc,
                    "recommendation": "Review high-weightage concepts and solve targeted drills.",
                })

        for s in subject_stats.values():
            attempted = s["correct"] + s["incorrect"]
            s["accuracy_percent"] = round((s["correct"] / attempted) * 100, 2) if attempted > 0 else 0.0

        duration_sec = 0
        if attempt.submitted_at and attempt.started_at:
            duration_sec = int((attempt.submitted_at - attempt.started_at).total_seconds())

        return {
            "attempt_id": attempt.id,
            "test_id": attempt.test_id,
            "overall_summary": {
                "score": float(attempt.total_score),
                "correct": attempt.correct_count,
                "incorrect": attempt.incorrect_count,
                "unanswered": attempt.unanswered_count,
                "duration_taken_seconds": max(0, duration_sec),
            },
            "subject_breakdown": list(subject_stats.values()),
            "topic_breakdown": list(topic_stats.values()),
            "weak_areas": weak_topics,
            "question_reviews": question_reviews
        }
        
    @staticmethod
    async def get_student_dashboard_summary(db: AsyncSession, user_id: int) -> Dict[str, Any]:
        from app.models.test import Test

        stmt = (
            select(TestAttempt, Test.title, Test.total_marks)
            .join(Test, TestAttempt.test_id == Test.id)
            .where(
                TestAttempt.user_id == user_id,
                TestAttempt.status.in_([AttemptStatus.SUBMITTED, AttemptStatus.AUTO_SUBMITTED])
            )
            .order_by(TestAttempt.submitted_at.desc())
        )
        rows = (await db.execute(stmt)).all()

        history = []
        total_score_pct = 0.0
        total_correct = 0
        total_answered = 0

        for attempt, test_title, test_total_marks in rows:
            max_marks = float(test_total_marks or 1.0)
            score = float(attempt.total_score or 0.0)
            percentage = round((score / max_marks) * 100, 2)
            
            attempted = (attempt.correct_count or 0) + (attempt.incorrect_count or 0)
            accuracy = round(((attempt.correct_count or 0) / attempted) * 100, 2) if attempted > 0 else 0.0

            total_correct += (attempt.correct_count or 0)
            total_answered += attempted
            total_score_pct += percentage

            duration = 0
            if attempt.submitted_at and attempt.started_at:
                duration = int((attempt.submitted_at - attempt.started_at).total_seconds())

            history.append({
                "attempt_id": attempt.id,
                "test_id": attempt.test_id,
                "test_title": test_title,
                "score": score,
                "total_marks": max_marks,
                "percentage": percentage,
                "accuracy": accuracy,
                "submitted_at": attempt.submitted_at.isoformat() if attempt.submitted_at else None,
                "duration_taken_seconds": max(0, duration)
            })

        count = len(history)
        avg_pct = round(total_score_pct / count, 2) if count > 0 else 0.0
        overall_acc = round((total_correct / total_answered) * 100, 2) if total_answered > 0 else 0.0

        return {
            "total_attempts": count,
            "tests_completed": count,
            "average_score_percentage": avg_pct,
            "overall_accuracy": overall_acc,
            "history": history
        }