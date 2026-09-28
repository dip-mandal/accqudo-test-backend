"""
Accqudo Team Progress Dashboard

Routes
------
GET  /api/v1/team/progress/dashboard?period=1m|3m|6m|1y|all
GET  /api/v1/team/progress/report/member/{user_id}?period=...
GET  /api/v1/team/progress/report/sales?period=...

Access
------
SUPER_ADMIN only.

The dashboard reports:
- Staff member contribution/activity
- Questions, topics, chapters, subjects created
- Papers/tests assembled
- Questions added to papers
- Package -> paper links created
- Contribution share among staff for the selected period
- Package sales, successful sales, revenue, buyers and subscriptions
- Monthly activity/revenue trend
- CSV downloads for a selected staff member and for sales

The sales portion intentionally follows the same current/legacy sales source
strategy used by the Team Sells dashboard:
    current: packages -> payments -> subscriptions
    legacy:  subscription_packages -> razorpay_orders -> user_subscriptions

No database schema changes are required.
"""

from __future__ import annotations

import csv
import io
from calendar import monthrange
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User


router = APIRouter(
    prefix="/team/progress",
    tags=["Team - Progress"],
)

SUPER_ROLES = {"SUPER_ADMIN", "SUPERADMIN"}
STAFF_ROLES = {"ADMIN", "TEAM", "SUPER_ADMIN", "SUPERADMIN"}
SUCCESS_STATUSES = {"SUCCESS", "PAID", "CAPTURED", "COMPLETED"}


def normalize(value: Any) -> str:
    if value is None:
        return ""
    return str(getattr(value, "value", value)).strip().upper()


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


def period_start(period: str) -> Optional[datetime]:
    """
    1m = current calendar month
    3m = current month + previous 2 months
    6m = current month + previous 5 months
    1y = current month + previous 11 months
    all = no lower bound
    """
    period = normalize(period).lower()
    now = datetime.utcnow()

    if period == "all":
        return None

    months = {
        "1m": 1,
        "3m": 3,
        "6m": 6,
        "1y": 12,
    }.get(period)

    if months is None:
        raise HTTPException(
            status_code=400,
            detail="period must be one of: 1m, 3m, 6m, 1y, all",
        )

    month_index = now.year * 12 + (now.month - 1) - (months - 1)
    year = month_index // 12
    month = month_index % 12 + 1
    return datetime(year, month, 1)


def period_meta(period: str, start: Optional[datetime]) -> Dict[str, Any]:
    now = datetime.utcnow()
    return {
        "key": period,
        "start_date": start.isoformat() if start else None,
        "end_date": now.isoformat(),
        "label": {
            "1m": "1 Month",
            "3m": "3 Months",
            "6m": "6 Months",
            "1y": "1 Year",
            "all": "Full Time",
        }[period],
    }


def date_condition(
    column: str,
    start: Optional[datetime],
    parameter: str = "start_date",
) -> tuple[str, dict[str, Any]]:
    if start is None:
        return "", {}
    return f" AND {column} >= :{parameter}", {parameter: start}


async def require_super_admin(current_user: User) -> None:
    role = normalize(getattr(current_user, "role", None))
    if role not in SUPER_ROLES:
        raise HTTPException(
            status_code=403,
            detail="Only SUPER_ADMIN can access team progress.",
        )


async def get_staff_users(db: AsyncSession) -> List[Dict[str, Any]]:
    return await rows(
        db,
        """
        SELECT id, full_name, email, role, is_active, created_at
        FROM users
        WHERE UPPER(role) IN ('ADMIN', 'TEAM', 'SUPER_ADMIN', 'SUPERADMIN')
        ORDER BY
            CASE
                WHEN UPPER(role) IN ('SUPER_ADMIN', 'SUPERADMIN') THEN 0
                WHEN UPPER(role) = 'ADMIN' THEN 1
                ELSE 2
            END,
            full_name,
            email,
            id
        """,
    )


