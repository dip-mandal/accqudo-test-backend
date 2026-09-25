from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.api.v1.deps import require_team_or_admin
from app.models.user import User
from app.models.exam import Exam, Subject, Chapter, Topic
from app.models.question import Question, QuestionType
from app.models.test import Test, TestQuestion
from app.models.package import SubscriptionPackage, PackageTest


router = APIRouter(
    prefix="/team",
    tags=["Team & Authoring"],
)


# ============================================================
# EXAM HIERARCHY
# ============================================================

@router.get("/hierarchy")
async def get_exam_hierarchy(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_team_or_admin),
):
    """
    Return complete exam hierarchy:

    Exam
      └── Subject
            └── Chapter
                  └── Topic
    """

    stmt = (
        select(Exam)
        .options(
            selectinload(Exam.subjects)
            .selectinload(Subject.chapters)
            .selectinload(Chapter.topics)
        )
        .order_by(Exam.id)
    )

    result = await db.execute(stmt)
    exams = result.scalars().unique().all()

    return [
        {
            "id": exam.id,
            "title": exam.title,
            "code": exam.code,
            "subjects": [
                {
                    "id": subject.id,
                    "name": subject.name,
                    "chapters": [
                        {
                            "id": chapter.id,
                            "name": chapter.name,
                            "topics": [
                                {
                                    "id": topic.id,
                                    "name": topic.name,
                                }
                                for topic in chapter.topics
                            ],
                        }
                        for chapter in subject.chapters
                    ],
                }
                for subject in exam.subjects
            ],
        }
        for exam in exams
    ]


# ============================================================
# ALL PACKAGES
# ============================================================

@router.get("/packages/all")
async def get_all_packages(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_team_or_admin),
):
    """
    Return all subscription packages and their tests.
    """

    stmt = (
        select(SubscriptionPackage)
        .options(
            selectinload(SubscriptionPackage.tests)
        )
        .order_by(SubscriptionPackage.id.desc())
    )

    result = await db.execute(stmt)

    packages = result.scalars().unique().all()

    return [
        {
            "id": package.id,
            "exam_id": package.exam_id,
            "title": package.title,
            "description": package.description,
            "price_inr": (
                float(package.price_inr)
                if package.price_inr is not None
                else 0.0
            ),
            "validity_days": package.validity_days,
            "is_active": package.is_active,
            "total_tests": len(package.tests),
            "tests": [
                {
                    "id": test.id,
                    "title": test.title,
                    "duration_minutes": test.duration_minutes,
                    "total_marks": test.total_marks,
                }
                for test in package.tests
            ],
        }
        for package in packages
    ]


# ============================================================
# QUESTION SEARCH
# ============================================================

@router.get("/questions/search")
async def search_questions(
    limit: int = 50000,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_team_or_admin),
):
    """
    Return recent questions with their hierarchy information.
    """

    # Prevent unreasonable requests.
    limit = max(1, min(limit, 50000))

    stmt = (
        select(Question)
        .options(
            selectinload(Question.topic)
            .selectinload(Topic.chapter)
            .selectinload(Chapter.subject)
        )
        .order_by(Question.id.desc())
        .limit(limit)
    )

    result = await db.execute(stmt)

    questions = result.scalars().unique().all()

    return [
        {
            "id": question.id,
            "question_type": (
                question.question_type.value
                if hasattr(question.question_type, "value")
                else str(question.question_type)
            ),
            "question_text": question.question_text,
            "default_marks": question.default_marks,
            "default_negative_marks": question.default_negative_marks,
            "topic_name": (
                question.topic.name
                if question.topic
                else None
            ),
            "chapter_name": (
                question.topic.chapter.name
                if question.topic
                and question.topic.chapter
                else None
            ),
            "subject_name": (
                question.topic.chapter.subject.name
                if question.topic
                and question.topic.chapter
                and question.topic.chapter.subject
                else None
            ),
        }
        for question in questions
    ]


# ============================================================
# CREATE QUESTION
# ============================================================

