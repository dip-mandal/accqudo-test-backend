"""
Accqudo Team Progress / Intelligence API

Routes
------
GET  /api/v1/team/progress/dashboard?period=1m|3m|6m|1y|all
GET  /api/v1/team/progress/report/member/{user_id}?period=...
GET  /api/v1/team/progress/report/member/{user_id}/download?period=...
GET  /api/v1/team/progress/report/sales/download?period=...
GET  /api/v1/team/progress/report/sales/packages/download?period=...

Access
------
SUPER_ADMIN only.

IMPORTANT DATABASE-SCHEMA RULE
------------------------------
This file is intentionally written against the active Accqudo schema supplied by
Database Inspector. The current schema does NOT contain creator/author columns on:
    questions, topics, chapters, subjects, tests
and test_questions has no created_at or added_by.

Only package_tests currently has an ownership column (created_by), and it has no
created_at. Therefore this API MUST NOT invent those columns or pretend that
historical content can be attributed to individual staff members when the database
cannot support that attribution.

The dashboard consequently reports:
- global content creation totals for timestamped tables;
- staff-attributed work only where the schema actually stores created_by;
- package_tests attribution by staff, lifetime only because package_tests has no
  timestamp;
- package sales/revenue from the actual current or legacy payment tables;
- monthly activity only for tables that actually have created_at;
- explicit attribution metadata so the frontend can explain unavailable metrics.

No database migration is performed by this module.
"""

from __future__ import annotations

import csv
import io
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

# These are the tables/columns confirmed by the supplied Database Inspector.
TIMESTAMPED_CONTENT_TABLES = {
    "questions": "created_at",
    "topics": "created_at",
    "chapters": "created_at",
    "subjects": "created_at",
    "tests": "created_at",
}

ATTRIBUTION_LIMITATIONS = {
    "questions": "questions has no created_by column; individual author attribution is unavailable.",
    "topics": "topics has no created_by column; individual author attribution is unavailable.",
    "chapters": "chapters has no created_by column; individual author attribution is unavailable.",
    "subjects": "subjects has no created_by column; individual author attribution is unavailable.",
    "tests": "tests has no created_by column; individual assembler attribution is unavailable.",
    "test_questions": "test_questions has neither added_by nor created_at; individual attribution and period filtering are unavailable.",
    "package_tests": "package_tests has created_by but no created_at; staff attribution is available only as lifetime data, not by period.",
}


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
    """Return the first day of the selected calendar-month window."""
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
    Staff-level attribution using ONLY columns that actually exist.

    package_tests.created_by is the only content/work ownership field available
    in the supplied schema. It has no timestamp, so its counts are lifetime
    counts regardless of the selected period.
    """
    return await rows(
        db,
        """
        SELECT
            u.id,
            u.full_name,
            u.email,
            u.role,
            u.is_active,
            u.created_at,

            0 AS questions_created,
            0 AS topics_created,
            0 AS chapters_created,
            0 AS subjects_created,
            0 AS papers_assembled,
            0 AS questions_added_to_papers,

            (
                SELECT COUNT(*)
                FROM package_tests pt
                WHERE pt.created_by = u.id
            ) AS package_paper_links,

            (
                SELECT COUNT(DISTINCT pt.package_id)
                FROM package_tests pt
                WHERE pt.created_by = u.id
            ) AS packages_linked

        FROM users u
        WHERE UPPER(u.role) IN ('ADMIN', 'TEAM', 'SUPER_ADMIN', 'SUPERADMIN')
        ORDER BY package_paper_links DESC, packages_linked DESC, u.full_name, u.id
        """,
    )


async def get_global_content_totals(
    db: AsyncSession,
    start: Optional[datetime],
) -> Dict[str, int]:
    """Count content globally for the selected period using real created_at fields."""
    result: Dict[str, int] = {
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
        condition, params = date_condition("created_at", start, "start_date")
        item = await one(
            db,
            f"SELECT COUNT(*) AS total FROM {table} WHERE 1=1 {condition}",
            params,
        )
        result[key] = int(item["total"] or 0) if item else 0

    # test_questions has no created_at. A period-specific count would be false.
    # For full-time mode we can safely report the current total.
    if start is None:
        item = await one(db, "SELECT COUNT(*) AS total FROM test_questions")
        result["questions_added_to_papers"] = int(item["total"] or 0) if item else 0

        item = await one(db, "SELECT COUNT(*) AS total FROM package_tests")
        result["package_paper_links"] = int(item["total"] or 0) if item else 0

        item = await one(
            db,
            "SELECT COUNT(DISTINCT package_id) AS total FROM package_tests",
        )
        result["packages_linked"] = int(item["total"] or 0) if item else 0

    return result


async def get_monthly_activity(
    db: AsyncSession,
    start: Optional[datetime],
) -> List[Dict[str, Any]]:
    """
    Monthly activity for tables that actually have created_at.

    test_questions and package_tests are intentionally excluded because the
    active schema has no timestamp on either table.
    """
    params: dict[str, Any] = {}

    def where(alias: str) -> str:
        if start is None:
            return ""
        params["start_date"] = start
        return f"WHERE {alias}.created_at >= :start_date"

    monthly = await rows(
        db,
        f"""
        SELECT month,
               SUM(questions) AS questions,
               SUM(topics) AS topics,
               SUM(chapters) AS chapters,
               SUM(subjects) AS subjects,
               SUM(papers) AS papers
        FROM (
            SELECT DATE_FORMAT(q.created_at, '%Y-%m') AS month,
                   COUNT(*) AS questions,
                   0 AS topics,
                   0 AS chapters,
                   0 AS subjects,
                   0 AS papers
            FROM questions q
            {where('q')}
            GROUP BY DATE_FORMAT(q.created_at, '%Y-%m')

            UNION ALL

            SELECT DATE_FORMAT(tp.created_at, '%Y-%m'),
                   0, COUNT(*), 0, 0, 0
            FROM topics tp
            {where('tp')}
            GROUP BY DATE_FORMAT(tp.created_at, '%Y-%m')

            UNION ALL

            SELECT DATE_FORMAT(c.created_at, '%Y-%m'),
                   0, 0, COUNT(*), 0, 0
            FROM chapters c
            {where('c')}
            GROUP BY DATE_FORMAT(c.created_at, '%Y-%m')

            UNION ALL

            SELECT DATE_FORMAT(s.created_at, '%Y-%m'),
                   0, 0, 0, COUNT(*), 0
            FROM subjects s
            {where('s')}
            GROUP BY DATE_FORMAT(s.created_at, '%Y-%m')

            UNION ALL

            SELECT DATE_FORMAT(t.created_at, '%Y-%m'),
                   0, 0, 0, 0, COUNT(*)
            FROM tests t
            {where('t')}
            GROUP BY DATE_FORMAT(t.created_at, '%Y-%m')
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
            # Kept for frontend compatibility. These values are unavailable
            # monthly because the corresponding tables have no timestamp.
            "question_additions": 0,
            "package_links": 0,
        }
        for row in monthly
    ]