async def get_staff_activity(
    db: AsyncSession,
    start: Optional[datetime],
) -> List[Dict[str, Any]]:
    """
    One row per staff member. Every metric is scoped to the selected period
    using the timestamp of the action being measured.
    """

    q_condition, q_params = date_condition("created_at", start, "q_start")
    topic_condition, topic_params = date_condition("created_at", start, "topic_start")
    chapter_condition, chapter_params = date_condition("created_at", start, "chapter_start")
    subject_condition, subject_params = date_condition("created_at", start, "subject_start")
    test_condition, test_params = date_condition("created_at", start, "test_start")
    tq_condition, tq_params = date_condition("created_at", start, "tq_start")
    pt_condition, pt_params = date_condition("created_at", start, "pt_start")

    params: dict[str, Any] = {}
    for group in (
        q_params,
        topic_params,
        chapter_params,
        subject_params,
        test_params,
        tq_params,
        pt_params,
    ):
        params.update(group)

    return await rows(
        db,
        f"""
        SELECT
            u.id,
            u.full_name,
            u.email,
            u.role,
            u.is_active,

            (
                SELECT COUNT(*)
                FROM questions q
                WHERE q.created_by = u.id
                {q_condition}
            ) AS questions_created,

            (
                SELECT COUNT(*)
                FROM topics tp
                WHERE tp.created_by = u.id
                {topic_condition}
            ) AS topics_created,

            (
                SELECT COUNT(*)
                FROM chapters c
                WHERE c.created_by = u.id
                {chapter_condition}
            ) AS chapters_created,

            (
                SELECT COUNT(*)
                FROM subjects s
                WHERE s.created_by = u.id
                {subject_condition}
            ) AS subjects_created,

            (
                SELECT COUNT(*)
                FROM tests t
                WHERE t.created_by = u.id
                {test_condition}
            ) AS papers_assembled,

            (
                SELECT COUNT(*)
                FROM test_questions tq
                WHERE tq.added_by = u.id
                {tq_condition}
            ) AS questions_added_to_papers,

            (
                SELECT COUNT(*)
                FROM package_tests pt
                WHERE pt.created_by = u.id
                {pt_condition}
            ) AS package_paper_links,

            (
                SELECT COUNT(DISTINCT pt.package_id)
                FROM package_tests pt
                WHERE pt.created_by = u.id
                {pt_condition}
            ) AS packages_linked

        FROM users u
        WHERE UPPER(u.role) IN ('ADMIN', 'TEAM', 'SUPER_ADMIN', 'SUPERADMIN')
        ORDER BY questions_created DESC, papers_assembled DESC, u.full_name, u.id
        """,
        params,
    )


async def get_team_totals(
    db: AsyncSession,
    start: Optional[datetime],
) -> Dict[str, int]:
    activity = await get_staff_activity(db, start)
    fields = (
        "questions_created",
        "topics_created",
        "chapters_created",
        "subjects_created",
        "papers_assembled",
        "questions_added_to_papers",
        "package_paper_links",
        "packages_linked",
    )
    return {
        field: sum(int(row.get(field) or 0) for row in activity)
        for field in fields
    }