@router.post("/questions")
async def create_question(
    payload: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_team_or_admin),
):
    """Create a question in the team question bank."""
    topic_id = payload.get("topic_id")
    question_text = str(payload.get("question_text") or "").strip()
    question_type_value = str(payload.get("question_type") or "MCQ").upper().strip()

    if topic_id is None:
        raise HTTPException(status_code=400, detail="topic_id is required.")
    if not question_text:
        raise HTTPException(status_code=400, detail="question_text is required.")

    try:
        topic_id = int(topic_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="topic_id must be an integer.")

    topic_result = await db.execute(select(Topic).where(Topic.id == topic_id))
    if topic_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Selected topic was not found.")

    try:
        question_type = QuestionType(question_type_value)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid question_type: {question_type_value}. Use MCQ, MSQ, or NAT.",
        )

    try:
        marks = float(payload.get("default_marks", 1.0))
        negative_marks = float(payload.get("default_negative_marks", 0.0))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Marks must be numeric.")

    if marks <= 0:
        raise HTTPException(status_code=400, detail="default_marks must be greater than 0.")
    if negative_marks < 0:
        raise HTTPException(status_code=400, detail="default_negative_marks cannot be negative.")

    evaluation_data = payload.get("evaluation_data")
    if not isinstance(evaluation_data, dict):
        raise HTTPException(status_code=400, detail="evaluation_data must be a JSON object.")

    question = Question(
        topic_id=topic_id,
        question_type=question_type,
        question_text=question_text,
        options=payload.get("options"),
        evaluation_data=evaluation_data,
        solution_text=payload.get("solution_text"),
        default_marks=marks,
        default_negative_marks=negative_marks,
    )

    db.add(question)
    await db.flush()

    # The database has a created_by column, but the SQLAlchemy Question model
    # does not expose it. Write ownership with SQL so we do not change the
    # existing ORM model/schema.
    await db.execute(
        text("UPDATE questions SET created_by = :uid WHERE id = :qid"),
        {"uid": current_user.id, "qid": question.id},
    )

    await db.commit()
    await db.refresh(question)

    return {"question_id": question.id, "status": "created"}


# ============================================================
# ASSEMBLE TEST
# ============================================================

@router.post("/tests/assemble")
async def assemble_test(
    payload: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_team_or_admin),
):
    """Create a test and persist the exact marks configured in the paper canvas."""
    title = str(payload.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="Test title is required.")

    exam_id = payload.get("exam_id")
    if exam_id is None:
        raise HTTPException(status_code=400, detail="exam_id is required.")
    try:
        exam_id = int(exam_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="exam_id must be an integer.")

    exam_result = await db.execute(select(Exam).where(Exam.id == exam_id))
    if exam_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Selected exam was not found.")

    # Preferred payload: [{question_id, marks, negative_marks}]
    raw_questions = payload.get("questions")
    if raw_questions is None:
        raw_ids = payload.get("question_ids", [])
        raw_questions = [{"question_id": qid} for qid in raw_ids]

    if not isinstance(raw_questions, list) or not raw_questions:
        raise HTTPException(status_code=400, detail="At least one question is required.")

    normalized = []
    seen = set()
    for index, item in enumerate(raw_questions, start=1):
        if isinstance(item, dict):
            question_id = item.get("question_id", item.get("id"))
            marks_raw = item.get("marks")
            negative_raw = item.get("negative_marks")
        else:
            question_id = item
            marks_raw = None
            negative_raw = None

        try:
            question_id = int(question_id)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"Invalid question_id at position {index}.")

        if question_id in seen:
            raise HTTPException(status_code=400, detail=f"Question #{question_id} is selected more than once.")
        seen.add(question_id)
        normalized.append((question_id, marks_raw, negative_raw))

    question_ids = [item[0] for item in normalized]
    result = await db.execute(select(Question).where(Question.id.in_(question_ids)))
    questions = result.scalars().all()
    by_id = {question.id: question for question in questions}

    missing = [qid for qid in question_ids if qid not in by_id]
    if missing:
        raise HTTPException(
            status_code=404,
            detail={"message": "Some questions were not found.", "missing_question_ids": missing},
        )

    # Preserve canvas order and use canvas overrides when supplied.
    prepared = []
    total_marks = 0.0
    for question_id, marks_raw, negative_raw in normalized:
        question = by_id[question_id]
        try:
            marks = float(question.default_marks if marks_raw is None else marks_raw)
            negative_marks = float(
                question.default_negative_marks if negative_raw is None else negative_raw
            )
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"Invalid marks for question #{question_id}.")

        if marks <= 0:
            raise HTTPException(status_code=400, detail=f"Marks for question #{question_id} must be greater than 0.")
        if negative_marks < 0:
            raise HTTPException(status_code=400, detail=f"Negative marks for question #{question_id} cannot be negative.")

        prepared.append((question_id, marks, negative_marks))
        total_marks += marks

    try:
        duration_minutes = int(payload.get("duration_minutes", 180))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="duration_minutes must be an integer.")
    if duration_minutes <= 0:
        raise HTTPException(status_code=400, detail="duration_minutes must be greater than 0.")

    test = Test(
        exam_id=exam_id,
        title=title,
        duration_minutes=duration_minutes,
        total_marks=total_marks,
        instructions=payload.get("instructions") or {},
    )
    db.add(test)
    await db.flush()

    # The database has tests.created_by, but the ORM model does not expose it.
    await db.execute(
        text("UPDATE tests SET created_by = :uid WHERE id = :tid"),
        {"uid": current_user.id, "tid": test.id},
    )

    for order, (question_id, marks, negative_marks) in enumerate(prepared, start=1):
        test_question = TestQuestion(
            test_id=test.id,
            question_id=question_id,
            order=order,
            marks=marks,
            negative_marks=negative_marks,
        )
        db.add(test_question)
        await db.flush()
        # Attribute this paper addition to the authenticated user without
        # adding an unsupported field to the ORM model.
        await db.execute(
            text("UPDATE test_questions SET added_by = :uid WHERE id = :tqid"),
            {"uid": current_user.id, "tqid": test_question.id},
        )

    package_ids = payload.get("package_ids") or []
    if not isinstance(package_ids, list):
        raise HTTPException(status_code=400, detail="package_ids must be an array.")

    if package_ids:
        try:
            package_ids = list(dict.fromkeys(int(pid) for pid in package_ids))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="package_ids must contain integers.")

        package_result = await db.execute(
            select(SubscriptionPackage).where(SubscriptionPackage.id.in_(package_ids))
        )
        packages = package_result.scalars().all()
        packages_by_id = {package.id: package for package in packages}
        missing_packages = [pid for pid in package_ids if pid not in packages_by_id]
        if missing_packages:
            raise HTTPException(
                status_code=404,
                detail={
                    "message": "Some subscription packages were not found.",
                    "missing_package_ids": missing_packages,
                },
            )

        wrong_exam_packages = [
            pid for pid in package_ids if packages_by_id[pid].exam_id != exam_id
        ]
        if wrong_exam_packages:
            raise HTTPException(
                status_code=400,
                detail={
                    "message": "A package can only be attached to a test from the same exam.",
                    "invalid_package_ids": wrong_exam_packages,
                },
            )

        for package_id in package_ids:
            db.add(PackageTest(package_id=package_id, test_id=test.id))

    await db.commit()
    await db.refresh(test)

    return {
        "test_id": test.id,
        "title": test.title,
        "question_count": len(prepared),
        "total_marks": total_marks,
        "status": "created",
    }


