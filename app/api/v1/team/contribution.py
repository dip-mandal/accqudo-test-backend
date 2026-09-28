"""
Accqudo Team Progress / Intelligence API

Routes
------
GET  /api/v1/team/progress/dashboard?period=1m|3m|6m|1y|all
GET  /api/v1/team/progress/report/member/{user_id}?period=...
GET  /api/v1/team/progress/report/member/{user_id}/download?period=...
GET  /api/v1/team/progress/report/sales/download?period=...
GET  /api/v1/team/progress/report/sales/packages/download?period=...

This module is aligned with the existing Team Contribution and Package Sales
implementations supplied with the application. In particular, staff ownership
is read from the same fields already used by the contribution endpoint:

    subjects.created_by
    chapters.created_by
    topics.created_by
    questions.created_by
    tests.created_by
    test_questions.added_by
    package_tests.created_by

The relationship tables test_questions and package_tests do not have their own
creation timestamp in the supplied schema, so those two contribution metrics
cannot be truthfully filtered by period. They are reported as lifetime/current
counts and are explicitly marked as such.

Package sales follows the same CURRENT / LEGACY source selection used by the
existing sells endpoint:

    CURRENT: packages -> payments -> subscriptions
    LEGACY: subscription_packages -> razorpay_orders -> user_subscriptions

No schema migration is performed here.
"""

from __future__ import annotations

import csv
import io
import math
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    PageBreak,
    KeepTogether,
)

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User


router = APIRouter(prefix="/team/progress", tags=["Team - Progress"])

SUPER_ROLES = {"SUPER_ADMIN", "SUPERADMIN"}
STAFF_ROLES = {"ADMIN", "TEAM", "SUPER_ADMIN", "SUPERADMIN"}
SUCCESS_STATUSES = {"SUCCESS", "PAID", "CAPTURED", "COMPLETED"}


# -----------------------------------------------------------------------------
# Generic helpers
# -----------------------------------------------------------------------------


def normalize(value: Any) -> str:
    if value is None:
        return ""
    return str(getattr(value, "value", value)).strip().upper()


def role_label(value: Any) -> str:
    return normalize(value).replace("_", " ")


def money(value: Any) -> float:
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def to_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    raw = str(value).strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except (TypeError, ValueError):
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            pass
    return None


