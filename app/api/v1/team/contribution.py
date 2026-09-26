"""
Accqudo Team Contribution Dashboard.

GET /api/v1/team/contribution

Only authenticated ADMIN, TEAM, and SUPER_ADMIN users can access this endpoint.
It returns only the authenticated user's own contribution data.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User

router = APIRouter(prefix="/team/contribution", tags=["Team - Contribution"])
ALLOWED_ROLES = {"ADMIN", "SUPER_ADMIN", "SUPERADMIN", "TEAM"}


def norm(value: Any) -> str:
    if value is None:
        return ""
    return str(getattr(value, "value", value)).strip().upper()


def iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if isinstance(value, datetime) else str(value)


async def rows(db: AsyncSession, sql: str, params: dict[str, Any] | None = None):
    result = await db.execute(text(sql), params or {})
    return [dict(r) for r in result.mappings().all()]


async def one(db: AsyncSession, sql: str, params: dict[str, Any] | None = None):
    result = await db.execute(text(sql), params or {})
    return result.mappings().first()


@router.get("")
async def contribution_dashboard(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if norm(getattr(current_user, "role", None)) not in ALLOWED_ROLES:
        raise HTTPException(403, "Only ADMIN,TEAM and SUPER_ADMIN can access contributions.")

    user_id = int(current_user.id)

    user_row = await one(db, """
        SELECT id, full_name, email, role FROM users WHERE id = :user_id
    """, {"user_id": user_id})

    counts = await one(db, """
        SELECT
          (SELECT COUNT(*) FROM subjects WHERE created_by=:uid) AS subjects,
          (SELECT COUNT(*) FROM chapters WHERE created_by=:uid) AS chapters,
          (SELECT COUNT(*) FROM topics WHERE created_by=:uid) AS topics,
          (SELECT COUNT(*) FROM questions WHERE created_by=:uid) AS questions,
          (SELECT COUNT(DISTINCT package_id) FROM package_tests WHERE created_by=:uid) AS packages,
          (SELECT COUNT(*) FROM tests WHERE created_by=:uid) AS papers_assembled,
          (SELECT COUNT(*) FROM test_questions WHERE added_by=:uid) AS questions_added_to_papers
    """, {"uid": user_id})

    # Direct question contributions and their full academic hierarchy.
    qrows = await rows(db, """
        SELECT q.id AS question_id, q.question_type, q.created_at,
               tp.id AS topic_id, tp.name AS topic_name,
               c.id AS chapter_id, c.name AS chapter_name,
               s.id AS subject_id, s.name AS subject_name,
               e.id AS exam_id, e.title AS exam_title, e.code AS exam_code
        FROM questions q
        JOIN topics tp ON tp.id=q.topic_id
        JOIN chapters c ON c.id=tp.chapter_id
        JOIN subjects s ON s.id=c.subject_id
        LEFT JOIN exams e ON e.id=s.exam_id
        WHERE q.created_by=:uid
        ORDER BY e.title, s.name, c.name, tp.name, q.id DESC
    """, {"uid": user_id})

    # Papers assembled by this user.
    papers = await rows(db, """
        SELECT t.id, t.title, t.exam_id, e.title AS exam_title, e.code AS exam_code,
               t.duration_minutes, t.total_marks, t.created_at,
               (SELECT COUNT(*) FROM test_questions tq WHERE tq.test_id=t.id) AS total_questions,
               (SELECT COUNT(*) FROM test_questions tq WHERE tq.test_id=t.id AND tq.added_by=:uid) AS questions_added_by_me
        FROM tests t
        LEFT JOIN exams e ON e.id=t.exam_id
        WHERE t.created_by=:uid
        ORDER BY t.created_at DESC, t.id DESC
    """, {"uid": user_id})

    # Questions the user added to papers assembled by anyone.
    additions = await rows(db, """
        SELECT tq.id AS test_question_id, tq.test_id, tq.question_id, tq.`order`,
               tq.marks, tq.negative_marks, t.title AS test_title,
               tp.id AS topic_id, tp.name AS topic_name,
               c.id AS chapter_id, c.name AS chapter_name,
               s.id AS subject_id, s.name AS subject_name
        FROM test_questions tq
        JOIN tests t ON t.id=tq.test_id
        JOIN questions q ON q.id=tq.question_id
        JOIN topics tp ON tp.id=q.topic_id
        JOIN chapters c ON c.id=tp.chapter_id
        JOIN subjects s ON s.id=c.subject_id
        WHERE tq.added_by=:uid
        ORDER BY tq.test_id DESC, tq.`order`
    """, {"uid": user_id})

    # Package -> test relations attributed to the current user.
    #
    # IMPORTANT:
    # `created_by` is stored on package_tests in the current database.
    # We therefore scope the complete package section by pt.created_by.
    # The packages table is used only for package metadata (title/exam_id).
    package_tests = await rows(db, """
        SELECT DISTINCT
               pt.package_id,
               pt.test_id,
               p.title AS package_title,
               p.exam_id AS package_exam_id,
               t.title AS test_title
        FROM package_tests pt
        JOIN packages p ON p.id=pt.package_id
        JOIN tests t ON t.id=pt.test_id
        WHERE pt.created_by=:uid
        ORDER BY pt.package_id, pt.test_id
    """, {"uid": user_id})

    # Direct hierarchy: subject -> chapter -> topic.
    topic_ids = sorted({int(r["topic_id"]) for r in qrows})
    total_by_topic: dict[int, int] = {}
    if topic_ids:
        placeholders = ",".join(f":t{i}" for i in range(len(topic_ids)))
        params = {f"t{i}": value for i, value in enumerate(topic_ids)}
        totals = await rows(db, f"""
            SELECT topic_id, COUNT(*) AS total_questions
            FROM questions
            WHERE topic_id IN ({placeholders})
            GROUP BY topic_id
        """, params)
        total_by_topic = {int(r["topic_id"]): int(r["total_questions"] or 0) for r in totals}

    subjects: dict[int, dict[str, Any]] = {}
    for r in qrows:
        sid, cid, tid, eid = map(int, (r["subject_id"], r["chapter_id"], r["topic_id"], r["exam_id"]))
        subject = subjects.setdefault(sid, {
            "id": sid, "name": r["subject_name"], "exam": {
                "id": eid, "title": r["exam_title"], "code": r["exam_code"]
            }, "contributed_questions": 0, "total_questions": 0, "chapters": {}
        })
        chapter = subject["chapters"].setdefault(cid, {
            "id": cid, "name": r["chapter_name"], "contributed_questions": 0,
            "total_questions": 0, "topics": {}
        })
        topic = chapter["topics"].setdefault(tid, {
            "id": tid, "name": r["topic_name"], "contributed_questions": 0,
            "total_questions": total_by_topic.get(tid, 0)
        })
        topic["contributed_questions"] += 1
        chapter["contributed_questions"] += 1
        chapter["total_questions"] += total_by_topic.get(tid, 0)
        subject["contributed_questions"] += 1
        subject["total_questions"] += total_by_topic.get(tid, 0)

    hierarchy = []
    for subject in subjects.values():
        chapters = []
        for chapter in subject["chapters"].values():
            chapter["topics"] = sorted(chapter["topics"].values(), key=lambda x: x["name"].lower())
            chapter["contribution_percent"] = round(100 * chapter["contributed_questions"] / chapter["total_questions"], 1) if chapter["total_questions"] else 0
            chapters.append(chapter)
        subject["chapters"] = sorted(chapters, key=lambda x: x["name"].lower())
        subject["contribution_percent"] = round(100 * subject["contributed_questions"] / subject["total_questions"], 1) if subject["total_questions"] else 0
        for chapter in subject["chapters"]:
            for topic in chapter["topics"]:
                topic["contribution_percent"] = round(100 * topic["contributed_questions"] / topic["total_questions"], 1) if topic["total_questions"] else 0
        hierarchy.append(subject)
    hierarchy.sort(key=lambda x: x["name"].lower())

    # Package -> paper -> user's contributed questions.
    package_nodes: dict[int, dict[str, Any]] = {}
    for r in package_tests:
        pid, tid = int(r["package_id"]), int(r["test_id"])
        package_nodes.setdefault(pid, {
            "id": pid, "title": r["package_title"], "exam_id": r["package_exam_id"], "papers": {}, "subjects": {}
        })
        package_nodes[pid]["papers"].setdefault(tid, {
            "id": tid, "title": r["test_title"], "contributed_questions": 0,
            "total_questions": 0, "questions": []
        })

    total_paper_rows = await rows(db, """
        SELECT pt.package_id, tq.test_id, COUNT(*) AS total_questions
        FROM package_tests pt
        JOIN test_questions tq ON tq.test_id=pt.test_id
        WHERE pt.created_by=:uid
        GROUP BY pt.package_id, tq.test_id
    """, {"uid": user_id})
    paper_totals = {(int(r["package_id"]), int(r["test_id"])): int(r["total_questions"] or 0) for r in total_paper_rows}

    contributed_paper_rows = await rows(db, """
        SELECT pt.package_id, tq.test_id, tq.question_id, tq.`order`,
               tp.id AS topic_id, tp.name AS topic_name,
               c.id AS chapter_id, c.name AS chapter_name,
               s.id AS subject_id, s.name AS subject_name
        FROM package_tests pt
        JOIN test_questions tq ON tq.test_id=pt.test_id
        JOIN questions q ON q.id=tq.question_id
        JOIN topics tp ON tp.id=q.topic_id
        JOIN chapters c ON c.id=tp.chapter_id
        JOIN subjects s ON s.id=c.subject_id
        WHERE pt.created_by=:uid
          AND q.created_by=:uid
        ORDER BY pt.package_id, tq.test_id, tq.`order`
    """, {"uid": user_id})

    for r in contributed_paper_rows:
        pid, tid = int(r["package_id"]), int(r["test_id"])
        paper = package_nodes.get(pid, {}).get("papers", {}).get(tid)
        if not paper:
            continue
        paper["contributed_questions"] += 1
        paper["total_questions"] = paper_totals.get((pid, tid), 0)
        paper["questions"].append({
            "id": int(r["question_id"]), "order": r["order"],
            "topic": {"id": int(r["topic_id"]), "name": r["topic_name"]},
            "chapter": {"id": int(r["chapter_id"]), "name": r["chapter_name"]},
            "subject": {"id": int(r["subject_id"]), "name": r["subject_name"]}
        })

    for package in package_nodes.values():
        package["papers"] = sorted(package["papers"].values(), key=lambda x: x["title"].lower())
        for paper in package["papers"]:
            paper["contribution_percent"] = round(100 * paper["contributed_questions"] / paper["total_questions"], 1) if paper["total_questions"] else 0
        package["paper_count"] = len(package["papers"])
        package["contributed_questions"] = sum(p["contributed_questions"] for p in package["papers"])
        package["total_questions"] = sum(p["total_questions"] for p in package["papers"])
        package["contribution_percent"] = round(100 * package["contributed_questions"] / package["total_questions"], 1) if package["total_questions"] else 0

    # Package -> Subject -> Chapter -> Topic contribution tree.
    # Counts are DISTINCT question IDs so the same question repeated across
    # two papers in one package is not double-counted.
    package_topic_rows = await rows(db, """
        SELECT
            pt.package_id,
            s.id AS subject_id, s.name AS subject_name,
            c.id AS chapter_id, c.name AS chapter_name,
            tp.id AS topic_id, tp.name AS topic_name,
            COUNT(DISTINCT q.id) AS total_questions,
            COUNT(DISTINCT CASE WHEN q.created_by = :uid THEN q.id END) AS contributed_questions
        FROM package_tests pt
        JOIN test_questions tq ON tq.test_id = pt.test_id
        JOIN questions q ON q.id = tq.question_id
        JOIN topics tp ON tp.id = q.topic_id
        JOIN chapters c ON c.id = tp.chapter_id
        JOIN subjects s ON s.id = c.subject_id
        WHERE pt.created_by = :uid
        GROUP BY pt.package_id, s.id, s.name, c.id, c.name, tp.id, tp.name
        ORDER BY pt.package_id, s.name, c.name, tp.name
    """, {"uid": user_id})

    for r in package_topic_rows:
        pid = int(r["package_id"])
        package = package_nodes.get(pid)
        if package is None:
            # Ignore orphan package-test rows instead of creating a fake package.
            # Every package shown by this endpoint must come from the database.
            continue
        package.setdefault("subjects", {})
        sid, cid, tid = int(r["subject_id"]), int(r["chapter_id"]), int(r["topic_id"])
        subject = package["subjects"].setdefault(sid, {
            "id": sid, "name": r["subject_name"], "contributed_questions": 0,
            "total_questions": 0, "chapters": {}
        })
        chapter = subject["chapters"].setdefault(cid, {
            "id": cid, "name": r["chapter_name"], "contributed_questions": 0,
            "total_questions": 0, "topics": {}
        })
        total = int(r["total_questions"] or 0)
        contributed = int(r["contributed_questions"] or 0)
        chapter["topics"][tid] = {
            "id": tid, "name": r["topic_name"],
            "contributed_questions": contributed,
            "total_questions": total,
            "contribution_percent": round(100 * contributed / total, 1) if total else 0
        }
        chapter["contributed_questions"] += contributed
        chapter["total_questions"] += total
        subject["contributed_questions"] += contributed
        subject["total_questions"] += total

    for package in package_nodes.values():
        subject_list = []
        for subject in package.get("subjects", {}).values():
            chapter_list = []
            for chapter in subject["chapters"].values():
                chapter["topics"] = sorted(chapter["topics"].values(), key=lambda x: x["name"].lower())
                chapter["contribution_percent"] = round(100 * chapter["contributed_questions"] / chapter["total_questions"], 1) if chapter["total_questions"] else 0
                chapter_list.append(chapter)
            subject["chapters"] = sorted(chapter_list, key=lambda x: x["name"].lower())
            subject["contribution_percent"] = round(100 * subject["contributed_questions"] / subject["total_questions"], 1) if subject["total_questions"] else 0
            subject_list.append(subject)
        package["subjects"] = sorted(subject_list, key=lambda x: x["name"].lower())
        package["academic_contributed_questions"] = sum(s["contributed_questions"] for s in subject_list)
        package["academic_total_questions"] = sum(s["total_questions"] for s in subject_list)
        package["academic_contribution_percent"] = round(100 * package["academic_contributed_questions"] / package["academic_total_questions"], 1) if package["academic_total_questions"] else 0

    package_hierarchy = sorted(package_nodes.values(), key=lambda x: x["title"].lower())

    return {
        "user": {
            "id": user_id,
            "name": user_row["full_name"] if user_row else getattr(current_user, "full_name", None),
            "email": user_row["email"] if user_row else getattr(current_user, "email", None),
            "role": norm(user_row["role"] if user_row else getattr(current_user, "role", None)),
        },
        "summary": {
            "subjects": int(counts["subjects"] or 0),
            "chapters": int(counts["chapters"] or 0),
            "topics": int(counts["topics"] or 0),
            "questions": int(counts["questions"] or 0),
            "packages": int(counts["packages"] or 0),
            "papers_assembled": int(counts["papers_assembled"] or 0),
            "questions_added_to_papers": int(counts["questions_added_to_papers"] or 0),
        },
        "hierarchy": hierarchy,
        "packages": package_hierarchy,
        "papers": [
            {
                "id": int(r["id"]), "title": r["title"], "exam_id": r["exam_id"],
                "exam_title": r["exam_title"], "exam_code": r["exam_code"],
                "duration_minutes": r["duration_minutes"], "total_marks": r["total_marks"],
                "total_questions": int(r["total_questions"] or 0),
                "questions_added_by_me": int(r["questions_added_by_me"] or 0),
                "created_at": iso(r["created_at"])
            } for r in papers
        ],
        "collaborative_paper_additions": [
            {
                "test_id": int(r["test_id"]), "test_title": r["test_title"],
                "question_id": int(r["question_id"]), "order": r["order"],
                "marks": r["marks"], "negative_marks": r["negative_marks"],
                "topic": {"id": int(r["topic_id"]), "name": r["topic_name"]},
                "chapter": {"id": int(r["chapter_id"]), "name": r["chapter_name"]},
                "subject": {"id": int(r["subject_id"]), "name": r["subject_name"]}
            } for r in additions
        ],
        "generated_at": datetime.utcnow().isoformat()
    }