async def get_monthly_activity(
    db: AsyncSession,
    start: Optional[datetime],
) -> List[Dict[str, Any]]:
    """
    Returns monthly staff activity for the selected range.
    For full-time mode, the series begins at the earliest available
    contributor action.
    """

    q_where = "WHERE q.created_by IS NOT NULL"
    topic_where = "WHERE tp.created_by IS NOT NULL"
    chapter_where = "WHERE c.created_by IS NOT NULL"
    subject_where = "WHERE s.created_by IS NOT NULL"
    test_where = "WHERE t.created_by IS NOT NULL"
    tq_where = "WHERE tq.added_by IS NOT NULL"
    pt_where = "WHERE pt.created_by IS NOT NULL"

    params: dict[str, Any] = {}

    if start:
        q_where += " AND q.created_at >= :start_date"
        topic_where += " AND tp.created_at >= :start_date"
        chapter_where += " AND c.created_at >= :start_date"
        subject_where += " AND s.created_at >= :start_date"
        test_where += " AND t.created_at >= :start_date"
        tq_where += " AND tq.created_at >= :start_date"
        pt_where += " AND pt.created_at >= :start_date"
        params["start_date"] = start

    monthly = await rows(
        db,
        f"""
        SELECT month, SUM(questions) AS questions,
               SUM(topics) AS topics,
               SUM(chapters) AS chapters,
               SUM(subjects) AS subjects,
               SUM(papers) AS papers,
               SUM(question_additions) AS question_additions,
               SUM(package_links) AS package_links
        FROM (
            SELECT DATE_FORMAT(q.created_at, '%Y-%m') AS month,
                   COUNT(*) AS questions,
                   0 AS topics, 0 AS chapters, 0 AS subjects,
                   0 AS papers, 0 AS question_additions, 0 AS package_links
            FROM questions q
            {q_where}
            GROUP BY DATE_FORMAT(q.created_at, '%Y-%m')

            UNION ALL

            SELECT DATE_FORMAT(tp.created_at, '%Y-%m'),
                   0, COUNT(*), 0, 0, 0, 0, 0
            FROM topics tp
            {topic_where}
            GROUP BY DATE_FORMAT(tp.created_at, '%Y-%m')

            UNION ALL

            SELECT DATE_FORMAT(c.created_at, '%Y-%m'),
                   0, 0, COUNT(*), 0, 0, 0, 0
            FROM chapters c
            {chapter_where}
            GROUP BY DATE_FORMAT(c.created_at, '%Y-%m')

            UNION ALL

            SELECT DATE_FORMAT(s.created_at, '%Y-%m'),
                   0, 0, 0, COUNT(*), 0, 0, 0
            FROM subjects s
            {subject_where}
            GROUP BY DATE_FORMAT(s.created_at, '%Y-%m')

            UNION ALL

            SELECT DATE_FORMAT(t.created_at, '%Y-%m'),
                   0, 0, 0, 0, COUNT(*), 0, 0
            FROM tests t
            {test_where}
            GROUP BY DATE_FORMAT(t.created_at, '%Y-%m')

            UNION ALL

            SELECT DATE_FORMAT(tq.created_at, '%Y-%m'),
                   0, 0, 0, 0, 0, COUNT(*), 0
            FROM test_questions tq
            {tq_where}
            GROUP BY DATE_FORMAT(tq.created_at, '%Y-%m')

            UNION ALL

            SELECT DATE_FORMAT(pt.created_at, '%Y-%m'),
                   0, 0, 0, 0, 0, 0, COUNT(*)
            FROM package_tests pt
            {pt_where}
            GROUP BY DATE_FORMAT(pt.created_at, '%Y-%m')
        ) activity
        GROUP BY month
        ORDER BY month
        """,
        params,
    )

    return [
        {
            "month": str(row["month"]),
            "questions": int(row["questions"] or 0),
            "topics": int(row["topics"] or 0),
            "chapters": int(row["chapters"] or 0),
            "subjects": int(row["subjects"] or 0),
            "papers": int(row["papers"] or 0),
            "question_additions": int(row["question_additions"] or 0),
            "package_links": int(row["package_links"] or 0),
        }
        for row in monthly
    ]


async def detect_sales_source(
    db: AsyncSession,
    start: Optional[datetime],
) -> str:
    """
    Match the Team Sells dashboard source selection:
    current payments are preferred when they contain successful sales;
    otherwise legacy razorpay orders are used.
    """

    condition, params = date_condition("created_at", start, "sales_start")

    current = await one(
        db,
        f"""
        SELECT COUNT(*) AS total
        FROM payments
        WHERE UPPER(CAST(status AS CHAR)) IN
            ('SUCCESS','PAID','CAPTURED','COMPLETED')
        {condition}
        """,
        params,
    )

    if current and int(current["total"] or 0) > 0:
        return "current"

    legacy = await one(
        db,
        f"""
        SELECT COUNT(*) AS total
        FROM razorpay_orders
        WHERE package_id IS NOT NULL
          AND UPPER(CAST(status AS CHAR)) IN
            ('SUCCESS','PAID','CAPTURED','COMPLETED')
        {condition}
        """,
        params,
    )

    if legacy and int(legacy["total"] or 0) > 0:
        return "legacy"

    # If the selected period has no successful sale, prefer the current
    # catalog if it exists, otherwise use legacy.
    current_catalog = await one(db, "SELECT COUNT(*) AS total FROM packages")
    if current_catalog and int(current_catalog["total"] or 0) > 0:
        return "current"

    return "legacy"