async def detect_sales_source(
    db: AsyncSession,
    start: Optional[datetime],
) -> str:
    """Prefer current payments when successful sales exist; otherwise legacy."""
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
            {condition.replace('created_at', 'p.created_at')}
            ORDER BY p.created_at DESC, p.id DESC
            """,
            params,
        )

        # Do not depend on subscriptions.created_at here. Some deployed database
        # versions do not expose that column even though the ORM/schema inspector
        # may show it. Subscription counts are therefore treated as current/lifetime
        # state counts, while sales/revenue remain period-filtered via payments.
        subscription_rows = await rows(
            db,
            """
            SELECT package_id, user_id, status
            FROM subscriptions
            """,
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
            {condition.replace('created_at', 'r.created_at')}
            ORDER BY r.created_at DESC, r.id DESC
            """,
            params,
        )

        # Legacy user_subscriptions may not have created_at in the deployed DB.
        # Read only stable columns; these subscription counts are lifetime/current
        # state counts, while sales/revenue remain period-filtered via razorpay_orders.
        subscription_rows = await rows(
            db,
            """
            SELECT package_id, user_id,
                   CASE WHEN is_active = 1 THEN 'ACTIVE' ELSE 'EXPIRED' END AS status
            FROM user_subscriptions
            """,
        )

    user_ids = sorted({
        int(row["user_id"])
        for row in sales
        if row.get("user_id") is not None
    })

    users: Dict[int, Dict[str, Any]] = {}
    if user_ids:
        placeholders = ", ".join(f":uid_{i}" for i in range(len(user_ids)))
        user_rows = await rows(
            db,
            f"""
            SELECT id, full_name, email
            FROM users
            WHERE id IN ({placeholders})
            """,
            {f"uid_{i}": uid for i, uid in enumerate(user_ids)},
        )
        users = {int(row["id"]): row for row in user_rows}

    package_map = {int(row["id"]): row for row in packages}
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
            monthly.setdefault(key, {"month": key, "sales": 0, "revenue": 0.0})
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
        recent_sales.append({
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
        })

    total_revenue = round(sum(float(item["revenue"]) for item in package_rows), 2)

    return {
        "source": source,
        "subscription_count_scope": "lifetime/current-state",
        "summary": {
            "revenue": total_revenue,
            "successful_sales": len(sales),
            "unique_buyers": len({
                int(sale["user_id"])
                for sale in sales
                if sale.get("user_id") is not None
            }),
            "packages_with_sales": sum(1 for item in package_rows if int(item["sales"]) > 0),
            "subscriptions": sum(int(item["subscriptions"]) for item in package_rows),
            "active_subscriptions": sum(int(item["active_subscriptions"]) for item in package_rows),
        },
        "packages": [
            {**item, "unique_buyers": int(item["unique_buyers"])}
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


def build_csv(headers: List[str], records: List[Dict[str, Any]]) -> io.BytesIO:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=headers, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(records)
    data = io.BytesIO(output.getvalue().encode("utf-8-sig"))
    data.seek(0)
    return data


def finalize_staff_rows(
    staff: List[Dict[str, Any]],
    totals: Dict[str, int],
) -> List[Dict[str, Any]]:
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

        member["question_share_percent"] = 0.0
        member["paper_share_percent"] = 0.0
        member["question_addition_share_percent"] = 0.0
        member["package_link_share_percent"] = (
            round(100 * member["package_paper_links"] / totals["package_paper_links"], 1)
            if totals["package_paper_links"] else 0.0
        )
        member["created_at"] = iso(member.get("created_at"))

        # Explicit frontend metadata.
        member["attribution_available"] = {
            "questions": False,
            "topics": False,
            "chapters": False,
            "subjects": False,
            "tests": False,
            "test_questions": False,
            "package_tests": True,
        }
        member["attribution_note"] = (
            "The active database stores staff ownership only on package_tests.created_by. "
            "Questions, topics, chapters, subjects, tests and test_questions do not contain "
            "the required author/creator fields, so those metrics cannot be assigned to this member."
        )

    return staff


@router.get("/dashboard")
async def progress_dashboard(
    period: str = Query(default="3m", description="1m, 3m, 6m, 1y or all"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await require_super_admin(current_user)

    period = period.lower().strip()
    start = period_start(period)

    staff = await get_staff_activity(db, start)
    totals = await get_global_content_totals(db, start)
    monthly_activity = await get_monthly_activity(db, start)
    sales = await get_sales_report(db, start)
    staff = finalize_staff_rows(staff, totals)

    return {
        "period": period_meta(period, start),
        "staff": staff,
        "team_totals": totals,
        "monthly_activity": monthly_activity,
        "sales": sales,
        "attribution": {
            "fully_supported": ["package_tests.created_by"],
            "unsupported": ATTRIBUTION_LIMITATIONS,
        },
        "generated_at": datetime.utcnow().isoformat(),
    }


@router.get("/report/sales/packages/download")
async def download_package_sales_summary(
    period: str = Query(default="3m"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
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

    records = [{key: row.get(key) for key in headers} for row in report["packages"]]
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
        raise HTTPException(status_code=404, detail="Staff member not found.")

    staff = await get_staff_activity(db, start)
    totals = await get_global_content_totals(db, start)
    target = next((row for row in staff if int(row["id"]) == user_id), None)

    if target is None:
        target = {
            "id": user_id,
            "full_name": member["full_name"],
            "email": member["email"],
            "role": member["role"],
            "is_active": member["is_active"],
            "created_at": iso(member.get("created_at")),
            "questions_created": 0,
            "topics_created": 0,
            "chapters_created": 0,
            "subjects_created": 0,
            "papers_assembled": 0,
            "questions_added_to_papers": 0,
            "package_paper_links": 0,
            "packages_linked": 0,
        }

    target = finalize_staff_rows([target], totals)[0]

    # These are the only detailed staff-owned records supported by the schema.
    package_link_rows = await rows(
        db,
        """
        SELECT
            pt.package_id,
            pt.test_id,
            pt.created_by,
            p.title AS package_title,
            t.title AS test_title,
            t.exam_id
        FROM package_tests pt
        LEFT JOIN subscription_packages p ON p.id = pt.package_id
        LEFT JOIN tests t ON t.id = pt.test_id
        WHERE pt.created_by = :uid
        ORDER BY pt.package_id, pt.test_id
        """,
        {"uid": user_id},
    )

    # Keep the historical endpoint shape for frontend compatibility. Questions
    # are intentionally empty because questions has no created_by in the real DB.
    return {
        "period": period_meta(period, start),
        "member": target,
        "questions": [],
        "package_links": [
            {
                "package_id": int(row["package_id"]),
                "package_title": row.get("package_title"),
                "test_id": int(row["test_id"]),
                "test_title": row.get("test_title"),
                "exam_id": row.get("exam_id"),
                "created_by": user_id,
                "period_filter_applied": False,
            }
            for row in package_link_rows
        ],
        "attribution_note": (
            "This report cannot attribute questions/topics/chapters/subjects/tests or "
            "test-question additions because those tables do not contain creator/author "
            "fields in the active database schema. package_tests.created_by is the only "
            "staff ownership field available, and package_tests has no created_at, so its "
            "records are lifetime rather than period-filtered."
        ),
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
        raise HTTPException(status_code=404, detail="Staff member not found.")

    staff = await get_staff_activity(db, start)
    totals = await get_global_content_totals(db, start)
    target = next((row for row in staff if int(row["id"]) == user_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="Staff activity not found.")

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
        "attribution_note",
    ]

    record = {
        "staff_id": user_id,
        "full_name": member["full_name"],
        "email": member["email"],
        "role": member["role"],
        "period": period,
        "questions_created": 0,
        "topics_created": 0,
        "chapters_created": 0,
        "subjects_created": 0,
        "papers_assembled": 0,
        "questions_added_to_papers": 0,
        "package_paper_links": int(target.get("package_paper_links") or 0),
        "packages_linked": int(target.get("packages_linked") or 0),
        "attribution_note": (
            "Only package_tests.created_by is stored in the active schema. "
            "Package-test links have no created_at, so these two values are lifetime counts."
        ),
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


async def _complete_sales_rows(
    db: AsyncSession,
    start: Optional[datetime],
    source: str,
) -> List[Dict[str, Any]]:
    """Return the complete selected-period successful-sale dataset for CSV."""
    condition, params = date_condition("created_at", start, "sales_start")

    if source == "current":
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
            {condition.replace('created_at', 'p.created_at')}
            ORDER BY p.created_at DESC, p.id DESC
            """,
            params,
        )
        package_rows = await rows(db, "SELECT id, title FROM packages")
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
            {condition.replace('created_at', 'r.created_at')}
            ORDER BY r.created_at DESC, r.id DESC
            """,
            params,
        )
        package_rows = await rows(db, "SELECT id, title FROM subscription_packages")

    package_map = {int(row["id"]): row["title"] for row in package_rows}
    user_ids = sorted({
        int(row["user_id"])
        for row in raw_sales
        if row.get("user_id") is not None
    })
    users: Dict[int, Dict[str, Any]] = {}

    if user_ids:
        placeholders = ", ".join(f":uid_{i}" for i in range(len(user_ids)))
        user_rows = await rows(
            db,
            f"""
            SELECT id, full_name, email
            FROM users
            WHERE id IN ({placeholders})
            """,
            {f"uid_{i}": uid for i, uid in enumerate(user_ids)},
        )
        users = {int(row["id"]): row for row in user_rows}

    output = []
    for row in raw_sales:
        uid = int(row["user_id"]) if row.get("user_id") is not None else None
        pid = int(row["package_id"])
        amount = (
            money(row.get("amount_inr"))
            if source == "legacy"
            else money(row.get("amount_paise")) / 100.0
        )
        user = users.get(uid) if uid is not None else None
        output.append({
            "sale_id": int(row["id"]),
            "created_at": iso(row["created_at"]),
            "student_id": uid,
            "student_name": user.get("full_name") if user else None,
            "student_email": user.get("email") if user else None,
            "package_id": pid,
            "package_title": package_map.get(pid, f"Package #{pid}"),
            "amount_inr": amount,
            "status": normalize(row["status"]),
            "order_id": row.get("order_id"),
            "payment_id": row.get("payment_id"),
        })

    return output


@router.get("/report/sales/download")
async def download_sales_report(
    period: str = Query(default="3m"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await require_super_admin(current_user)

    period = period.lower().strip()
    start = period_start(period)
    source = await detect_sales_source(db, start)
    records = await _complete_sales_rows(db, start, source)

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