async def rows(
    db: AsyncSession,
    sql: str,
    params: Optional[dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    result = await db.execute(text(sql), params or {})
    return [dict(row) for row in result.mappings().all()]


async def one(
    db: AsyncSession,
    sql: str,
    params: Optional[dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    result = await db.execute(text(sql), params or {})
    row = result.mappings().first()
    return dict(row) if row else None


def build_csv(headers: List[str], records: List[Dict[str, Any]]) -> io.BytesIO:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=headers, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(records)
    data = io.BytesIO(output.getvalue().encode("utf-8-sig"))
    data.seek(0)
    return data


def period_start(period: str) -> Optional[datetime]:
    period = normalize(period).lower()
    now = datetime.utcnow()
    if period == "all":
        return None
    months = {"1m": 1, "3m": 3, "6m": 6, "1y": 12}.get(period)
    if months is None:
        raise HTTPException(400, "period must be one of: 1m, 3m, 6m, 1y, all")
    month_index = now.year * 12 + (now.month - 1) - (months - 1)
    year = month_index // 12
    month = month_index % 12 + 1
    return datetime(year, month, 1)


def period_meta(period: str, start: Optional[datetime]) -> Dict[str, Any]:
    return {
        "key": period,
        "label": {
            "1m": "1 Month",
            "3m": "3 Months",
            "6m": "6 Months",
            "1y": "1 Year",
            "all": "Full Time",
        }[period],
        "start_date": iso(start),
        "end_date": datetime.utcnow().isoformat(),
    }


def date_condition(column: str, start: Optional[datetime], parameter: str) -> tuple[str, dict[str, Any]]:
    if start is None:
        return "", {}
    return f" AND {column} >= :{parameter}", {parameter: start}


async def require_super_admin(current_user: User) -> None:
    if normalize(getattr(current_user, "role", None)) not in SUPER_ROLES:
        raise HTTPException(403, "Only SUPER_ADMIN can access team progress.")


# -----------------------------------------------------------------------------
# Staff contribution - intentionally mirrors the working contribution endpoint
# -----------------------------------------------------------------------------


async def get_staff_users(db: AsyncSession) -> List[Dict[str, Any]]:
    return await rows(
        db,
        """
        SELECT id, full_name, email, role, is_active, created_at
        FROM users
        WHERE UPPER(role) IN ('ADMIN','TEAM','SUPER_ADMIN','SUPERADMIN')
        ORDER BY
          CASE WHEN UPPER(role) IN ('SUPER_ADMIN','SUPERADMIN') THEN 0
               WHEN UPPER(role)='ADMIN' THEN 1 ELSE 2 END,
          full_name, email, id
        """,
    )


async def get_staff_activity(db: AsyncSession, start: Optional[datetime]) -> List[Dict[str, Any]]:
    """Return period-aware staff contribution using the same ownership fields
    as /team/contribution. Relationship-table counts are lifetime because those
    tables have no creation timestamp.
    """
    users = await get_staff_users(db)
    for u in users:
        uid = int(u["id"])
        params: Dict[str, Any] = {"uid": uid}

        fields = {
            "questions_created": ("questions", "created_by", "created_at"),
            "topics_created": ("topics", "created_by", "created_at"),
            "chapters_created": ("chapters", "created_by", "created_at"),
            "subjects_created": ("subjects", "created_by", "created_at"),
            "papers_assembled": ("tests", "created_by", "created_at"),
        }
        for key, (table, owner, created) in fields.items():
            condition = ""
            if start is not None:
                condition = f" AND {created} >= :{key}_start"
                params[f"{key}_start"] = start
            row = await one(
                db,
                f"SELECT COUNT(*) AS total FROM {table} WHERE {owner}=:uid{condition}",
                params,
            )
            u[key] = int(row["total"] or 0) if row else 0

        # These two are exactly the ownership columns used by the existing
        # contribution endpoint. Neither table has created_at.
        row = await one(
            db,
            "SELECT COUNT(*) AS total FROM test_questions WHERE added_by=:uid",
            {"uid": uid},
        )
        u["questions_added_to_papers"] = int(row["total"] or 0) if row else 0

        row = await one(
            db,
            "SELECT COUNT(*) AS total FROM package_tests WHERE created_by=:uid",
            {"uid": uid},
        )
        u["package_paper_links"] = int(row["total"] or 0) if row else 0

        row = await one(
            db,
            "SELECT COUNT(DISTINCT package_id) AS total FROM package_tests WHERE created_by=:uid",
            {"uid": uid},
        )
        u["packages_linked"] = int(row["total"] or 0) if row else 0

        u["relationship_counts_scope"] = "lifetime"

    users.sort(
        key=lambda x: (
            -int(x.get("questions_created", 0)),
            -int(x.get("papers_assembled", 0)),
            -int(x.get("questions_added_to_papers", 0)),
            -int(x.get("package_paper_links", 0)),
            str(x.get("full_name") or x.get("email") or "").lower(),
            int(x["id"]),
        )
    )
    return users


async def get_global_content_totals(db: AsyncSession, start: Optional[datetime]) -> Dict[str, int]:
    result = {
        "questions_created": 0,
        "topics_created": 0,
        "chapters_created": 0,
        "subjects_created": 0,
        "papers_assembled": 0,
        "questions_added_to_papers": 0,
        "package_paper_links": 0,
        "packages_linked": 0,
    }

    for key, table in (
        ("questions_created", "questions"),
        ("topics_created", "topics"),
        ("chapters_created", "chapters"),
        ("subjects_created", "subjects"),
        ("papers_assembled", "tests"),
    ):
        condition = ""
        params: Dict[str, Any] = {}
        if start is not None:
            condition = " AND created_at >= :start_date"
            params["start_date"] = start
        row = await one(db, f"SELECT COUNT(*) AS total FROM {table} WHERE 1=1{condition}", params)
        result[key] = int(row["total"] or 0) if row else 0

    # No timestamp exists on these relationship tables. Always report their
    # actual current totals; the response marks them as lifetime metrics.
    row = await one(db, "SELECT COUNT(*) AS total FROM test_questions")
    result["questions_added_to_papers"] = int(row["total"] or 0) if row else 0
    row = await one(db, "SELECT COUNT(*) AS total FROM package_tests")
    result["package_paper_links"] = int(row["total"] or 0) if row else 0
    row = await one(db, "SELECT COUNT(DISTINCT package_id) AS total FROM package_tests")
    result["packages_linked"] = int(row["total"] or 0) if row else 0
    return result


async def get_monthly_activity(db: AsyncSession, start: Optional[datetime]) -> List[Dict[str, Any]]:
    """Monthly activity only for tables with a reliable created_at field."""
    parts: List[str] = []
    params: Dict[str, Any] = {}
    sources = [
        ("questions", "q", "questions"),
        ("topics", "tp", "topics"),
        ("chapters", "c", "chapters"),
        ("subjects", "s", "subjects"),
        ("tests", "t", "papers"),
    ]
    for i, (table, alias, metric) in enumerate(sources):
        where = ""
        if start is not None:
            name = f"month_start_{i}"
            where = f"WHERE {alias}.created_at >= :{name}"
            params[name] = start
        zeros = {m: "0" for _, _, m in sources}
        zeros[metric] = "COUNT(*)"
        select = ", ".join(
            [f"{zeros[m]} AS {m}" for _, _, m in sources]
        )
        parts.append(
            f"SELECT DATE_FORMAT({alias}.created_at, '%Y-%m') AS month, {select} "
            f"FROM {table} {alias} {where} GROUP BY DATE_FORMAT({alias}.created_at, '%Y-%m')"
        )
    sql = (
        "SELECT month, SUM(questions) questions, SUM(topics) topics, "
        "SUM(chapters) chapters, SUM(subjects) subjects, SUM(papers) papers "
        f"FROM ({' UNION ALL '.join(parts)}) activity GROUP BY month ORDER BY month"
    )
    data = await rows(db, sql, params)
    return [
        {
            "month": str(r["month"]),
            "questions": int(r["questions"] or 0),
            "topics": int(r["topics"] or 0),
            "chapters": int(r["chapters"] or 0),
            "subjects": int(r["subjects"] or 0),
            "papers": int(r["papers"] or 0),
            "question_additions": 0,
            "package_links": 0,
        }
        for r in data
    ]


async def get_member_details(
    db: AsyncSession,
    user_id: int,
    start: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Return the complete attributable work of one staff member.

    The academic breakdown intentionally follows the exact ownership fields
    used by the working team/contribution endpoint supplied with the project.
    Questions and papers can be period-filtered because they have created_at;
    test_questions.added_by and package_tests.created_by are lifetime/current
    relationship metrics because those tables have no reliable creation time.
    """
    q_condition, q_params = date_condition("q.created_at", start, "q_start")
    t_condition, t_params = date_condition("t.created_at", start, "t_start")

    questions = await rows(
        db,
        f"""
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
        WHERE q.created_by=:uid {q_condition}
        ORDER BY q.created_at DESC, q.id DESC
        """,
        {"uid": user_id, **q_params},
    )
    papers = await rows(
        db,
        f"""
        SELECT t.id, t.title, t.exam_id, e.title AS exam_title, e.code AS exam_code,
               t.duration_minutes, t.total_marks, t.created_at,
               (SELECT COUNT(*) FROM test_questions tq WHERE tq.test_id=t.id) AS total_questions,
               (SELECT COUNT(*) FROM test_questions tq WHERE tq.test_id=t.id AND tq.added_by=:uid) AS questions_added_by_me
        FROM tests t
        LEFT JOIN exams e ON e.id=t.exam_id
        WHERE t.created_by=:uid {t_condition}
        ORDER BY t.created_at DESC, t.id DESC
        """,
        {"uid": user_id, **t_params},
    )
    additions = await rows(
        db,
        """
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
        """,
        {"uid": user_id},
    )

    # Resolve package titles from either catalog. This keeps the report useful
    # across the current and legacy package implementations.
    package_links = await rows(
        db,
        """
        SELECT DISTINCT pt.package_id, pt.test_id,
               sp.title AS legacy_package_title, sp.exam_id AS legacy_package_exam_id,
               t.title AS test_title, t.exam_id AS test_exam_id
        FROM package_tests pt
        LEFT JOIN subscription_packages sp ON sp.id=pt.package_id
        JOIN tests t ON t.id=pt.test_id
        WHERE pt.created_by=:uid
        ORDER BY pt.package_id, pt.test_id
        """,
        {"uid": user_id},
    )
    package_ids = sorted({int(r["package_id"]) for r in package_links if r.get("package_id") is not None})
    package_meta: Dict[int, Dict[str, Any]] = {}
    if package_ids:
        placeholders = ",".join(f":pid_{i}" for i in range(len(package_ids)))
        params = {f"pid_{i}": pid for i, pid in enumerate(package_ids)}
        try:
            current_packages = await rows(
                db,
                f"""
                SELECT id, title, exam_id FROM packages WHERE id IN ({placeholders})
                """,
                params,
            )
            package_meta.update({int(r["id"]): {"title": r.get("title"), "exam_id": r.get("exam_id")} for r in current_packages})
        except Exception:
            pass
        for r in package_links:
            pid = int(r["package_id"])
            if pid not in package_meta:
                package_meta[pid] = {
                    "title": r.get("legacy_package_title"),
                    "exam_id": r.get("legacy_package_exam_id"),
                }

    for r in package_links:
        pid = int(r["package_id"])
        meta = package_meta.get(pid, {})
        r["package_title"] = meta.get("title") or r.get("legacy_package_title") or f"Package #{pid}"
        r["package_exam_id"] = meta.get("exam_id") or r.get("legacy_package_exam_id")

    # Complete academic hierarchy for questions authored by the selected user.
    hierarchy_map: Dict[int, Dict[str, Any]] = {}
    for r in questions:
        eid = int(r["exam_id"]) if r.get("exam_id") is not None else None
        sid, cid, tid = int(r["subject_id"]), int(r["chapter_id"]), int(r["topic_id"])
        exam_key = eid if eid is not None else 0
        exam = hierarchy_map.setdefault(exam_key, {
            "id": eid, "title": r.get("exam_title") or "Unassigned Exam", "code": r.get("exam_code"),
            "contributed_questions": 0, "subjects": {},
        })
        subject = exam["subjects"].setdefault(sid, {
            "id": sid, "name": r["subject_name"], "contributed_questions": 0, "chapters": {},
        })
        chapter = subject["chapters"].setdefault(cid, {
            "id": cid, "name": r["chapter_name"], "contributed_questions": 0, "topics": {},
        })
        topic = chapter["topics"].setdefault(tid, {
            "id": tid, "name": r["topic_name"], "contributed_questions": 0,
        })
        topic["contributed_questions"] += 1
        chapter["contributed_questions"] += 1
        subject["contributed_questions"] += 1
        exam["contributed_questions"] += 1

    # Denominators: all contributor-attributed questions in the same academic
    # nodes, matching the contribution backend's anti-import inflation rule.
    topic_ids = sorted({int(r["topic_id"]) for r in questions})
    total_by_topic: Dict[int, int] = {}
    if topic_ids:
        placeholders = ",".join(f":tid_{i}" for i in range(len(topic_ids)))
        params = {f"tid_{i}": tid for i, tid in enumerate(topic_ids)}
        condition = ""
        if start is not None:
            condition = " AND created_at >= :topic_start"
            params["topic_start"] = start
        totals = await rows(
            db,
            f"""
            SELECT topic_id, COUNT(*) AS total_questions
            FROM questions
            WHERE topic_id IN ({placeholders}) AND created_by IS NOT NULL {condition}
            GROUP BY topic_id
            """,
            params,
        )
        total_by_topic = {int(r["topic_id"]): int(r["total_questions"] or 0) for r in totals}

    academic_breakdown: List[Dict[str, Any]] = []
    for exam in hierarchy_map.values():
        subject_list = []
        exam_total = 0
        for subject in exam["subjects"].values():
            chapter_list = []
            subject_total = 0
            for chapter in subject["chapters"].values():
                topic_list = []
                chapter_total = 0
                for topic in chapter["topics"].values():
                    total = total_by_topic.get(int(topic["id"]), 0)
                    topic["total_questions"] = total
                    topic["contribution_percent"] = round(100 * topic["contributed_questions"] / total, 1) if total else 0.0
                    chapter_total += total
                    topic_list.append(topic)
                chapter["topics"] = sorted(topic_list, key=lambda x: x["name"].lower())
                chapter["total_questions"] = chapter_total
                chapter["contribution_percent"] = round(100 * chapter["contributed_questions"] / chapter_total, 1) if chapter_total else 0.0
                subject_total += chapter_total
                chapter_list.append(chapter)
            subject["chapters"] = sorted(chapter_list, key=lambda x: x["name"].lower())
            subject["total_questions"] = subject_total
            subject["contribution_percent"] = round(100 * subject["contributed_questions"] / subject_total, 1) if subject_total else 0.0
            exam_total += subject_total
            subject_list.append(subject)
        exam["subjects"] = sorted(subject_list, key=lambda x: x["name"].lower())
        exam["total_questions"] = exam_total
        exam["contribution_percent"] = round(100 * exam["contributed_questions"] / exam_total, 1) if exam_total else 0.0
        academic_breakdown.append(exam)
    academic_breakdown.sort(key=lambda x: str(x["title"]).lower())

    by_type: Dict[str, int] = defaultdict(int)
    by_exam: Dict[str, int] = defaultdict(int)
    by_subject: Dict[str, int] = defaultdict(int)
    by_chapter: Dict[str, int] = defaultdict(int)
    by_topic: Dict[str, int] = defaultdict(int)
    for r in questions:
        by_type[normalize(r.get("question_type")) or "UNKNOWN"] += 1
        by_exam[r.get("exam_title") or "Unassigned Exam"] += 1
        by_subject[r.get("subject_name") or "Unknown Subject"] += 1
        by_chapter[r.get("chapter_name") or "Unknown Chapter"] += 1
        by_topic[r.get("topic_name") or "Unknown Topic"] += 1

    # Package-level contribution: every package-test relation created by the
    # member, plus distinct questions authored by the member inside those tests.
    package_contrib_rows = await rows(
        db,
        """
        SELECT pt.package_id, pt.test_id,
               COUNT(DISTINCT tq.question_id) AS total_questions,
               COUNT(DISTINCT CASE WHEN q.created_by=:uid THEN q.id END) AS contributed_questions
        FROM package_tests pt
        JOIN test_questions tq ON tq.test_id=pt.test_id
        JOIN questions q ON q.id=tq.question_id
        WHERE pt.created_by=:uid
        GROUP BY pt.package_id, pt.test_id
        ORDER BY pt.package_id, pt.test_id
        """,
        {"uid": user_id},
    )
    package_contributions: Dict[int, Dict[str, Any]] = {}
    for r in package_contrib_rows:
        pid = int(r["package_id"])
        package_contributions.setdefault(pid, {
            "package_id": pid,
            "package_title": package_meta.get(pid, {}).get("title") or f"Package #{pid}",
            "exam_id": package_meta.get(pid, {}).get("exam_id"),
            "papers": [],
        })
        total = int(r["total_questions"] or 0)
        contributed = int(r["contributed_questions"] or 0)
        package_contributions[pid]["papers"].append({
            "test_id": int(r["test_id"]),
            "total_questions": total,
            "contributed_questions": contributed,
            "contribution_percent": round(100 * contributed / total, 1) if total else 0.0,
        })
    for item in package_contributions.values():
        item["papers"].sort(key=lambda x: x["test_id"])
        item["total_questions"] = sum(x["total_questions"] for x in item["papers"])
        item["contributed_questions"] = sum(x["contributed_questions"] for x in item["papers"])
        item["contribution_percent"] = round(100 * item["contributed_questions"] / item["total_questions"], 1) if item["total_questions"] else 0.0
    package_contributions_list = sorted(package_contributions.values(), key=lambda x: str(x["package_title"]).lower())

    return {
        "questions": questions,
        "papers": papers,
        "additions": additions,
        "package_links": package_links,
        "academic_breakdown": academic_breakdown,
        "question_breakdown": {
            "by_type": [{"label": k, "count": v} for k, v in sorted(by_type.items(), key=lambda x: (-x[1], x[0]))],
            "by_exam": [{"label": k, "count": v} for k, v in sorted(by_exam.items(), key=lambda x: (-x[1], x[0]))],
            "by_subject": [{"label": k, "count": v} for k, v in sorted(by_subject.items(), key=lambda x: (-x[1], x[0]))],
            "by_chapter": [{"label": k, "count": v} for k, v in sorted(by_chapter.items(), key=lambda x: (-x[1], x[0]))],
            "by_topic": [{"label": k, "count": v} for k, v in sorted(by_topic.items(), key=lambda x: (-x[1], x[0]))],
        },
        "package_contributions": package_contributions_list,
    }


# -----------------------------------------------------------------------------
# Sales - aligned with team/sells.py, but intentionally defensive around
# optional subscription columns because deployed database versions can differ.
# -----------------------------------------------------------------------------


async def get_table_count(db: AsyncSession, table: str) -> int:
    allowed = {"packages", "subscription_packages", "payments", "razorpay_orders", "subscriptions", "user_subscriptions"}
    if table not in allowed:
        raise ValueError(table)
    row = await one(db, f"SELECT COUNT(*) AS total FROM `{table}`")
    return int(row["total"] or 0) if row else 0


async def detect_sales_source(db: AsyncSession, start: Optional[datetime]) -> str:
    condition, params = date_condition("created_at", start, "sales_start")
    current = await one(
        db,
        f"""
        SELECT COUNT(*) AS total FROM payments
        WHERE UPPER(CAST(status AS CHAR)) IN ('SUCCESS','PAID','CAPTURED','COMPLETED')
        {condition}
        """,
        params,
    )
    if current and int(current["total"] or 0) > 0:
        return "current"
    legacy = await one(
        db,
        f"""
        SELECT COUNT(*) AS total FROM razorpay_orders
        WHERE package_id IS NOT NULL
          AND UPPER(CAST(status AS CHAR)) IN ('SUCCESS','PAID','CAPTURED','COMPLETED')
        {condition}
        """,
        params,
    )
    if legacy and int(legacy["total"] or 0) > 0:
        return "legacy"
    current_catalog = await get_table_count(db, "packages")
    return "current" if current_catalog > 0 else "legacy"


async def fetch_sales(db: AsyncSession, source: str, start: Optional[datetime]) -> List[Dict[str, Any]]:
    if source == "current":
        condition, params = date_condition("p.created_at", start, "sales_start")
        return await rows(
            db,
            f"""
            SELECT p.id, p.user_id, p.package_id,
                   p.razorpay_order_id AS order_id,
                   p.razorpay_payment_id AS payment_id,
                   p.amount_paise, p.status, p.created_at
            FROM payments p
            WHERE UPPER(CAST(p.status AS CHAR)) IN ('SUCCESS','PAID','CAPTURED','COMPLETED')
            {condition}
            ORDER BY p.created_at DESC, p.id DESC
            """,
            params,
        )
    condition, params = date_condition("r.created_at", start, "sales_start")
    return await rows(
        db,
        f"""
        SELECT r.id, r.user_id, r.package_id,
               r.razorpay_order_id AS order_id,
               r.razorpay_payment_id AS payment_id,
               r.amount_inr, r.status, r.created_at
        FROM razorpay_orders r
        WHERE r.package_id IS NOT NULL
          AND UPPER(CAST(r.status AS CHAR)) IN ('SUCCESS','PAID','CAPTURED','COMPLETED')
        {condition}
        ORDER BY r.created_at DESC, r.id DESC
        """,
        params,
    )


async def fetch_packages(db: AsyncSession, source: str) -> List[Dict[str, Any]]:
    """Read package catalog and normalize price to INR.

    Current ``packages.price_paise`` is stored in paise.
    Legacy ``subscription_packages.price`` is already stored in INR.
    """
    if source == "current":
        package_rows = await rows(
            db,
            """
            SELECT id, exam_id, title, description, price_paise, discount_paise,
                   expiry_type, validity_days, fixed_expiry_date, is_active,
                   created_at, updated_at
            FROM packages ORDER BY created_at DESC, id DESC
            """,
        )

        return [
            {
                **p,
                "price_inr": round(money(p.get("price_paise")) / 100.0, 2),
                "tier": None,
            }
            for p in package_rows
        ]

    package_rows = await rows(
        db,
        """
        SELECT id, exam_id, title, description, tier, price, validity_days,
               is_active, created_at
        FROM subscription_packages ORDER BY created_at DESC, id DESC
        """,
    )

    normalized: List[Dict[str, Any]] = []
    for p in package_rows:
        # IMPORTANT: the legacy sells implementation treats ``price`` as INR.
        price_inr = round(money(p.get("price")), 2)
        normalized.append({
            **p,
            "price_inr": price_inr,
            "price_paise": int(round(price_inr * 100)),
        })
    return normalized


async def fetch_subscription_rows(db: AsyncSession, source: str) -> List[Dict[str, Any]]:
    """Best-effort subscription read. Sales must never fail because an older
    subscription table version lacks one of the optional date columns."""
    if source == "current":
        queries = [
            "SELECT package_id, user_id, start_date, expiry_date, status FROM subscriptions",
            "SELECT package_id, user_id, status FROM subscriptions",
        ]
    else:
        queries = [
            "SELECT package_id, user_id, start_date, end_date, is_active FROM user_subscriptions",
            "SELECT package_id, user_id, is_active FROM user_subscriptions",
        ]
    for sql in queries:
        try:
            data = await rows(db, sql)
            normalized: List[Dict[str, Any]] = []
            for r in data:
                if source == "current":
                    normalized.append({
                        "package_id": r.get("package_id"),
                        "user_id": r.get("user_id"),
                        "start_date": r.get("start_date"),
                        "expiry_date": r.get("expiry_date"),
                        "status": normalize(r.get("status")),
                    })
                else:
                    normalized.append({
                        "package_id": r.get("package_id"),
                        "user_id": r.get("user_id"),
                        "start_date": r.get("start_date"),
                        "expiry_date": r.get("end_date"),
                        "status": "ACTIVE" if bool(r.get("is_active")) else "EXPIRED",
                    })
            return normalized
        except Exception:
            continue
    return []


async def get_sales_report(db: AsyncSession, start: Optional[datetime]) -> Dict[str, Any]:
    source = await detect_sales_source(db, start)
    packages = await fetch_packages(db, source)
    sales = await fetch_sales(db, source, start)
    subscriptions = await fetch_subscription_rows(db, source)

    package_map = {int(p["id"]): p for p in packages}
    user_ids = sorted({int(s["user_id"]) for s in sales if s.get("user_id") is not None})
    users: Dict[int, Dict[str, Any]] = {}
    if user_ids:
        placeholders = ",".join(f":uid_{i}" for i in range(len(user_ids)))
        user_rows = await rows(
            db,
            f"SELECT id, full_name, email FROM users WHERE id IN ({placeholders})",
            {f"uid_{i}": uid for i, uid in enumerate(user_ids)},
        )
        users = {int(r["id"]): r for r in user_rows}

    performance: Dict[int, Dict[str, Any]] = {}
    for p in packages:
        pid = int(p["id"])
        price = money(p.get("price_inr"))
        performance[pid] = {
            "package_id": pid,
            "package_title": p.get("title"),
            "description": p.get("description"),
            "exam_id": p.get("exam_id"),
            "tier": p.get("tier"),
            "is_active": bool(p.get("is_active")),
            "price_inr": round(price, 2),
            "sales": 0,
            "revenue": 0.0,
            "unique_buyers": set(),
            "subscriptions": 0,
            "active_subscriptions": 0,
            "expired_subscriptions": 0,
        }

    monthly: Dict[str, Dict[str, Any]] = {}
    for sale in sales:
        pid = int(sale["package_id"])
        if pid not in performance:
            performance[pid] = {
                "package_id": pid, "package_title": f"Package #{pid}",
                "description": None, "exam_id": None, "tier": None,
                "is_active": True, "price_inr": 0.0, "sales": 0,
                "revenue": 0.0, "unique_buyers": set(), "subscriptions": 0,
                "active_subscriptions": 0, "expired_subscriptions": 0,
            }
        amount = money(sale.get("amount_inr")) if source == "legacy" else money(sale.get("amount_paise")) / 100
        item = performance[pid]
        item["sales"] += 1
        item["revenue"] += amount
        if sale.get("user_id") is not None:
            item["unique_buyers"].add(int(sale["user_id"]))
        dt = to_datetime(sale.get("created_at"))
        if dt:
            key = dt.strftime("%Y-%m")
            monthly.setdefault(key, {"month": key, "sales": 0, "revenue": 0.0})
            monthly[key]["sales"] += 1
            monthly[key]["revenue"] += amount

    now = datetime.utcnow()
    for sub in subscriptions:
        pid = sub.get("package_id")
        if pid is None or int(pid) not in performance:
            continue
        pid = int(pid)
        performance[pid]["subscriptions"] += 1
        status = normalize(sub.get("status"))
        expiry = to_datetime(sub.get("expiry_date"))
        active = status == "ACTIVE" and (expiry is None or expiry >= now)
        if active:
            performance[pid]["active_subscriptions"] += 1
        else:
            performance[pid]["expired_subscriptions"] += 1

    # If there are no subscription rows, mirror the sells endpoint's payment
    # fallback: successful paying users are treated as enrollments.
    if not subscriptions:
        payment_users: Dict[int, set[int]] = defaultdict(set)
        for sale in sales:
            if sale.get("user_id") is not None:
                payment_users[int(sale["package_id"])].add(int(sale["user_id"]))
        for pid, uids in payment_users.items():
            if pid in performance:
                performance[pid]["subscriptions"] = len(uids)
                performance[pid]["active_subscriptions"] = len(uids)

    package_rows: List[Dict[str, Any]] = []
    for item in performance.values():
        item["revenue"] = round(float(item["revenue"]), 2)
        item["unique_buyers"] = len(item["unique_buyers"])
        package_rows.append(item)
    package_rows.sort(key=lambda x: (-float(x["revenue"]), -int(x["sales"]), str(x["package_title"]).lower()))

    recent_sales = []
    for sale in sales[:100]:
        pid = int(sale["package_id"])
        uid = int(sale["user_id"]) if sale.get("user_id") is not None else None
        user = users.get(uid) if uid is not None else None
        amount = money(sale.get("amount_inr")) if source == "legacy" else money(sale.get("amount_paise")) / 100
        recent_sales.append({
            "id": int(sale["id"]),
            "user_id": uid,
            "student_name": user.get("full_name") if user else None,
            "student_email": user.get("email") if user else None,
            "package_id": pid,
            "package_title": package_map.get(pid, {}).get("title", f"Package #{pid}"),
            "amount_inr": round(amount, 2),
            "status": normalize(sale.get("status")),
            "order_id": sale.get("order_id"),
            "payment_id": sale.get("payment_id"),
            "created_at": iso(sale.get("created_at")),
        })

    total_revenue = round(sum(float(x["revenue"]) for x in package_rows), 2)
    unique_buyers = len({int(s["user_id"]) for s in sales if s.get("user_id") is not None})
    active_students = len({
        int(s["user_id"]) for s in subscriptions
        if s.get("user_id") is not None
        and normalize(s.get("status")) == "ACTIVE"
        and (to_datetime(s.get("expiry_date")) is None or to_datetime(s.get("expiry_date")) >= now)
    })
    if not subscriptions:
        active_students = unique_buyers

    return {
        "source": source,
        "subscription_count_scope": "current-state" if subscriptions else "payment-fallback",
        "summary": {
            "revenue": total_revenue,
            "total_revenue": total_revenue,
            "successful_sales": len(sales),
            "sales": len(sales),
            "unique_buyers": unique_buyers,
            "active_students": active_students,
            "packages_with_sales": sum(1 for x in package_rows if x["sales"] > 0),
            "subscriptions": sum(int(x["subscriptions"]) for x in package_rows),
            "active_subscriptions": sum(int(x["active_subscriptions"]) for x in package_rows),
        },
        "packages": [
            {**x, "unique_buyers": int(x["unique_buyers"])} for x in package_rows
        ],
        "monthly": [
            {"month": k, "sales": int(v["sales"]), "revenue": round(float(v["revenue"]), 2)}
            for k, v in sorted(monthly.items())
        ],
        "revenue_trend": [
            {"month": k, "sales": int(v["sales"]), "revenue": round(float(v["revenue"]), 2)}
            for k, v in sorted(monthly.items())
        ],
        "recent_sales": recent_sales,
    }


# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------


@router.get("/dashboard")
async def progress_dashboard(
    period: str = Query(default="3m"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await require_super_admin(current_user)
    period = period.strip().lower()
    start = period_start(period)

    staff = await get_staff_activity(db, start)
    totals = await get_global_content_totals(db, start)
    monthly_activity = await get_monthly_activity(db, start)
    sales = await get_sales_report(db, start)

    total_question_created = totals["questions_created"]
    total_papers = totals["papers_assembled"]
    total_added = totals["questions_added_to_papers"]
    total_links = totals["package_paper_links"]

    for member in staff:
        member["id"] = int(member["id"])
        for key in (
            "questions_created", "topics_created", "chapters_created", "subjects_created",
            "papers_assembled", "questions_added_to_papers", "package_paper_links", "packages_linked",
        ):
            member[key] = int(member.get(key) or 0)
        member["question_share_percent"] = round(100 * member["questions_created"] / total_question_created, 1) if total_question_created else 0.0
        member["paper_share_percent"] = round(100 * member["papers_assembled"] / total_papers, 1) if total_papers else 0.0
        member["question_addition_share_percent"] = round(100 * member["questions_added_to_papers"] / total_added, 1) if total_added else 0.0
        member["package_link_share_percent"] = round(100 * member["package_paper_links"] / total_links, 1) if total_links else 0.0
        member["created_at"] = iso(member.get("created_at"))
        member["attribution_available"] = {
            "questions": True,
            "topics": True,
            "chapters": True,
            "subjects": True,
            "tests": True,
            "test_questions": True,
            "package_tests": True,
        }
        member["period_scopes"] = {
            "questions": "period" if start else "all",
            "topics": "period" if start else "all",
            "chapters": "period" if start else "all",
            "subjects": "period" if start else "all",
            "tests": "period" if start else "all",
            "test_questions": "lifetime",
            "package_tests": "lifetime",
        }
        member["attribution_note"] = (
            "Creator/addition ownership is read from the same fields used by the existing "
            "team contribution endpoint. test_questions.added_by and package_tests.created_by "
            "do not have timestamps, so their counts are lifetime rather than period-filtered."
        )

    return {
        "period": period_meta(period, start),
        "staff": staff,
        "team_totals": totals,
        "monthly_activity": monthly_activity,
        "sales": sales,
        "attribution": {
            "supported": [
                "subjects.created_by", "chapters.created_by", "topics.created_by",
                "questions.created_by", "tests.created_by", "test_questions.added_by",
                "package_tests.created_by",
            ],
            "period_limitations": {
                "test_questions": "No created_at column; count is lifetime.",
                "package_tests": "No created_at column; count is lifetime.",
            },
        },
        "generated_at": datetime.utcnow().isoformat(),
    }


async def build_member_report(db: AsyncSession, user_id: int, period: str) -> Dict[str, Any]:
    start = period_start(period)
    member = await one(
        db,
        """
        SELECT id, full_name, email, role, is_active, created_at
        FROM users
        WHERE id=:uid AND UPPER(role) IN ('ADMIN','TEAM','SUPER_ADMIN','SUPERADMIN')
        """,
        {"uid": user_id},
    )
    if not member:
        raise HTTPException(404, "Staff member not found.")

    staff = await get_staff_activity(db, start)
    totals = await get_global_content_totals(db, start)
    target = next((x for x in staff if int(x["id"]) == user_id), None)
    if target is None:
        raise HTTPException(404, "Staff activity not found.")

    details = await get_member_details(db, user_id, start)
    qrows_period = details["questions"]
    papers_period = details["papers"]
    additions = details["additions"]
    links = details["package_links"]

    # Relationship metrics are deliberately lifetime/current because their
    # source tables have no creation timestamp.
    summary = {
        "questions_created": len(qrows_period),
        "topics_created": target["topics_created"],
        "chapters_created": target["chapters_created"],
        "subjects_created": target["subjects_created"],
        "papers_assembled": len(papers_period),
        "questions_added_to_papers": len(additions),
        "package_paper_links": len(links),
        "packages_linked": target["packages_linked"],
    }

    return {
        "period": period_meta(period, start),
        "member": {**target, "created_at": iso(target.get("created_at"))},
        "summary": summary,
        "questions": [
            {
                "question_id": int(r["question_id"]),
                "question_type": r.get("question_type"),
                "created_at": iso(r.get("created_at")),
                "topic": {"id": int(r["topic_id"]), "name": r["topic_name"]},
                "chapter": {"id": int(r["chapter_id"]), "name": r["chapter_name"]},
                "subject": {"id": int(r["subject_id"]), "name": r["subject_name"]},
                "exam": {"id": r.get("exam_id"), "title": r.get("exam_title"), "code": r.get("exam_code")},
            }
            for r in qrows_period
        ],
        "papers": [
            {
                "id": int(r["id"]), "title": r["title"], "exam_id": r.get("exam_id"),
                "exam_title": r.get("exam_title"), "exam_code": r.get("exam_code"),
                "duration_minutes": r.get("duration_minutes"), "total_marks": r.get("total_marks"),
                "total_questions": int(r.get("total_questions") or 0),
                "questions_added_by_me": int(r.get("questions_added_by_me") or 0),
                "created_at": iso(r.get("created_at")),
            }
            for r in papers_period
        ],
        "collaborative_paper_additions": [
            {
                "test_question_id": int(r["test_question_id"]), "test_id": int(r["test_id"]),
                "test_title": r.get("test_title"), "question_id": int(r["question_id"]),
                "order": r.get("order"), "marks": r.get("marks"), "negative_marks": r.get("negative_marks"),
                "topic": {"id": int(r["topic_id"]), "name": r["topic_name"]},
                "chapter": {"id": int(r["chapter_id"]), "name": r["chapter_name"]},
                "subject": {"id": int(r["subject_id"]), "name": r["subject_name"]},
                "period_filter_applied": False,
            }
            for r in additions
        ],
        "package_links": [
            {
                "package_id": int(r["package_id"]), "package_title": r.get("package_title"),
                "test_id": int(r["test_id"]), "test_title": r.get("test_title"),
                "exam_id": r.get("package_exam_id"), "created_by": user_id,
                "period_filter_applied": False,
            }
            for r in links
        ],
        "academic_breakdown": details["academic_breakdown"],
        "question_breakdown": details["question_breakdown"],
        "package_contributions": details["package_contributions"],
        "scope": {
            "questions_and_papers": "period" if start else "all",
            "test_question_additions": "lifetime",
            "package_test_links": "lifetime",
            "note": "Questions and papers use created_at. Test-question additions and package-test links are lifetime/current because those relationship tables do not expose a creation timestamp.",
        },
        "team_totals": totals,
        "generated_at": datetime.utcnow().isoformat(),
    }


@router.get("/report/member/{user_id}")
async def member_progress_report(
    user_id: int,
    period: str = Query(default="3m"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await require_super_admin(current_user)
    period = period.strip().lower()
    return await build_member_report(db, user_id, period)


# -----------------------------------------------------------------------------
# Official PDF report generation
# -----------------------------------------------------------------------------


def _pdf_text(value: Any) -> str:
    text_value = "" if value is None else str(value)
    return (text_value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _pdf_styles():
    styles = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("AccTitle", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=18, leading=22, textColor=colors.HexColor("#16293F"), spaceAfter=4),
        "subtitle": ParagraphStyle("AccSubtitle", parent=styles["Normal"], fontName="Helvetica", fontSize=8.5, leading=12, textColor=colors.HexColor("#667085"), spaceAfter=2),
        "section": ParagraphStyle("AccSection", parent=styles["Heading2"], fontName="Helvetica-Bold", fontSize=11.5, leading=14, textColor=colors.HexColor("#16293F"), spaceBefore=8, spaceAfter=6),
        "body": ParagraphStyle("AccBody", parent=styles["BodyText"], fontName="Helvetica", fontSize=8.5, leading=12, textColor=colors.HexColor("#344054")),
        "small": ParagraphStyle("AccSmall", parent=styles["BodyText"], fontName="Helvetica", fontSize=7.2, leading=9.5, textColor=colors.HexColor("#667085")),
        "tiny": ParagraphStyle("AccTiny", parent=styles["BodyText"], fontName="Helvetica", fontSize=6.5, leading=8, textColor=colors.HexColor("#667085")),
        "metric": ParagraphStyle("AccMetric", parent=styles["BodyText"], fontName="Helvetica-Bold", fontSize=14, leading=16, textColor=colors.HexColor("#1F3A5C"), alignment=TA_CENTER),
        "metric_label": ParagraphStyle("AccMetricLabel", parent=styles["BodyText"], fontName="Helvetica-Bold", fontSize=6.5, leading=8, textColor=colors.HexColor("#667085"), alignment=TA_CENTER),
        "right": ParagraphStyle("AccRight", parent=styles["BodyText"], fontName="Helvetica", fontSize=7.5, leading=9, textColor=colors.HexColor("#667085"), alignment=TA_RIGHT),
    }


def _pdf_table(data: List[List[Any]], widths: Optional[List[float]] = None, header: bool = True, font_size: float = 7.2):
    converted = []
    for row in data:
        converted.append([x if isinstance(x, Paragraph) else Paragraph(_pdf_text(x), ParagraphStyle("cell", fontName="Helvetica", fontSize=font_size, leading=font_size+2, textColor=colors.HexColor("#344054"))) for x in row])
    table = Table(converted, colWidths=widths, repeatRows=1 if header else 0, hAlign="LEFT")
    style = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#E4E7EC")),
    ]
    if header and converted:
        style += [
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F2F4F7")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#344054")),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ]
    table.setStyle(TableStyle(style))
    return table


class _AccqudoPDF(BaseDocTemplate):
    def __init__(self, buffer, member_name: str, period_label: str, generated_at: str):
        super().__init__(buffer, pagesize=A4, rightMargin=14*mm, leftMargin=14*mm, topMargin=32*mm, bottomMargin=17*mm, title=f"Accqudo Staff Contribution Report - {member_name}")
        frame = Frame(self.leftMargin, self.bottomMargin, self.width, self.height, id="normal")
        self.addPageTemplates([PageTemplate(id="accqudo", frames=[frame], onPage=self._draw_header_footer)])
        self.member_name = member_name
        self.period_label = period_label
        self.generated_at = generated_at

    def _draw_header_footer(self, canvas, doc):
        canvas.saveState()
        width, height = A4
        navy = colors.HexColor("#16293F")
        blue = colors.HexColor("#1F3A5C")
        muted = colors.HexColor("#667085")
        canvas.setFillColor(navy)
        canvas.setFont("Helvetica-Bold", 17)
        canvas.drawString(14*mm, height-15*mm, "ACCQUDO")
        canvas.setFillColor(blue)
        canvas.setFont("Helvetica-Bold", 7)
        canvas.drawString(14*mm, height-20*mm, "EDUCATION • PRACTICE • PERFORMANCE")
        canvas.setFillColor(muted)
        canvas.setFont("Helvetica", 6.5)
        canvas.drawRightString(width-14*mm, height-15*mm, "INTERNAL CONTRIBUTION REPORT")
        canvas.line(14*mm, height-23*mm, width-14*mm, height-23*mm)

        canvas.setStrokeColor(colors.HexColor("#E4E7EC"))
        canvas.line(14*mm, 12*mm, width-14*mm, 12*mm)
        canvas.setFillColor(muted)
        canvas.setFont("Helvetica", 6.5)
        canvas.drawString(14*mm, 7.5*mm, "Accqudo • accqudo.com • Confidential internal document")
        canvas.drawRightString(width-14*mm, 7.5*mm, f"Page {doc.page}")
        canvas.restoreState()


def build_member_pdf(report: Dict[str, Any]) -> io.BytesIO:
    buffer = io.BytesIO()
    member = report["member"]
    period = report["period"]
    styles = _pdf_styles()
    doc = _AccqudoPDF(
        buffer,
        str(member.get("full_name") or member.get("email") or f"Staff {member.get('id')}"),
        str(period["label"]),
        str(report.get("generated_at") or ""),
    )
    story: List[Any] = []

    story.append(Paragraph("STAFF CONTRIBUTION & PERFORMANCE REPORT", styles["title"]))
    story.append(Paragraph("Official internal report of attributable academic-content and paper-building activity.", styles["subtitle"]))
    story.append(Spacer(1, 4))

    identity = [
        [Paragraph("STAFF MEMBER", styles["metric_label"]), Paragraph("ROLE", styles["metric_label"]), Paragraph("REPORT PERIOD", styles["metric_label"]), Paragraph("GENERATED", styles["metric_label"])],
        [Paragraph(_pdf_text(member.get("full_name") or "Unnamed staff"), styles["body"]), Paragraph(_pdf_text(role_label(member.get("role"))), styles["body"]), Paragraph(_pdf_text(period["label"]), styles["body"]), Paragraph(_pdf_text(iso(report.get("generated_at"))), styles["body"])],
        [Paragraph("EMAIL", styles["metric_label"]), Paragraph("STAFF ID", styles["metric_label"]), Paragraph("PERIOD START", styles["metric_label"]), Paragraph("PERIOD END", styles["metric_label"])],
        [Paragraph(_pdf_text(member.get("email")), styles["body"]), Paragraph(str(member.get("id")), styles["body"]), Paragraph(_pdf_text(iso(period.get("start_date")) or "Full history"), styles["body"]), Paragraph(_pdf_text(iso(period.get("end_date"))), styles["body"])],
    ]
    story.append(_pdf_table(identity, widths=[43*mm, 33*mm, 47*mm, 47*mm], header=False))
    story.append(Spacer(1, 8))

    summary = report["summary"]
    metric_rows = [[
        Paragraph("QUESTIONS", styles["metric_label"]), Paragraph("PAPERS", styles["metric_label"]), Paragraph("PAPER ADDITIONS", styles["metric_label"]), Paragraph("PACKAGE LINKS", styles["metric_label"]), Paragraph("PACKAGES", styles["metric_label"]),
    ], [
        Paragraph(str(summary["questions_created"]), styles["metric"]), Paragraph(str(summary["papers_assembled"]), styles["metric"]), Paragraph(str(summary["questions_added_to_papers"]), styles["metric"]), Paragraph(str(summary["package_paper_links"]), styles["metric"]), Paragraph(str(summary["packages_linked"]), styles["metric"]),
    ]]
    story.append(_pdf_table(metric_rows, widths=[35.2*mm]*5, header=False))
    story.append(Spacer(1, 7))

    story.append(Paragraph("1. Contribution overview", styles["section"]))
    story.append(Paragraph(
        f"This report covers <b>{_pdf_text(period['label'])}</b>. Question and paper creation are filtered using their database creation timestamps. Paper-question additions and package-paper links are shown as lifetime/current attribution because the underlying relationship tables do not contain a reliable creation timestamp.",
        styles["body"],
    ))

    qb = report["question_breakdown"]
    for title, key in [("Question types", "by_type"), ("Exams", "by_exam"), ("Subjects", "by_subject")]:
        items = qb.get(key, [])
        if items:
            story.append(Paragraph(title, styles["section"]))
            rows_data = [["Category", "Questions", "Share"]]
            total = sum(int(x["count"]) for x in items)
            for item in items:
                count = int(item["count"])
                share = (100 * count / total) if total else 0
                rows_data.append([item["label"], count, f"{share:.1f}%"])
            story.append(_pdf_table(rows_data, widths=[105*mm, 35*mm, 25*mm]))

    story.append(Paragraph("2. Academic contribution by section", styles["section"]))
    academic = report.get("academic_breakdown", [])
    if not academic:
        story.append(Paragraph("No authored questions were found for the selected period.", styles["body"]))
    else:
        for exam in academic:
            story.append(KeepTogether([Paragraph(f"Exam: {_pdf_text(exam.get('title'))}", styles["body"]), Spacer(1, 2)]))
            rows_data = [["Subject", "Chapter", "Topic", "Contributed", "Topic total", "Share"]]
            for subject in exam.get("subjects", []):
                for chapter in subject.get("chapters", []):
                    for topic in chapter.get("topics", []):
                        rows_data.append([
                            subject.get("name"), chapter.get("name"), topic.get("name"),
                            topic.get("contributed_questions", 0), topic.get("total_questions", 0),
                            f"{float(topic.get('contribution_percent', 0)):.1f}%",
                        ])
            story.append(_pdf_table(rows_data, widths=[35*mm, 43*mm, 48*mm, 19*mm, 19*mm, 18*mm], font_size=6.4))
            story.append(Spacer(1, 5))

    story.append(Paragraph("3. Papers assembled", styles["section"]))
    papers = report.get("papers", [])
    if papers:
        rows_data = [["Paper", "Exam", "Questions", "Added by member", "Marks", "Created"]]
        for p in papers:
            rows_data.append([
                p.get("title") or f"Paper #{p.get('id')}", p.get("exam_title") or "—",
                p.get("total_questions", 0), p.get("questions_added_by_me", 0),
                p.get("total_marks") if p.get("total_marks") is not None else "—", iso(p.get("created_at")) or "—",
            ])
        story.append(_pdf_table(rows_data, widths=[52*mm, 38*mm, 20*mm, 28*mm, 20*mm, 29*mm], font_size=6.6))
    else:
        story.append(Paragraph("No papers assembled in the selected period.", styles["body"]))

    story.append(Paragraph("4. Collaborative paper additions", styles["section"]))
    additions = report.get("collaborative_paper_additions", [])
    if additions:
        rows_data = [["Paper", "Question", "Subject", "Chapter", "Topic", "Marks"]]
        for a in additions:
            rows_data.append([
                a.get("test_title") or f"Paper #{a.get('test_id')}", f"#{a.get('question_id')}",
                a.get("subject", {}).get("name"), a.get("chapter", {}).get("name"), a.get("topic", {}).get("name"),
                a.get("marks") if a.get("marks") is not None else "—",
            ])
        story.append(_pdf_table(rows_data, widths=[45*mm, 18*mm, 32*mm, 39*mm, 42*mm, 16*mm], font_size=6.2))
    else:
        story.append(Paragraph("No collaborative paper additions found.", styles["body"]))

    story.append(Paragraph("5. Package contribution", styles["section"]))
    package_items = report.get("package_contributions", [])
    if package_items:
        rows_data = [["Package", "Paper ID", "Total questions", "Member questions", "Contribution"]]
        for package in package_items:
            for paper in package.get("papers", []):
                rows_data.append([
                    package.get("package_title") or f"Package #{package.get('package_id')}",
                    paper.get("test_id"), paper.get("total_questions", 0), paper.get("contributed_questions", 0),
                    f"{float(paper.get('contribution_percent', 0)):.1f}%",
                ])
        story.append(_pdf_table(rows_data, widths=[70*mm, 25*mm, 32*mm, 32*mm, 30*mm]))
    else:
        story.append(Paragraph("No package contribution links were attributed to this member.", styles["body"]))

    story.append(Paragraph("6. Package ↔ paper links created by member", styles["section"]))
    links = report.get("package_links", [])
    if links:
        rows_data = [["Package", "Package ID", "Paper", "Paper ID"]]
        for link in links:
            rows_data.append([
                link.get("package_title") or f"Package #{link.get('package_id')}", link.get("package_id"),
                link.get("test_title") or f"Paper #{link.get('test_id')}", link.get("test_id"),
            ])
        story.append(_pdf_table(rows_data, widths=[75*mm, 25*mm, 75*mm, 25*mm]))
    else:
        story.append(Paragraph("No package ↔ paper links found.", styles["body"]))

    # Full authored-question appendix: this makes the report auditable rather
    # than merely a KPI summary.
    story.append(PageBreak())
    story.append(Paragraph("APPENDIX A — COMPLETE AUTHORED QUESTION REGISTER", styles["title"]))
    story.append(Paragraph("Every question attributed to the selected staff member in the report period.", styles["subtitle"]))
    qrows = [["Question ID", "Type", "Exam", "Subject", "Chapter", "Topic", "Created"]]
    for q in report.get("questions", []):
        qrows.append([
            q.get("question_id"), q.get("question_type") or "—", q.get("exam", {}).get("title") or "—",
            q.get("subject", {}).get("name") or "—", q.get("chapter", {}).get("name") or "—",
            q.get("topic", {}).get("name") or "—", iso(q.get("created_at")) or "—",
        ])
    if len(qrows) == 1:
        qrows.append(["—", "—", "No questions", "—", "—", "—", "—"])
    story.append(_pdf_table(qrows, widths=[17*mm, 21*mm, 37*mm, 34*mm, 39*mm, 39*mm, 28*mm], font_size=5.8))

    story.append(Spacer(1, 8))
    story.append(Paragraph("Report control note", styles["section"]))
    story.append(Paragraph(
        "This document is generated directly from Accqudo application data and is intended for internal administrative review. Attribution is based on the database ownership fields used by the Team Contribution system. Counts should be interpreted within the selected reporting period and the timestamp limitations stated above.",
        styles["small"],
    ))

    doc.build(story)
    buffer.seek(0)
    return buffer


@router.get("/report/member/{user_id}/download")
async def download_member_report(
    user_id: int,
    period: str = Query(default="3m"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await require_super_admin(current_user)
    period = period.strip().lower()
    report = await build_member_report(db, user_id, period)
    pdf = build_member_pdf(report)
    member_name = str(report["member"].get("full_name") or f"staff_{user_id}").strip()
    safe_name = "_".join(part for part in member_name.replace("/", "_").split() if part) or f"staff_{user_id}"
    filename = f"accqudo_{safe_name}_{period}_staff_contribution_report.pdf"
    return StreamingResponse(
        pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/report/member/{user_id}/download/csv")
async def download_member_csv(
    user_id: int,
    period: str = Query(default="3m"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await require_super_admin(current_user)
    period = period.strip().lower()
    report = await build_member_report(db, user_id, period)
    headers = [
        "staff_id", "full_name", "email", "role", "period", "questions_created", "topics_created",
        "chapters_created", "subjects_created", "papers_assembled", "questions_added_to_papers",
        "package_paper_links", "packages_linked", "question_share_percent", "paper_share_percent",
        "question_addition_share_percent", "package_link_share_percent", "attribution_scope",
    ]
    member = report["member"]
    target = member
    totals = report["team_totals"]
    record = {
        "staff_id": user_id, "full_name": member.get("full_name"), "email": member.get("email"),
        "role": member.get("role"), "period": period, **report["summary"],
        "question_share_percent": round(100 * report["summary"]["questions_created"] / totals["questions_created"], 1) if totals["questions_created"] else 0,
        "paper_share_percent": round(100 * report["summary"]["papers_assembled"] / totals["papers_assembled"], 1) if totals["papers_assembled"] else 0,
        "question_addition_share_percent": round(100 * report["summary"]["questions_added_to_papers"] / totals["questions_added_to_papers"], 1) if totals["questions_added_to_papers"] else 0,
        "package_link_share_percent": round(100 * report["summary"]["package_paper_links"] / totals["package_paper_links"], 1) if totals["package_paper_links"] else 0,
        "attribution_scope": report["scope"]["note"],
    }
    data = build_csv(headers, [record])
    name = str(member.get("full_name") or f"staff_{user_id}").strip().replace(" ", "_").replace("/", "_")
    return StreamingResponse(data, media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="accqudo_{name}_{period}_progress.csv"'})


async def complete_sales_rows(db: AsyncSession, start: Optional[datetime], source: str) -> List[Dict[str, Any]]:
    sales = await fetch_sales(db, source, start)
    packages = await fetch_packages(db, source)
    package_map = {int(p["id"]): p.get("title") for p in packages}
    user_ids = sorted({int(r["user_id"]) for r in sales if r.get("user_id") is not None})
    users: Dict[int, Dict[str, Any]] = {}
    if user_ids:
        placeholders = ",".join(f":uid_{i}" for i in range(len(user_ids)))
        user_rows = await rows(db, f"SELECT id, full_name, email FROM users WHERE id IN ({placeholders})", {f"uid_{i}": uid for i, uid in enumerate(user_ids)})
        users = {int(r["id"]): r for r in user_rows}
    output = []
    for r in sales:
        pid = int(r["package_id"])
        uid = int(r["user_id"]) if r.get("user_id") is not None else None
        amount = money(r.get("amount_inr")) if source == "legacy" else money(r.get("amount_paise")) / 100
        user = users.get(uid) if uid is not None else None
        output.append({
            "sale_id": int(r["id"]), "created_at": iso(r.get("created_at")),
            "student_id": uid, "student_name": user.get("full_name") if user else None,
            "student_email": user.get("email") if user else None,
            "package_id": pid, "package_title": package_map.get(pid, f"Package #{pid}"),
            "amount_inr": round(amount, 2), "status": normalize(r.get("status")),
            "order_id": r.get("order_id"), "payment_id": r.get("payment_id"),
        })
    return output


@router.get("/report/sales/download")
async def download_sales_report(
    period: str = Query(default="3m"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await require_super_admin(current_user)
    period = period.strip().lower()
    start = period_start(period)
    source = await detect_sales_source(db, start)
    records = await complete_sales_rows(db, start, source)
    headers = ["sale_id", "created_at", "student_id", "student_name", "student_email", "package_id", "package_title", "amount_inr", "status", "order_id", "payment_id"]
    data = build_csv(headers, records)
    return StreamingResponse(data, media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="accqudo_package_sales_{period}.csv"'})


@router.get("/report/sales/packages/download")
async def download_package_sales_summary(
    period: str = Query(default="3m"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await require_super_admin(current_user)
    period = period.strip().lower()
    start = period_start(period)
    report = await get_sales_report(db, start)
    headers = [
        "package_id", "package_title", "exam_id", "tier", "is_active", "price_inr",
        "sales", "revenue", "unique_buyers", "subscriptions", "active_subscriptions", "expired_subscriptions",
    ]
    records = [{k: r.get(k) for k in headers} for r in report["packages"]]
    data = build_csv(headers, records)
    return StreamingResponse(data, media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="accqudo_package_summary_{period}.csv"'})
