from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime, timezone

from app.core.database import get_db, Base
from app.core.security import get_current_user
from app.models.attempt import TestAttempt
from app.models.enrollment import TestEnrollment
from app.models.test import Test, TestQuestion
from app.models.exam import Exam, Subject, Chapter, Topic
from app.models.question import Question
from app.models.package import SubscriptionPackage, package_tests_table
from app.models.user import User
from app.services.analytics_service import AnalyticsService
from app.services.leaderboard_service import LeaderboardService
from app.services.pdf_service import PDFCertificateService

router = APIRouter(tags=["Analytics & Insights"])


@router.get("/attempt/{attempt_id}")
async def get_attempt_analytics(
    attempt_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Fetch granular question-by-question analytics for a completed attempt."""
    return await AnalyticsService.get_attempt_analytics(db, attempt_id, current_user.id)


@router.get("/dashboard/me")
async def get_my_dashboard(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Aggregate candidate metrics, historical attempts, and the complete
    database-backed test catalog.

    Candidate access is resolved from every real entitlement source currently
    used by Accqudo:

      1. TestEnrollment
      2. Successful legacy razorpay_orders records
      3. Successful current payments records
      4. Active/current subscriptions
      5. package_tests relationships
      6. Individual test purchases from payment/order records

    No dummy/fallback tests are created.
    No artificial test-count limit is applied.
    """

    # -------------------------------------------------------------------------
    # 1. Fetch historical user attempts
    # -------------------------------------------------------------------------
    stmt = (
        select(
            TestAttempt,
            Test.title.label("test_title"),
            Test.total_marks.label("test_total_marks"),
        )
        .join(Test, Test.id == TestAttempt.test_id, isouter=True)
        .where(TestAttempt.user_id == current_user.id)
        .order_by(desc(TestAttempt.id))
    )

    result = await db.execute(stmt)
    records = result.all()

    history = []
    total_attempts = len(records)
    completed_count = 0
    total_percentage_sum = 0.0

    for attempt, test_title, test_total_marks in records:
        status_val = getattr(attempt, "status", "IN_PROGRESS")
        status_str = (
            status_val.value
            if hasattr(status_val, "value")
            else str(status_val)
        ).strip().upper()

        is_completed = status_str in {
            "COMPLETED",
            "SUBMITTED",
            "AUTO_SUBMITTED",
        }

        if is_completed:
            completed_count += 1

        tot_marks = float(test_total_marks or 0.0)
        score = float(getattr(attempt, "total_score", 0.0) or 0.0)

        pct = (
            round((score / tot_marks) * 100.0, 2)
            if tot_marks > 0
            else 0.0
        )

        if is_completed:
            total_percentage_sum += pct

        submitted_at = (
            getattr(attempt, "submitted_at", None)
            or getattr(attempt, "end_time", None)
        )

        started_at = (
            getattr(attempt, "started_at", None)
            or getattr(attempt, "start_time", None)
            or getattr(attempt, "created_at", None)
        )

        duration_sec = 0

        if submitted_at and started_at:
            duration_sec = max(
                0,
                int((submitted_at - started_at).total_seconds()),
            )

        # Do not manufacture an accuracy value. If the attempt model contains
        # a real accuracy field, expose it; otherwise return None.
        attempt_accuracy = getattr(attempt, "accuracy", None)
        if attempt_accuracy is not None:
            try:
                attempt_accuracy = float(attempt_accuracy)
            except (TypeError, ValueError):
                attempt_accuracy = None

        history.append(
            {
                "attempt_id": attempt.id,
                "test_id": attempt.test_id,
                "test_title": test_title or f"Test #{attempt.test_id}",
                "score": score,
                "total_marks": tot_marks,
                "percentage": pct,
                "accuracy": attempt_accuracy,
                "submitted_at": (
                    submitted_at.isoformat()
                    if submitted_at
                    else None
                ),
                "duration_taken_seconds": duration_sec,
                "status": status_str,
            }
        )

    avg_pct = (
        round(total_percentage_sum / completed_count, 2)
        if completed_count > 0
        else 0.0
    )

    # -------------------------------------------------------------------------
    # 2. Start with direct TestEnrollment records
    # -------------------------------------------------------------------------
    enroll_stmt = select(TestEnrollment.test_id).where(
        TestEnrollment.user_id == current_user.id
    )

    enroll_res = await db.execute(enroll_stmt)

    enrolled_ids = {
        int(test_id)
        for test_id in enroll_res.scalars().all()
        if test_id is not None
    }

    # Package IDs and individually purchased test IDs are collected separately.
    purchased_package_ids: set[int] = set()
    purchased_test_ids: set[int] = set()

    # -------------------------------------------------------------------------
    # 3. Resolve payment/order ownership from the actual loaded database tables
    #
    # This intentionally supports both the older razorpay_orders flow and the
    # newer payments/subscriptions flow. Missing tables/columns are ignored
    # safely instead of assuming a particular schema.
    # -------------------------------------------------------------------------
    successful_statuses = {
        "SUCCESS",
        "PAID",
        "CAPTURED",
        "COMPLETED",
        "SUCCESSFUL",
    }

    def normalize_status(value) -> str:
        return (
            value.value
            if hasattr(value, "value")
            else str(value or "")
        ).strip().upper()

    def get_table(*names):
        for name in names:
            table = Base.metadata.tables.get(name)
            if table is not None:
                return table
        return None

    async def collect_payment_entitlements(
        table,
        *,
        allowed_statuses: set[str],
        include_active_without_status: bool = False,
    ):
        """
        Collect package_id/test_id values from a payment-like table without
        assuming that every deployment has exactly the same columns.
        """
        if table is None:
            return

        columns = table.c

        user_col = columns.get("user_id")
        package_col = columns.get("package_id")
        test_col = columns.get("test_id")

        # SQLAlchemy Column objects cannot be evaluated as booleans.
        # Therefore, do not use Python "or" between columns.
        status_col = columns.get("status")
        if status_col is None:
            status_col = columns.get("payment_status")
        if status_col is None:
            status_col = columns.get("order_status")

        if user_col is None:
            return

        selected_columns = [user_col]

        if package_col is not None:
            selected_columns.append(package_col)

        if test_col is not None:
            selected_columns.append(test_col)

        if status_col is not None:
            selected_columns.append(status_col)

        # If the table has neither package_id nor test_id, it cannot establish
        # a test entitlement.
        if package_col is None and test_col is None:
            return

        stmt = select(*selected_columns).where(
            user_col == current_user.id
        )

        result = await db.execute(stmt)

        for row in result.all():
            values = list(row)
            index = 1

            package_id = None
            test_id = None
            row_status = None

            if package_col is not None:
                package_id = values[index]
                index += 1

            if test_col is not None:
                test_id = values[index]
                index += 1

            if status_col is not None:
                row_status = values[index]

            if status_col is not None:
                normalized = normalize_status(row_status)
                if normalized not in allowed_statuses:
                    continue
            elif not include_active_without_status:
                continue

            if package_id is not None:
                try:
                    purchased_package_ids.add(int(package_id))
                except (TypeError, ValueError):
                    pass

            if test_id is not None:
                try:
                    purchased_test_ids.add(int(test_id))
                except (TypeError, ValueError):
                    pass

    # Legacy payment/order table.
    razorpay_orders_table = get_table(
        "razorpay_orders",
        "razorpay_order",
    )

    await collect_payment_entitlements(
        razorpay_orders_table,
        allowed_statuses=successful_statuses,
    )

    # Current payment table.
    payments_table = get_table("payments")

    await collect_payment_entitlements(
        payments_table,
        allowed_statuses=successful_statuses,
    )

    # -------------------------------------------------------------------------
    # 4. Resolve active/current subscriptions
    #
    # A subscription can establish package access even when the payment
    # record is stored in a different table.
    # -------------------------------------------------------------------------
    subscriptions_table = get_table(
        "subscriptions",
        "user_subscriptions",
    )

    if subscriptions_table is not None:
        columns = subscriptions_table.c

        user_col = columns.get("user_id")
        package_col = columns.get("package_id")
        status_col = columns.get("status")

        if user_col is not None and package_col is not None:
            selected_columns = [package_col]

            if status_col is not None:
                selected_columns.append(status_col)

            # Support common expiry column names without requiring them.
            # SQLAlchemy Column objects cannot be evaluated as booleans.
            # Resolve the first available expiry column explicitly.
            expiry_col = columns.get("expiry_date")
            if expiry_col is None:
                expiry_col = columns.get("expires_at")
            if expiry_col is None:
                expiry_col = columns.get("end_date")

            if expiry_col is not None:
                selected_columns.append(expiry_col)

            subscription_stmt = select(*selected_columns).where(
                user_col == current_user.id
            )

            subscription_res = await db.execute(subscription_stmt)

            active_subscription_statuses = {
                "ACTIVE",
                "SUCCESS",
                "PAID",
                "CAPTURED",
                "COMPLETED",
                "SUBSCRIBED",
                "CURRENT",
            }

            for row in subscription_res.all():
                values = list(row)

                package_id = values[0]
                cursor = 1

                row_status = None
                expiry_value = None

                if status_col is not None:
                    row_status = values[cursor]
                    cursor += 1

                    normalized = normalize_status(row_status)

                    if normalized not in active_subscription_statuses:
                        continue

                elif status_col is None:
                    # If the deployment has no status column, the subscription
                    # record itself is the available ownership evidence.
                    pass

                if expiry_col is not None:
                    expiry_value = values[cursor]

                # Do not grant access to an expired subscription.
                if expiry_value is not None:
                    now = datetime.now(timezone.utc)

                    try:
                        if expiry_value.tzinfo is None:
                            expiry_compare = expiry_value.replace(
                                tzinfo=timezone.utc
                            )
                        else:
                            expiry_compare = expiry_value

                        if expiry_compare < now:
                            continue
                    except (AttributeError, TypeError):
                        # If the value is not a datetime-like object, do not
                        # invent an expiry interpretation.
                        pass

                if package_id is not None:
                    try:
                        purchased_package_ids.add(int(package_id))
                    except (TypeError, ValueError):
                        pass

    # -------------------------------------------------------------------------
    # 5. Resolve every real test attached to purchased packages
    # -------------------------------------------------------------------------
    package_tests_table = Base.metadata.tables.get("package_tests")

    if purchased_package_ids and package_tests_table is not None:
        package_col = package_tests_table.c.get("package_id")
        test_col = package_tests_table.c.get("test_id")

        if package_col is not None and test_col is not None:
            pt_stmt = select(test_col).where(
                package_col.in_(purchased_package_ids)
            )

            pt_res = await db.execute(pt_stmt)

            for row in pt_res.all():
                test_id = row[0]

                if test_id is not None:
                    try:
                        purchased_test_ids.add(int(test_id))
                    except (TypeError, ValueError):
                        pass

    # Every actual entitlement source contributes to the final enrolled set.
    enrolled_ids.update(purchased_test_ids)

    # -------------------------------------------------------------------------
    # 6. Fetch ALL real test papers
    #
    # There is deliberately NO .limit(...).
    # -------------------------------------------------------------------------
    catalog_stmt = select(Test).order_by(Test.id.asc())

    catalog_res = await db.execute(catalog_stmt)
    all_tests = catalog_res.scalars().all()

    # -------------------------------------------------------------------------
    # 6A. Build the real academic hierarchy for every test:
    # Exam Stream -> Subject -> Chapter -> Topic -> Test
    #
    # A test is classified from the topics of its real test questions. A test
    # may legitimately occur under multiple topics/chapters/subjects.
    # -------------------------------------------------------------------------
    test_ids = [int(t.id) for t in all_tests]

    taxonomy_by_test: dict[int, list[dict]] = {}
    exam_by_test: dict[int, dict] = {}

    if test_ids:
        exam_rows = (
            await db.execute(
                select(
                    Test.id,
                    Exam.id,
                    Exam.title,
                    Exam.code,
                )
                .join(Exam, Exam.id == Test.exam_id)
                .where(Test.id.in_(test_ids))
            )
        ).all()

        for test_id, exam_id, exam_title, exam_code in exam_rows:
            exam_by_test[int(test_id)] = {
                "id": int(exam_id),
                "title": exam_title,
                "code": exam_code,
            }

        taxonomy_stmt = (
            select(
                TestQuestion.test_id,
                Exam.id.label("exam_id"),
                Exam.title.label("exam_title"),
                Exam.code.label("exam_code"),
                Subject.id.label("subject_id"),
                Subject.name.label("subject_name"),
                Chapter.id.label("chapter_id"),
                Chapter.name.label("chapter_name"),
                Topic.id.label("topic_id"),
                Topic.name.label("topic_name"),
            )
            .join(Question, Question.id == TestQuestion.question_id)
            .join(Topic, Topic.id == Question.topic_id)
            .join(Chapter, Chapter.id == Topic.chapter_id)
            .join(Subject, Subject.id == Chapter.subject_id)
            .join(Exam, Exam.id == Subject.exam_id)
            .where(TestQuestion.test_id.in_(test_ids))
        )

        taxonomy_rows = (await db.execute(taxonomy_stmt)).all()

        seen_paths: set[tuple] = set()
        for row in taxonomy_rows:
            (
                test_id,
                exam_id,
                exam_title,
                exam_code,
                subject_id,
                subject_name,
                chapter_id,
                chapter_name,
                topic_id,
                topic_name,
            ) = row

            key = (
                int(test_id),
                int(subject_id),
                int(chapter_id),
                int(topic_id),
            )
            if key in seen_paths:
                continue
            seen_paths.add(key)

            taxonomy_by_test.setdefault(int(test_id), []).append(
                {
                    "exam_stream": {
                        "id": int(exam_id),
                        "title": exam_title,
                        "code": exam_code,
                    },
                    "subject": {
                        "id": int(subject_id),
                        "name": subject_name,
                    },
                    "chapter": {
                        "id": int(chapter_id),
                        "name": chapter_name,
                    },
                    "topic": {
                        "id": int(topic_id),
                        "name": topic_name,
                    },
                }
            )

    # -------------------------------------------------------------------------
    # 6B. Resolve purchased package -> tests and package metadata.
    # -------------------------------------------------------------------------
    package_by_test: dict[int, list[dict]] = {}

    if purchased_package_ids:
        package_rows = (
            await db.execute(
                select(
                    SubscriptionPackage.id,
                    SubscriptionPackage.exam_id,
                    SubscriptionPackage.title,
                    Exam.title.label("exam_title"),
                    Exam.code.label("exam_code"),
                )
                .join(Exam, Exam.id == SubscriptionPackage.exam_id)
                .where(
                    SubscriptionPackage.id.in_(purchased_package_ids)
                )
            )
        ).all()

        package_meta = {
            int(row.id): {
                "id": int(row.id),
                "title": row.title,
                "exam_stream": {
                    "id": int(row.exam_id),
                    "title": row.exam_title,
                    "code": row.exam_code,
                },
            }
            for row in package_rows
        }

        if package_meta:
            pt_rows = (
                await db.execute(
                    select(
                        package_tests_table.c.package_id,
                        package_tests_table.c.test_id,
                    ).where(
                        package_tests_table.c.package_id.in_(
                            set(package_meta.keys())
                        )
                    )
                )
            ).all()

            for package_id, test_id in pt_rows:
                if test_id is None:
                    continue
                meta = package_meta.get(int(package_id))
                if meta is not None:
                    package_by_test.setdefault(int(test_id), []).append(meta)

    def build_classification(test_id: int) -> dict:
        paths = taxonomy_by_test.get(test_id, [])
        exam = exam_by_test.get(test_id)

        # Prefer the academic hierarchy from the actual questions. If a test
        # has no questions yet, fall back to the Test.exam_id relation only.
        subjects_map: dict[int, dict] = {}

        for path in paths:
            subject = path["subject"]
            chapter = path["chapter"]
            topic = path["topic"]

            subject_node = subjects_map.setdefault(
                int(subject["id"]),
                {
                    "id": int(subject["id"]),
                    "name": subject["name"],
                    "chapters": {},
                },
            )

            chapter_node = subject_node["chapters"].setdefault(
                int(chapter["id"]),
                {
                    "id": int(chapter["id"]),
                    "name": chapter["name"],
                    "topics": {},
                },
            )

            chapter_node["topics"][int(topic["id"])] = {
                "id": int(topic["id"]),
                "name": topic["name"],
            }

        subjects = []
        for subject in subjects_map.values():
            chapters = []
            for chapter in subject["chapters"].values():
                chapters.append(
                    {
                        "id": chapter["id"],
                        "name": chapter["name"],
                        "topics": list(chapter["topics"].values()),
                    }
                )
            subjects.append(
                {
                    "id": subject["id"],
                    "name": subject["name"],
                    "chapters": chapters,
                }
            )

        packages = package_by_test.get(test_id, [])

        return {
            "exam_stream": exam,
            "packages": packages,
            "subjects": subjects,
            "has_topic_classification": bool(subjects),
        }

    classifications = {
        int(test.id): build_classification(int(test.id))
        for test in all_tests
    }

    enrolled_tests = []
    store_catalog = []

    # -------------------------------------------------------------------------
    # 7. Build the catalog strictly from database Test records
    # -------------------------------------------------------------------------
    for test in all_tests:
        test_id = int(test.id)

        item = {
            "id": test_id,
            "title": test.title,
            "duration_minutes": getattr(
                test,
                "duration_minutes",
                None,
            ),
            "total_marks": getattr(
                test,
                "total_marks",
                None,
            ),
            "is_enrolled": test_id in enrolled_ids,
            "classification": classifications.get(
                test_id,
                {
                    "exam_stream": None,
                    "packages": [],
                    "subjects": [],
                    "has_topic_classification": False,
                },
            ),
        }

        if test_id in enrolled_ids:
            enrolled_tests.append(item)
        else:
            store_catalog.append(item)

    # Attach the same real hierarchy to every attempt record.
    for history_item in history:
        history_item["classification"] = classifications.get(
            int(history_item["test_id"]),
            {
                "exam_stream": None,
                "packages": [],
                "subjects": [],
                "has_topic_classification": False,
            },
        )

    # -------------------------------------------------------------------------
    # 8. Return database-backed dashboard data
    # -------------------------------------------------------------------------
    return {
        "student": {
            "id": current_user.id,
            "full_name": current_user.full_name or "Candidate",
            "email": current_user.email,
        },
        "total_attempts": total_attempts,
        "tests_completed": completed_count,
        "average_score_percentage": avg_pct,
        # No fabricated accuracy value. The API only exposes a real attempt
        # accuracy when the model actually provides one.
        "overall_accuracy": None,
        "history": history,
        "enrolled_tests": enrolled_tests,
        "store_catalog": store_catalog,
    }


@router.get("/attempt/{attempt_id}/pdf")
async def download_attempt_pdf(
    attempt_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Stream official cryptographically sealed PDF scorecard."""
    stmt = select(TestAttempt).where(
        TestAttempt.id == attempt_id,
        TestAttempt.user_id == current_user.id,
    )
    attempt = (await db.execute(stmt)).scalar_one_or_none()
    if not attempt:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Examination attempt record not found",
        )

    test_stmt = select(Test).where(Test.id == attempt.test_id)
    test = (await db.execute(test_stmt)).scalar_one_or_none()
    if not test:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Associated assessment definition not found",
        )

    rank_info = None
    try:
        rank_info = await LeaderboardService.get_user_rank(attempt.test_id, current_user.id)
    except Exception:
        pass

    pdf_buffer = PDFCertificateService.generate_scorecard(
        attempt=attempt,
        user=current_user,
        test=test,
        rank_info=rank_info,
    )

    filename = f"Accqudo_Scorecard_Attempt_{attempt_id}.pdf"
    return StreamingResponse(
        pdf_buffer,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Access-Control-Expose-Headers": "Content-Disposition",
        },
    )


@router.get("/verify/attempt/{attempt_id}")
async def verify_attempt_public(
    attempt_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Public verification endpoint for QR-code authenticated scorecards."""
    stmt = select(TestAttempt).where(TestAttempt.id == attempt_id)
    attempt = (await db.execute(stmt)).scalar_one_or_none()
    if not attempt:
        raise HTTPException(status_code=404, detail="Certificate not found or invalid.")

    test_stmt = select(Test).where(Test.id == attempt.test_id)
    test = (await db.execute(test_stmt)).scalar_one_or_none()

    submitted_at = getattr(attempt, "submitted_at", None) or getattr(attempt, "end_time", None)

    return {
        "verified": True,
        "attempt_id": attempt.id,
        "test_title": test.title if test else "N/A",
        "candidate_id": attempt.user_id,
        "score": attempt.total_score,
        "status": getattr(attempt, "status", "UNKNOWN"),
        "submitted_at": submitted_at,
    }