async def get_sales_report(
    db: AsyncSession,
    start: Optional[datetime],
) -> Dict[str, Any]:
    source = await detect_sales_source(db, start)
    condition, params = date_condition("created_at", start, "sales_start")

    if source == "current":
        packages = await rows(
            db,
            """
            SELECT id, title, exam_id, price_paise, is_active
            FROM packages
            ORDER BY title, id
            """,
        )

        sales = await rows(
            db,
            f"""
            SELECT
                p.id,
                p.user_id,
                p.package_id,
                p.razorpay_order_id AS order_id,
                p.razorpay_payment_id AS payment_id,
                p.amount_paise,
                p.status,
                p.created_at
            FROM payments p
            WHERE UPPER(CAST(p.status AS CHAR)) IN
                ('SUCCESS','PAID','CAPTURED','COMPLETED')
            {condition.replace("created_at", "p.created_at")}
            ORDER BY p.created_at DESC, p.id DESC
            """,
            params,
        )

        subscription_rows = await rows(
            db,
            f"""
            SELECT package_id, user_id, status, start_date, expiry_date, created_at
            FROM subscriptions
            {"WHERE created_at >= :sales_start" if start else ""}
            """,
            params,
        )
    else:
        packages = await rows(
            db,
            """
            SELECT id, title, exam_id, price AS price_inr, is_active
            FROM subscription_packages
            ORDER BY title, id
            """,
        )

        sales = await rows(
            db,
            f"""
            SELECT
                r.id,
                r.user_id,
                r.package_id,
                r.razorpay_order_id AS order_id,
                r.razorpay_payment_id AS payment_id,
                r.amount_inr,
                r.status,
                r.created_at
            FROM razorpay_orders r
            WHERE r.package_id IS NOT NULL
              AND UPPER(CAST(r.status AS CHAR)) IN
                ('SUCCESS','PAID','CAPTURED','COMPLETED')
            {condition.replace("created_at", "r.created_at")}
            ORDER BY r.created_at DESC, r.id DESC
            """,
            params,
        )

        subscription_rows = await rows(
            db,
            f"""
            SELECT package_id, user_id,
                   CASE WHEN is_active = 1 THEN 'ACTIVE' ELSE 'EXPIRED' END AS status,
                   start_date, end_date AS expiry_date, created_at
            FROM user_subscriptions
            {"WHERE created_at >= :sales_start" if start else ""}
            """,
            params,
        )

    user_ids = sorted(
        {
            int(row["user_id"])
            for row in sales
            if row.get("user_id") is not None
        }
    )

    users: Dict[int, Dict[str, Any]] = {}
    if user_ids:
        placeholders = ", ".join(
            f":uid_{i}" for i in range(len(user_ids))
        )
        user_params = {
            f"uid_{i}": uid for i, uid in enumerate(user_ids)
        }
        user_rows = await rows(
            db,
            f"""
            SELECT id, full_name, email
            FROM users
            WHERE id IN ({placeholders})
            """,
            user_params,
        )
        users = {
            int(row["id"]): row
            for row in user_rows
        }

    package_map = {
        int(row["id"]): row
        for row in packages
    }

    performance: Dict[int, Dict[str, Any]] = {}

    for package in packages:
        pid = int(package["id"])
        performance[pid] = {
            "package_id": pid,
            "package_title": package["title"],
            "exam_id": package.get("exam_id"),
            "is_active": bool(package.get("is_active")),
            "price_inr": (
                money(package.get("price_inr"))
                if source == "legacy"
                else money(package.get("price_paise")) / 100.0
            ),
            "sales": 0,
            "revenue": 0.0,
            "unique_buyers": set(),
            "subscriptions": 0,
            "active_subscriptions": 0,
        }

    monthly: Dict[str, Dict[str, Any]] = {}

    for sale in sales:
        pid = int(sale["package_id"])
        if pid not in performance:
            performance[pid] = {
                "package_id": pid,
                "package_title": f"Package #{pid}",
                "exam_id": None,
                "is_active": True,
                "price_inr": 0.0,
                "sales": 0,
                "revenue": 0.0,
                "unique_buyers": set(),
                "subscriptions": 0,
                "active_subscriptions": 0,
            }

        amount = (
            money(sale.get("amount_inr"))
            if source == "legacy"
            else money(sale.get("amount_paise")) / 100.0
        )

        item = performance[pid]
        item["sales"] += 1
        item["revenue"] += amount
        if sale.get("user_id") is not None:
            item["unique_buyers"].add(int(sale["user_id"]))

        created = sale.get("created_at")
        if isinstance(created, datetime):
            key = created.strftime("%Y-%m")
            monthly.setdefault(
                key,
                {"month": key, "sales": 0, "revenue": 0.0},
            )
            monthly[key]["sales"] += 1
            monthly[key]["revenue"] += amount

    for subscription in subscription_rows:
        pid = int(subscription["package_id"])
        if pid not in performance:
            continue

        performance[pid]["subscriptions"] += 1
        if normalize(subscription.get("status")) == "ACTIVE":
            performance[pid]["active_subscriptions"] += 1

    package_rows = []
    for item in performance.values():
        item["revenue"] = round(float(item["revenue"]), 2)
        item["unique_buyers"] = len(item["unique_buyers"])
        package_rows.append(item)

    package_rows.sort(
        key=lambda row: (
            -float(row["revenue"]),
            -int(row["sales"]),
            str(row["package_title"]).lower(),
        )
    )

    recent_sales = []
    for sale in sales[:100]:
        pid = int(sale["package_id"])
        user = users.get(int(sale["user_id"])) if sale.get("user_id") is not None else None
        amount = (
            money(sale.get("amount_inr"))
            if source == "legacy"
            else money(sale.get("amount_paise")) / 100.0
        )
        recent_sales.append(
            {
                "id": int(sale["id"]),
                "user_id": int(sale["user_id"]) if sale.get("user_id") is not None else None,
                "student_name": user.get("full_name") if user else None,
                "student_email": user.get("email") if user else None,
                "package_id": pid,
                "package_title": package_map.get(pid, {}).get("title", f"Package #{pid}"),
                "amount_inr": amount,
                "status": normalize(sale.get("status")),
                "order_id": sale.get("order_id"),
                "payment_id": sale.get("payment_id"),
                "created_at": iso(sale.get("created_at")),
            }
        )

    total_revenue = round(
        sum(float(item["revenue"]) for item in package_rows),
        2,
    )

    return {
        "source": source,
        "summary": {
            "revenue": total_revenue,
            "successful_sales": len(sales),
            "unique_buyers": len({
                int(sale["user_id"])
                for sale in sales
                if sale.get("user_id") is not None
            }),
            "packages_with_sales": sum(
                1 for item in package_rows if int(item["sales"]) > 0
            ),
            "subscriptions": sum(
                int(item["subscriptions"]) for item in package_rows
            ),
            "active_subscriptions": sum(
                int(item["active_subscriptions"]) for item in package_rows
            ),
        },
        "packages": [
            {
                **item,
                "unique_buyers": int(item["unique_buyers"]),
            }
            for item in package_rows
        ],
        "monthly": [
            {
                "month": key,
                "sales": int(value["sales"]),
                "revenue": round(float(value["revenue"]), 2),
            }
            for key, value in sorted(monthly.items())
        ],
        "recent_sales": recent_sales,
    }