# ============================================================
# CREATE EXAM
# ============================================================

@router.post("/exams")
async def create_exam(
    payload: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_team_or_admin),
):
    """
    Create an exam.
    """

    title = payload.get("title")
    code = payload.get("code")

    if not title:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Exam title is required.",
        )

    if not code:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Exam code is required.",
        )

    exam = Exam(
        title=title,
        code=code,
    )

    db.add(exam)

    await db.commit()
    await db.refresh(exam)

    return {
        "exam_id": exam.id,
        "title": exam.title,
        "code": exam.code,
    }


# ============================================================
# CREATE SUBJECT
# ============================================================

@router.post("/subjects")
async def create_subject(
    payload: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_team_or_admin),
):
    """
    Create a subject under an exam.
    """

    exam_id = payload.get("exam_id")
    name = payload.get("name")

    if exam_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="exam_id is required.",
        )

    if not name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Subject name is required.",
        )

    subject = Subject(
        exam_id=exam_id,
        name=name,
    )

    db.add(subject)
    await db.flush()
    await db.execute(
        text("UPDATE subjects SET created_by = :uid WHERE id = :sid"),
        {"uid": current_user.id, "sid": subject.id},
    )

    await db.commit()
    await db.refresh(subject)

    return {
        "subject_id": subject.id,
        "name": subject.name,
    }


# ============================================================
# CREATE CHAPTER
# ============================================================

@router.post("/chapters")
async def create_chapter(
    payload: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_team_or_admin),
):
    """
    Create a chapter under a subject.
    """

    subject_id = payload.get("subject_id")
    name = payload.get("name")

    if subject_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="subject_id is required.",
        )

    if not name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Chapter name is required.",
        )

    chapter = Chapter(
        subject_id=subject_id,
        name=name,
    )

    db.add(chapter)
    await db.flush()
    await db.execute(
        text("UPDATE chapters SET created_by = :uid WHERE id = :cid"),
        {"uid": current_user.id, "cid": chapter.id},
    )

    await db.commit()
    await db.refresh(chapter)

    return {
        "chapter_id": chapter.id,
        "name": chapter.name,
    }


# ============================================================
# CREATE TOPIC
# ============================================================

@router.post("/topics")
async def create_topic(
    payload: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_team_or_admin),
):
    """
    Create a topic under a chapter.
    """

    chapter_id = payload.get("chapter_id")
    name = payload.get("name")

    if chapter_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="chapter_id is required.",
        )

    if not name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Topic name is required.",
        )

    topic = Topic(
        chapter_id=chapter_id,
        name=name,
    )

    db.add(topic)
    await db.flush()
    await db.execute(
        text("UPDATE topics SET created_by = :uid WHERE id = :tid"),
        {"uid": current_user.id, "tid": topic.id},
    )

    await db.commit()
    await db.refresh(topic)

    return {
        "topic_id": topic.id,
        "name": topic.name,
    }