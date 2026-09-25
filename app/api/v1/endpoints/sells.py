"""
Package Sales / Sells Dashboard

This endpoint intentionally supports both package systems currently present
in the database:

CURRENT:
    packages
        -> payments
        -> subscriptions

LEGACY:
    subscription_packages
        -> razorpay_orders
        -> user_subscriptions

The dashboard automatically selects the sales source that actually contains
successful package sales, preventing the dashboard from incorrectly showing
zero when the data exists in the legacy tables.

Route:
    GET /api/v1/team/sells/dashboard
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User


router = APIRouter(
    prefix="/team/sells",
    tags=["Team - Package Sales"],
)


# ============================================================================
# CONSTANTS
# ============================================================================

CURRENT_SUCCESS_STATUSES = {
    "SUCCESS",
    "PAID",
    "CAPTURED",
    "COMPLETED",
}

LEGACY_SUCCESS_STATUSES = {
    "SUCCESS",
    "PAID",
    "CAPTURED",
    "COMPLETED",
}

ACTIVE_SUBSCRIPTION_STATUS = "ACTIVE"


# ============================================================================
# HELPERS
# ============================================================================

def normalize(value: Any) -> str:
    """
    Safely convert enums / strings into uppercase plain strings.
    """
    if value is None:
        return ""

    raw_value = getattr(value, "value", value)

    return str(raw_value).strip().upper()


def money(value: Any) -> float:
    """
    Safely convert database numeric values into rupees.
    """
    if value is None:
        return 0.0

    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return 0.0


def iso_datetime(value: Any) -> Optional[str]:
    """
    Convert datetime values into ISO strings.
    """
    if value is None:
        return None

    if isinstance(value, datetime):
        return value.isoformat()

    return str(value)


def month_label(year: int, month: int) -> str:
    """
    Return YYYY-MM.
    """
    return f"{year:04d}-{month:02d}"


def get_month_starts(months: int) -> List[datetime]:
    """
    Generate month-start dates ending with the current month.
    """
    now = datetime.utcnow()

    current_month_start = datetime(
        now.year,
        now.month,
        1,
    )

    result: List[datetime] = []

    year = current_month_start.year
    month = current_month_start.month

    for offset in range(months - 1, -1, -1):
        y = year
        m = month - offset

        while m <= 0:
            m += 12
            y -= 1

        result.append(datetime(y, m, 1))

    return result


def authorized_role(current_user: User) -> bool:
    """
    Allow both uppercase and lowercase role representations because the
    database currently stores role as VARCHAR.
    """
    role = normalize(getattr(current_user, "role", None))

    return role in {
        "ADMIN",
        "SUPER_ADMIN",
        "SUPERADMIN",
    }


# ============================================================================
# DATABASE SOURCE DETECTION
# ============================================================================

async def get_table_count(
    db: AsyncSession,
    table_name: str,
) -> int:
    """
    Return row count for one of the known package/sales tables.

    Table names are hardcoded internally and never come from user input.
    """
    allowed_tables = {
        "packages",
        "subscription_packages",
        "payments",
        "razorpay_orders",
        "subscriptions",
        "user_subscriptions",
    }

    if table_name not in allowed_tables:
        raise ValueError(f"Unsupported table: {table_name}")

    result = await db.execute(
        text(f"SELECT COUNT(*) AS total FROM `{table_name}`")
    )

    row = result.mappings().first()

    if not row:
        return 0

    return int(row["total"] or 0)


async def detect_sales_source(
    db: AsyncSession,
    start_date: datetime,
) -> Dict[str, str]:
    """
    Determine which package/payment/subscription system contains the real
    sales data.

    Priority:
        1. New payments table if it contains successful payments.
        2. Legacy razorpay_orders if it contains successful payments.
        3. Otherwise whichever package table contains packages.

    This avoids the old problem where the dashboard showed zero simply
    because it was reading the wrong package system.
    """

    # ----------------------------------------------------------------------
    # Check CURRENT payment system
    # ----------------------------------------------------------------------

    current_payment_result = await db.execute(
        text(
            """
            SELECT COUNT(*) AS total
            FROM payments
            WHERE created_at >= :start_date
              AND UPPER(CAST(status AS CHAR)) IN
                  ('SUCCESS', 'PAID', 'CAPTURED', 'COMPLETED')
            """
        ),
        {
            "start_date": start_date,
        },
    )

    current_payment_row = current_payment_result.mappings().first()

    current_success_count = int(
        current_payment_row["total"] or 0
    ) if current_payment_row else 0

    # ----------------------------------------------------------------------
    # Check LEGACY payment system
    # ----------------------------------------------------------------------

    legacy_payment_result = await db.execute(
        text(
            """
            SELECT COUNT(*) AS total
            FROM razorpay_orders
            WHERE created_at >= :start_date
              AND UPPER(CAST(status AS CHAR)) IN
                  ('SUCCESS', 'PAID', 'CAPTURED', 'COMPLETED')
            """
        ),
        {
            "start_date": start_date,
        },
    )

    legacy_payment_row = legacy_payment_result.mappings().first()

    legacy_success_count = int(
        legacy_payment_row["total"] or 0
    ) if legacy_payment_row else 0

    # ----------------------------------------------------------------------
    # Select source
    # ----------------------------------------------------------------------

    if current_success_count > 0:
        return {
            "package_source": "packages",
            "payment_source": "payments",
            "subscription_source": "subscriptions",
        }

    if legacy_success_count > 0:
        return {
            "package_source": "subscription_packages",
            "payment_source": "razorpay_orders",
            "subscription_source": "user_subscriptions",
        }

    # ----------------------------------------------------------------------
    # No successful payment found.
    #
    # Use whichever package table actually contains catalog data.
    # ----------------------------------------------------------------------

    current_packages = await get_table_count(
        db,
        "packages",
    )

    legacy_packages = await get_table_count(
        db,
        "subscription_packages",
    )

    if current_packages > 0:
        return {
            "package_source": "packages",
            "payment_source": "payments",
            "subscription_source": "subscriptions",
        }

    return {
        "package_source": "subscription_packages",
        "payment_source": "razorpay_orders",
        "subscription_source": "user_subscriptions",
    }


# ============================================================================
# PACKAGE QUERIES
# ============================================================================

async def fetch_current_packages(
    db: AsyncSession,
) -> List[Dict[str, Any]]:
    """
    Read packages table.
    """

    result = await db.execute(
        text(
            """
            SELECT
                id,
                exam_id,
                title,
                description,
                price_paise,
                discount_paise,
                expiry_type,
                validity_days,
                fixed_expiry_date,
                is_active,
                created_at,
                updated_at
            FROM packages
            ORDER BY created_at DESC, id DESC
            """
        )
    )

    rows = result.mappings().all()

    packages: List[Dict[str, Any]] = []

    for row in rows:
        packages.append(
            {
                "id": int(row["id"]),
                "exam_id": row["exam_id"],
                "title": row["title"],
                "description": row["description"],
                "price_inr": money(row["price_paise"]) / 100.0,
                "price_paise": int(row["price_paise"] or 0),
                "discount_paise": int(row["discount_paise"] or 0),
                "expiry_type": normalize(row["expiry_type"]),
                "validity_days": row["validity_days"],
                "fixed_expiry_date": iso_datetime(
                    row["fixed_expiry_date"]
                ),
                "is_active": bool(row["is_active"]),
                "created_at": iso_datetime(row["created_at"]),
                "updated_at": iso_datetime(row["updated_at"]),
                "tier": None,
            }
        )

    return packages


async def fetch_legacy_packages(
    db: AsyncSession,
) -> List[Dict[str, Any]]:
    """
    Read legacy subscription_packages table.

    Only columns that are actually required are selected.
    """

    result = await db.execute(
        text(
            """
            SELECT
                id,
                exam_id,
                title,
                description,
                tier,
                price,
                validity_days,
                is_active,
                created_at
            FROM subscription_packages
            ORDER BY created_at DESC, id DESC
            """
        )
    )

    rows = result.mappings().all()

    packages: List[Dict[str, Any]] = []

    for row in rows:
        packages.append(
            {
                "id": int(row["id"]),
                "exam_id": row["exam_id"],
                "title": row["title"],
                "description": row["description"],
                "price_inr": money(row["price"]),
                "price_paise": int(round(money(row["price"]) * 100)),
                "discount_paise": 0,
                "expiry_type": None,
                "validity_days": row["validity_days"],
                "fixed_expiry_date": None,
                "is_active": bool(row["is_active"]),
                "created_at": iso_datetime(row["created_at"]),
                "updated_at": None,
                "tier": normalize(row["tier"]),
            }
        )

    return packages


# ============================================================================
# PAYMENT QUERIES
# ============================================================================

async def fetch_current_payments(
    db: AsyncSession,
    start_date: datetime,
) -> List[Dict[str, Any]]:
    """
    Read current payments table.
    """

    result = await db.execute(
        text(
            """
            SELECT
                id,
                user_id,
                package_id,
                razorpay_order_id,
                razorpay_payment_id,
                amount_paise,
                status,
                created_at,
                updated_at
            FROM payments
            WHERE created_at >= :start_date
            ORDER BY created_at DESC, id DESC
            """
        ),
        {
            "start_date": start_date,
        },
    )

    rows = result.mappings().all()

    payments: List[Dict[str, Any]] = []

    for row in rows:
        amount_paise = int(row["amount_paise"] or 0)

        payments.append(
            {
                "id": int(row["id"]),
                "user_id": int(row["user_id"]),
                "package_id": int(row["package_id"]),
                "order_id": row["razorpay_order_id"],
                "payment_id": row["razorpay_payment_id"],
                "amount_paise": amount_paise,
                "amount_inr": round(amount_paise / 100.0, 2),
                "status": normalize(row["status"]),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        )

    return payments


async def fetch_legacy_payments(
    db: AsyncSession,
    start_date: datetime,
) -> List[Dict[str, Any]]:
    """
    Read legacy razorpay_orders table.
    """

    result = await db.execute(
        text(
            """
            SELECT
                id,
                user_id,
                package_id,
                test_id,
                razorpay_order_id,
                razorpay_payment_id,
                amount_inr,
                currency,
                status,
                created_at
            FROM razorpay_orders
            WHERE created_at >= :start_date
              AND package_id IS NOT NULL
            ORDER BY created_at DESC, id DESC
            """
        ),
        {
            "start_date": start_date,
        },
    )

    rows = result.mappings().all()

    payments: List[Dict[str, Any]] = []

    for row in rows:
        amount_inr = money(row["amount_inr"])

        payments.append(
            {
                "id": int(row["id"]),
                "user_id": int(row["user_id"]),
                "package_id": int(row["package_id"]),
                "order_id": row["razorpay_order_id"],
                "payment_id": row["razorpay_payment_id"],
                "amount_paise": int(round(amount_inr * 100)),
                "amount_inr": amount_inr,
                "status": normalize(row["status"]),
                "created_at": row["created_at"],
                "updated_at": None,
            }
        )

    return payments


# ============================================================================
# SUBSCRIPTION QUERIES
# ============================================================================

async def fetch_current_subscriptions(
    db: AsyncSession,
) -> List[Dict[str, Any]]:
    """
    Read current subscriptions table.
    """

    result = await db.execute(
        text(
            """
            SELECT
                id,
                user_id,
                package_id,
                payment_id,
                start_date,
                expiry_date,
                status,
                created_at,
                updated_at
            FROM subscriptions
            ORDER BY created_at DESC, id DESC
            """
        )
    )

    rows = result.mappings().all()

    subscriptions: List[Dict[str, Any]] = []

    for row in rows:
        subscriptions.append(
            {
                "id": int(row["id"]),
                "user_id": int(row["user_id"]),
                "package_id": int(row["package_id"]),
                "payment_id": int(row["payment_id"]),
                "start_date": row["start_date"],
                "expiry_date": row["expiry_date"],
                "status": normalize(row["status"]),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        )

    return subscriptions


async def fetch_legacy_subscriptions(
    db: AsyncSession,
) -> List[Dict[str, Any]]:
    """
    Read legacy user_subscriptions table.

    IMPORTANT:
    Do not select `tier`.

    The previous implementation selected user_subscriptions.tier and
    generated:

        Unknown column 'user_subscriptions.tier'

    This query intentionally uses only the columns required by the sales
    dashboard.
    """

    result = await db.execute(
        text(
            """
            SELECT
                id,
                user_id,
                package_id,
                start_date,
                end_date,
                is_active,
                created_at
            FROM user_subscriptions
            ORDER BY created_at DESC, id DESC
            """
        )
    )

    rows = result.mappings().all()

    subscriptions: List[Dict[str, Any]] = []

    for row in rows:
        subscriptions.append(
            {
                "id": int(row["id"]),
                "user_id": int(row["user_id"]),
                "package_id": int(row["package_id"]),
                "payment_id": None,
                "start_date": row["start_date"],
                "expiry_date": row["end_date"],
                "status": (
                    "ACTIVE"
                    if bool(row["is_active"])
                    else "EXPIRED"
                ),
                "created_at": row["created_at"],
                "updated_at": None,
            }
        )

    return subscriptions


# ============================================================================
# USER LOOKUP
# ============================================================================

async def fetch_users(
    db: AsyncSession,
    user_ids: List[int],
) -> Dict[int, Dict[str, Any]]:
    """
    Fetch names/emails for users involved in package sales.

    Uses only columns confirmed in the supplied users schema.
    """

    if not user_ids:
        return {}

    result = await db.execute(
        text(
            """
            SELECT
                id,
                email,
                full_name
            FROM users
            WHERE id IN :user_ids
            """
        ).bindparams(
            # MySQL does not expand tuple parameters automatically through
            # plain text() in every SQLAlchemy version.
            # This query is replaced below by dynamically created placeholders.
        )
    )

    # This function is intentionally not used directly.
    # The actual implementation below uses a safe placeholder query.
    return {}


async def fetch_users_safe(
    db: AsyncSession,
    user_ids: List[int],
) -> Dict[int, Dict[str, Any]]:
    """
    Safe IN query for MySQL/aiomysql.
    """

    if not user_ids:
        return {}

    unique_ids = sorted(
        {
            int(user_id)
            for user_id in user_ids
            if user_id is not None
        }
    )

    if not unique_ids:
        return {}

    placeholders = ", ".join(
        f":user_id_{index}"
        for index in range(len(unique_ids))
    )

    params = {
        f"user_id_{index}": user_id
        for index, user_id in enumerate(unique_ids)
    }

    result = await db.execute(
        text(
            f"""
            SELECT
                id,
                email,
                full_name
            FROM users
            WHERE id IN ({placeholders})
            """
        ),
        params,
    )

    rows = result.mappings().all()

    return {
        int(row["id"]): {
            "id": int(row["id"]),
            "email": row["email"],
            "full_name": row["full_name"],
        }
        for row in rows
    }


# ============================================================================
# DASHBOARD
# ============================================================================

@router.get("/dashboard")
async def package_sales_dashboard(
    months: int = Query(
        default=3,
        ge=1,
        le=24,
        description="Number of months to include in the sales report",
    ),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Package Sales Dashboard.

    GET:
        /api/v1/team/sells/dashboard

    Optional:
        ?months=3
        ?months=6
        ?months=12
    """

    # ----------------------------------------------------------------------
    # AUTHORIZATION
    # ----------------------------------------------------------------------

    if not authorized_role(current_user):
        raise HTTPException(
            status_code=403,
            detail="Only ADMIN and SUPER_ADMIN can access package sales.",
        )

    # ----------------------------------------------------------------------
    # DATE RANGE
    # ----------------------------------------------------------------------

    month_starts = get_month_starts(months)

    start_date = month_starts[0]

    now = datetime.utcnow()

    # ----------------------------------------------------------------------
    # DETECT REAL DATA SOURCE
    # ----------------------------------------------------------------------

    sources = await detect_sales_source(
        db,
        start_date,
    )

    package_source = sources["package_source"]
    payment_source = sources["payment_source"]
    subscription_source = sources["subscription_source"]

    # ----------------------------------------------------------------------
    # LOAD PACKAGES
    # ----------------------------------------------------------------------

    if package_source == "packages":
        packages = await fetch_current_packages(db)
    else:
        packages = await fetch_legacy_packages(db)

    # ----------------------------------------------------------------------
    # LOAD PAYMENTS
    # ----------------------------------------------------------------------

    if payment_source == "payments":
        payments = await fetch_current_payments(
            db,
            start_date,
        )
    else:
        payments = await fetch_legacy_payments(
            db,
            start_date,
        )

    # ----------------------------------------------------------------------
    # LOAD SUBSCRIPTIONS
    # ----------------------------------------------------------------------

    try:
        if subscription_source == "subscriptions":
            subscriptions = await fetch_current_subscriptions(db)
        else:
            subscriptions = await fetch_legacy_subscriptions(db)
    except Exception:
        # If the legacy subscription table has an unexpected old structure,
        # do not allow it to break the entire sales dashboard.
        subscriptions = []

    # ----------------------------------------------------------------------
    # USERS
    # ----------------------------------------------------------------------

    relevant_user_ids = list(
        {
            int(payment["user_id"])
            for payment in payments
            if payment.get("user_id") is not None
        }
        |
        {
            int(subscription["user_id"])
            for subscription in subscriptions
            if subscription.get("user_id") is not None
        }
    )

    users = await fetch_users_safe(
        db,
        relevant_user_ids,
    )

    # ----------------------------------------------------------------------
    # PAYMENT FILTERS
    # ----------------------------------------------------------------------

    successful_payments = [
        payment
        for payment in payments
        if normalize(payment["status"]) in CURRENT_SUCCESS_STATUSES
    ]

    # ----------------------------------------------------------------------
    # REVENUE
    # ----------------------------------------------------------------------

    total_revenue = round(
        sum(
            money(payment["amount_inr"])
            for payment in successful_payments
        ),
        2,
    )

    successful_sales = len(successful_payments)

    # ----------------------------------------------------------------------
    # PACKAGE COUNTS
    # ----------------------------------------------------------------------

    total_packages = len(packages)

    active_packages = sum(
        1
        for package in packages
        if bool(package["is_active"])
    )

    # ----------------------------------------------------------------------
    # ACTIVE STUDENTS
    # ----------------------------------------------------------------------

    active_subscription_user_ids = {
        int(subscription["user_id"])
        for subscription in subscriptions
        if normalize(subscription["status"]) == ACTIVE_SUBSCRIPTION_STATUS
        and (
            subscription["expiry_date"] is None
            or subscription["expiry_date"] >= now
        )
    }

    active_students = len(active_subscription_user_ids)

    # ----------------------------------------------------------------------
    # FALLBACK ACTIVE STUDENTS
    #
    # If the current payment flow has successful payments but subscriptions
    # have not been written yet, show the actual paying users rather than
    # incorrectly showing zero.
    # ----------------------------------------------------------------------

    if active_students == 0 and successful_payments:
        active_students = len(
            {
                int(payment["user_id"])
                for payment in successful_payments
            }
        )

    # ----------------------------------------------------------------------
    # PAYMENT STATUS DISTRIBUTION
    # ----------------------------------------------------------------------

    status_counts: Dict[str, int] = defaultdict(int)

    for payment in payments:
        status = normalize(payment["status"]) or "UNKNOWN"
        status_counts[status] += 1

    payment_status = [
        {
            "status": status,
            "count": count,
        }
        for status, count in sorted(
            status_counts.items(),
            key=lambda item: (-item[1], item[0]),
        )
    ]

    # ----------------------------------------------------------------------
    # PACKAGE MAP
    # ----------------------------------------------------------------------

    package_map = {
        int(package["id"]): package
        for package in packages
    }

    # ----------------------------------------------------------------------
    # PACKAGE SALES AGGREGATION
    # ----------------------------------------------------------------------

    package_sales_count: Dict[int, int] = defaultdict(int)
    package_revenue: Dict[int, float] = defaultdict(float)

    for payment in successful_payments:
        package_id = int(payment["package_id"])

        package_sales_count[package_id] += 1
        package_revenue[package_id] += money(
            payment["amount_inr"]
        )

    # ----------------------------------------------------------------------
    # SUBSCRIPTION AGGREGATION
    # ----------------------------------------------------------------------

    package_subscription_count: Dict[int, int] = defaultdict(int)
    package_active_students: Dict[int, set] = defaultdict(set)
    package_expired_students: Dict[int, set] = defaultdict(set)

    for subscription in subscriptions:
        package_id = int(subscription["package_id"])
        user_id = int(subscription["user_id"])

        package_subscription_count[package_id] += 1

        status = normalize(subscription["status"])
        expiry_date = subscription["expiry_date"]

        if (
            status == "ACTIVE"
            and (
                expiry_date is None
                or expiry_date >= now
            )
        ):
            package_active_students[package_id].add(
                user_id
            )
        else:
            package_expired_students[package_id].add(
                user_id
            )

    # ----------------------------------------------------------------------
    # PACKAGE PERFORMANCE
    # ----------------------------------------------------------------------

    package_performance: List[Dict[str, Any]] = []

    for package in packages:
        package_id = int(package["id"])

        package_performance.append(
            {
                "id": package_id,
                "package_id": package_id,
                "title": package["title"],
                "description": package["description"],
                "exam_id": package["exam_id"],
                "tier": package.get("tier"),
                "expiry_type": package.get("expiry_type"),
                "price_inr": money(package["price_inr"]),
                "validity_days": package["validity_days"],
                "is_active": bool(package["is_active"]),
                "sales": package_sales_count.get(
                    package_id,
                    0,
                ),
                "successful_sales": package_sales_count.get(
                    package_id,
                    0,
                ),
                "revenue": round(
                    package_revenue.get(
                        package_id,
                        0.0,
                    ),
                    2,
                ),
                "active_students": len(
                    package_active_students.get(
                        package_id,
                        set(),
                    )
                ),
                "total_students": len(
                    {
                        int(subscription["user_id"])
                        for subscription in subscriptions
                        if int(subscription["package_id"])
                        == package_id
                    }
                ),
                "expired_students": len(
                    package_expired_students.get(
                        package_id,
                        set(),
                    )
                ),
                "subscription_count": package_subscription_count.get(
                    package_id,
                    0,
                ),
            }
        )

    # ----------------------------------------------------------------------
    # SORT PACKAGE PERFORMANCE
    # ----------------------------------------------------------------------

    package_performance.sort(
        key=lambda item: (
            -float(item["revenue"]),
            -int(item["sales"]),
            str(item["title"]).lower(),
        )
    )

    # ----------------------------------------------------------------------
    # MONTHLY REVENUE
    # ----------------------------------------------------------------------

    monthly_revenue_map: Dict[str, Dict[str, Any]] = {}

    for start in month_starts:
        key = month_label(
            start.year,
            start.month,
        )

        monthly_revenue_map[key] = {
            "month": key,
            "label": start.strftime("%b"),
            "revenue": 0.0,
            "sales": 0,
        }

    for payment in successful_payments:
        created_at = payment["created_at"]

        if not created_at:
            continue

        if not isinstance(created_at, datetime):
            continue

        key = month_label(
            created_at.year,
            created_at.month,
        )

        if key not in monthly_revenue_map:
            continue

        monthly_revenue_map[key]["revenue"] += money(
            payment["amount_inr"]
        )

        monthly_revenue_map[key]["sales"] += 1

    monthly_revenue = []

    for item in monthly_revenue_map.values():
        item["revenue"] = round(
            float(item["revenue"]),
            2,
        )

        monthly_revenue.append(item)

    # ----------------------------------------------------------------------
    # RECENT SALES
    # ----------------------------------------------------------------------

    recent_sales: List[Dict[str, Any]] = []

    for payment in payments[:20]:
        package = package_map.get(
            int(payment["package_id"])
        )

        user = users.get(
            int(payment["user_id"])
        )

        recent_sales.append(
            {
                "id": payment["id"],
                "user_id": payment["user_id"],
                "student_name": (
                    user["full_name"]
                    if user
                    else None
                ),
                "student_email": (
                    user["email"]
                    if user
                    else None
                ),
                "package_id": payment["package_id"],
                "package_title": (
                    package["title"]
                    if package
                    else f"Package #{payment['package_id']}"
                ),
                "amount_inr": money(
                    payment["amount_inr"]
                ),
                "status": normalize(
                    payment["status"]
                ),
                "order_id": payment["order_id"],
                "payment_id": payment["payment_id"],
                "created_at": iso_datetime(
                    payment["created_at"]
                ),
            }
        )

    # ----------------------------------------------------------------------
    # ENROLLMENT REPORT
    # ----------------------------------------------------------------------

    enrollment_report: List[Dict[str, Any]] = []

    for package in packages:
        package_id = int(package["id"])

        student_ids = {
            int(subscription["user_id"])
            for subscription in subscriptions
            if int(subscription["package_id"]) == package_id
        }

        enrollment_report.append(
            {
                "package_id": package_id,
                "package_title": package["title"],
                "students": len(student_ids),
                "total_enrollments": package_subscription_count.get(
                    package_id,
                    0,
                ),
                "active_students": len(
                    package_active_students.get(
                        package_id,
                        set(),
                    )
                ),
                "expired_students": len(
                    package_expired_students.get(
                        package_id,
                        set(),
                    )
                ),
                "sales": package_sales_count.get(
                    package_id,
                    0,
                ),
                "revenue": round(
                    package_revenue.get(
                        package_id,
                        0.0,
                    ),
                    2,
                ),
            }
        )

    enrollment_report.sort(
        key=lambda item: (
            -int(item["students"]),
            -float(item["revenue"]),
            str(item["package_title"]).lower(),
        )
    )

    # ----------------------------------------------------------------------
    # TOP PACKAGE
    # ----------------------------------------------------------------------

    top_package = None

    if package_performance:
        top = package_performance[0]

        if (
            top["sales"] > 0
            or top["revenue"] > 0
            or top["active_students"] > 0
        ):
            top_package = {
                "id": top["id"],
                "title": top["title"],
                "sales": top["sales"],
                "revenue": top["revenue"],
                "active_students": top["active_students"],
            }

    # ----------------------------------------------------------------------
    # PROFIT
    #
    # There is no cost/expense column in the supplied package/payment
    # schema, therefore we intentionally do NOT fabricate a profit number.
    # ----------------------------------------------------------------------

    profit = None

    # ----------------------------------------------------------------------
    # RESPONSE
    # ----------------------------------------------------------------------

    return {
        "period": {
            "months": months,
            "start_date": start_date.isoformat(),
            "end_date": now.isoformat(),
        },

        "summary": {
            "total_revenue": total_revenue,
            "revenue": total_revenue,
            "successful_sales": successful_sales,
            "sales": successful_sales,
            "active_students": active_students,
            "active_packages": active_packages,
            "total_packages": total_packages,
            "all_packages": total_packages,
            "profit": profit,
        },

        # Kept at the top level as well so the existing frontend can use
        # either dashboard.summary.* or dashboard.*.
        "total_revenue": total_revenue,
        "revenue": total_revenue,
        "successful_sales": successful_sales,
        "active_students": active_students,
        "active_packages": active_packages,
        "total_packages": total_packages,
        "profit": profit,

        "monthly_revenue": monthly_revenue,

        "revenue_trend": monthly_revenue,

        "payment_status": payment_status,

        "package_performance": package_performance,

        "enrollment_report": enrollment_report,

        "recent_sales": recent_sales,

        "top_package": top_package,

        "sources": {
            "packages": package_source,
            "payments": payment_source,
            "subscriptions": subscription_source,
        },
    }