def build_csv(
    headers: List[str],
    records: List[Dict[str, Any]],
) -> io.BytesIO:
    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=headers,
        extrasaction="ignore",
    )
    writer.writeheader()
    writer.writerows(records)

    data = io.BytesIO(output.getvalue().encode("utf-8-sig"))
    data.seek(0)
    return data


@router.get("/dashboard")
async def progress_dashboard(
    period: str = Query(
        default="3m",
        description="1m, 3m, 6m, 1y or all",
    ),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await require_super_admin(current_user)

    period = period.lower().strip()
    start = period_start(period)

    staff = await get_staff_activity(db, start)
    totals = await get_team_totals(db, start)
    monthly_activity = await get_monthly_activity(db, start)
    sales = await get_sales_report(db, start)

    for member in staff:
        member["id"] = int(member["id"])
        for field in (
            "questions_created",
            "topics_created",
            "chapters_created",
            "subjects_created",
            "papers_assembled",
            "questions_added_to_papers",
            "package_paper_links",
            "packages_linked",
        ):
            member[field] = int(member.get(field) or 0)

        member["question_share_percent"] = round(
            100 * member["questions_created"] / totals["questions_created"],
            1,
        ) if totals["questions_created"] else 0.0

        member["paper_share_percent"] = round(
            100 * member["papers_assembled"] / totals["papers_assembled"],
            1,
        ) if totals["papers_assembled"] else 0.0

        member["question_addition_share_percent"] = round(
            100
            * member["questions_added_to_papers"]
            / totals["questions_added_to_papers"],
            1,
        ) if totals["questions_added_to_papers"] else 0.0

        member["package_link_share_percent"] = round(
            100 * member["package_paper_links"] / totals["package_paper_links"],
            1,
        ) if totals["package_paper_links"] else 0.0

        member["created_at"] = iso(member.get("created_at"))

    return {
        "period": period_meta(period, start),
        "staff": staff,
        "team_totals": totals,
        "monthly_activity": monthly_activity,
        "sales": sales,
        "generated_at": datetime.utcnow().isoformat(),
    }




@router.get("/report/sales/packages/download")
async def download_package_sales_summary(
    period: str = Query(default="3m"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Download one row per package with period sales/revenue metrics."""
    await require_super_admin(current_user)

    period = period.lower().strip()
    start = period_start(period)
    report = await get_sales_report(db, start)

    headers = [
        "package_id",
        "package_title",
        "exam_id",
        "is_active",
        "price_inr",
        "sales",
        "revenue",
        "unique_buyers",
        "subscriptions",
        "active_subscriptions",
    ]

    records = [
        {
            "package_id": row["package_id"],
            "package_title": row["package_title"],
            "exam_id": row["exam_id"],
            "is_active": row["is_active"],
            "price_inr": row["price_inr"],
            "sales": row["sales"],
            "revenue": row["revenue"],
            "unique_buyers": row["unique_buyers"],
            "subscriptions": row["subscriptions"],
            "active_subscriptions": row["active_subscriptions"],
        }
        for row in report["packages"]
    ]

    data = build_csv(headers, records)

    return StreamingResponse(
        data,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="accqudo_package_summary_{period}.csv"'
            )
        },
    )


@router.get("/report/member/{user_id}")
async def member_progress_report(
    user_id: int,
    period: str = Query(default="3m"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await require_super_admin(current_user)

    period = period.lower().strip()
    start = period_start(period)

    member = await one(
        db,
        """
        SELECT id, full_name, email, role, is_active, created_at
        FROM users
        WHERE id = :user_id
          AND UPPER(role) IN ('ADMIN','TEAM','SUPER_ADMIN','SUPERADMIN')
        """,
        {"user_id": user_id},
    )

    if not member:
        raise HTTPException(
            status_code=404,
            detail="Staff member not found.",
        )

    # Reuse the exact same aggregation used by the dashboard.
    staff = await get_staff_activity(db, start)
    target = next(
        (row for row in staff if int(row["id"]) == user_id),
        None,
    )

    if target is None:
        target = {
            "id": user_id,
            "full_name": member["full_name"],
            "email": member["email"],
            "role": member["role"],
            "is_active": member["is_active"],
            "questions_created": 0,
            "topics_created": 0,
            "chapters_created": 0,
            "subjects_created": 0,
            "papers_assembled": 0,
            "questions_added_to_papers": 0,
            "package_paper_links": 0,
            "packages_linked": 0,
        }

    question_rows = await rows(
        db,
        """
        SELECT
            q.id AS question_id,
            q.question_type,
            q.created_at,
            tp.name AS topic_name,
            c.name AS chapter_name,
            s.name AS subject_name,
            e.title AS exam_title
        FROM questions q
        JOIN topics tp ON tp.id = q.topic_id
        JOIN chapters c ON c.id = tp.chapter_id
        JOIN subjects s ON s.id = c.subject_id
        LEFT JOIN exams e ON e.id = s.exam_id
        WHERE q.created_by = :uid
        ORDER BY q.created_at DESC, q.id DESC
        """,
        {"uid": user_id},
    )

    if start:
        question_rows = [
            row
            for row in question_rows
            if row.get("created_at") and row["created_at"] >= start
        ]

    return {
        "period": period_meta(period, start),
        "member": target,
        "questions": [
            {
                "question_id": int(row["question_id"]),
                "question_type": normalize(row["question_type"]),
                "topic": row["topic_name"],
                "chapter": row["chapter_name"],
                "subject": row["subject_name"],
                "exam": row["exam_title"],
                "created_at": iso(row["created_at"]),
            }
            for row in question_rows
        ],
        "generated_at": datetime.utcnow().isoformat(),
    }


@router.get("/report/member/{user_id}/download")
async def download_member_report(
    user_id: int,
    period: str = Query(default="3m"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await require_super_admin(current_user)

    period = period.lower().strip()
    start = period_start(period)

    member = await one(
        db,
        """
        SELECT id, full_name, email, role
        FROM users
        WHERE id = :user_id
          AND UPPER(role) IN ('ADMIN','TEAM','SUPER_ADMIN','SUPERADMIN')
        """,
        {"user_id": user_id},
    )

    if not member:
        raise HTTPException(404, "Staff member not found.")

    activity = await get_staff_activity(db, start)
    target = next(
        (row for row in activity if int(row["id"]) == user_id),
        None,
    )

    if not target:
        raise HTTPException(404, "Staff activity not found.")

    headers = [
        "staff_id",
        "full_name",
        "email",
        "role",
        "period",
        "questions_created",
        "topics_created",
        "chapters_created",
        "subjects_created",
        "papers_assembled",
        "questions_added_to_papers",
        "package_paper_links",
        "packages_linked",
    ]

    record = {
        "staff_id": user_id,
        "full_name": member["full_name"],
        "email": member["email"],
        "role": member["role"],
        "period": period,
        **{
            key: int(target.get(key) or 0)
            for key in headers[5:]
        },
    }

    data = build_csv(headers, [record])

    safe_name = (
        str(member["full_name"] or f"staff_{user_id}")
        .strip()
        .replace(" ", "_")
        .replace("/", "_")
    )

    return StreamingResponse(
        data,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="accqudo_{safe_name}_{period}_progress.csv"'
            )
        },
    )


@router.get("/report/sales/download")
async def download_sales_report(
    period: str = Query(default="3m"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await require_super_admin(current_user)

    period = period.lower().strip()
    start = period_start(period)
    report = await get_sales_report(db, start)

    headers = [
        "sale_id",
        "created_at",
        "student_id",
        "student_name",
        "student_email",
        "package_id",
        "package_title",
        "amount_inr",
        "status",
        "order_id",
        "payment_id",
    ]

    records = [
        {
            "sale_id": sale["id"],
            "created_at": sale["created_at"],
            "student_id": sale["user_id"],
            "student_name": sale["student_name"],
            "student_email": sale["student_email"],
            "package_id": sale["package_id"],
            "package_title": sale["package_title"],
            "amount_inr": sale["amount_inr"],
            "status": sale["status"],
            "order_id": sale["order_id"],
            "payment_id": sale["payment_id"],
        }
        for sale in report["recent_sales"]
    ]

    # The dashboard only keeps the most recent 100 sales for the UI. For the
    # downloadable report, query the complete selected-period sale list again
    # so the export is not silently truncated.
    condition, params = date_condition("created_at", start, "sales_start")

    if report["source"] == "current":
        raw_sales = await rows(
            db,
            f"""
            SELECT
                p.id,
                p.user_id,
                p.package_id,
                p.razorpay_order_id AS order_id,
                p.razorpay_payment_id AS payment_id,
                p.amount_paise,
                p.status,
                p.created_at
            FROM payments p
            WHERE UPPER(CAST(p.status AS CHAR)) IN
                ('SUCCESS','PAID','CAPTURED','COMPLETED')
            {condition.replace("created_at", "p.created_at")}
            ORDER BY p.created_at DESC, p.id DESC
            """,
            params,
        )
        package_rows = await rows(
            db,
            "SELECT id, title FROM packages",
        )
        package_map = {int(row["id"]): row["title"] for row in package_rows}
        user_ids = sorted({
            int(row["user_id"]) for row in raw_sales
            if row.get("user_id") is not None
        })
        users = {}
        if user_ids:
            placeholders = ", ".join(
                f":uid_{i}" for i in range(len(user_ids))
            )
            users_rows = await rows(
                db,
                f"""
                SELECT id, full_name, email
                FROM users
                WHERE id IN ({placeholders})
                """,
                {f"uid_{i}": uid for i, uid in enumerate(user_ids)},
            )
            users = {int(row["id"]): row for row in users_rows}

        records = [
            {
                "sale_id": int(row["id"]),
                "created_at": iso(row["created_at"]),
                "student_id": int(row["user_id"]),
                "student_name": users.get(int(row["user_id"]), {}).get("full_name"),
                "student_email": users.get(int(row["user_id"]), {}).get("email"),
                "package_id": int(row["package_id"]),
                "package_title": package_map.get(int(row["package_id"])),
                "amount_inr": round(int(row["amount_paise"] or 0) / 100, 2),
                "status": normalize(row["status"]),
                "order_id": row["order_id"],
                "payment_id": row["payment_id"],
            }
            for row in raw_sales
        ]
    else:
        raw_sales = await rows(
            db,
            f"""
            SELECT
                r.id,
                r.user_id,
                r.package_id,
                r.razorpay_order_id AS order_id,
                r.razorpay_payment_id AS payment_id,
                r.amount_inr,
                r.status,
                r.created_at
            FROM razorpay_orders r
            WHERE r.package_id IS NOT NULL
              AND UPPER(CAST(r.status AS CHAR)) IN
                ('SUCCESS','PAID','CAPTURED','COMPLETED')
            {condition.replace("created_at", "r.created_at")}
            ORDER BY r.created_at DESC, r.id DESC
            """,
            params,
        )
        package_rows = await rows(
            db,
            "SELECT id, title FROM subscription_packages",
        )
        package_map = {int(row["id"]): row["title"] for row in package_rows}
        user_ids = sorted({
            int(row["user_id"]) for row in raw_sales
            if row.get("user_id") is not None
        })
        users = {}
        if user_ids:
            placeholders = ", ".join(
                f":uid_{i}" for i in range(len(user_ids))
            )
            users_rows = await rows(
                db,
                f"""
                SELECT id, full_name, email
                FROM users
                WHERE id IN ({placeholders})
                """,
                {f"uid_{i}": uid for i, uid in enumerate(user_ids)},
            )
            users = {int(row["id"]): row for row in users_rows}

        records = [
            {
                "sale_id": int(row["id"]),
                "created_at": iso(row["created_at"]),
                "student_id": int(row["user_id"]),
                "student_name": users.get(int(row["user_id"]), {}).get("full_name"),
                "student_email": users.get(int(row["user_id"]), {}).get("email"),
                "package_id": int(row["package_id"]),
                "package_title": package_map.get(int(row["package_id"])),
                "amount_inr": money(row["amount_inr"]),
                "status": normalize(row["status"]),
                "order_id": row["order_id"],
                "payment_id": row["payment_id"],
            }
            for row in raw_sales
        ]

    data = build_csv(headers, records)

    return StreamingResponse(
        data,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="accqudo_package_sales_{period}.csv"'
            )
        },
    )
