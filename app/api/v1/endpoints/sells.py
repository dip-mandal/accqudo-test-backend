from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_db, get_current_user

from app.models.user import User

# These are the models that map to the current sales tables:
#
# packages
# payments
# subscriptions
#
from app.models.payment import (
    Package,
    Payment,
    Subscription,
)


router = APIRouter(
    prefix="/team/sells",
    tags=["Team - Package Sales"],
)


# ============================================================
# Constants
# ============================================================

SUCCESSFUL_PAYMENT_STATUSES = {
    "SUCCESS",
    "PAID",
    "CAPTURED",
    "COMPLETED",
    "SUCCESSFUL",
}

ACTIVE_SUBSCRIPTION_STATUSES = {
    "ACTIVE",
}


# ============================================================
# Authorization
# ============================================================

def require_admin_or_super_admin(
    current_user: User = Depends(get_current_user),
) -> User:
    """
    Only ADMIN and SUPER_ADMIN can access package sales.
    """

    role = str(
        getattr(current_user, "role", "") or ""
    ).strip().upper()

    if role not in {
        "ADMIN",
        "SUPER_ADMIN",
        "SUPERADMIN",
    }:
        raise HTTPException(
            status_code=403,
            detail="Admin or Super Admin access required.",
        )

    return current_user


# ============================================================
# Generic helpers
# ============================================================

def get_value(
    obj: Any,
    *field_names: str,
    default: Any = None,
) -> Any:
    """
    Safely get the first available attribute.
    """

    if obj is None:
        return default

    for field_name in field_names:
        if hasattr(obj, field_name):
            value = getattr(obj, field_name)

            if value is not None:
                return value

    return default


def to_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def to_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def normalize_status(value: Any) -> str:
    """
    Supports SQLAlchemy Enum values as well as strings.
    """

    if value is None:
        return ""

    # Python enum
    enum_value = getattr(
        value,
        "value",
        None,
    )

    if enum_value is not None:
        return str(
            enum_value
        ).strip().upper()

    return str(
        value
    ).strip().upper()


def serialize_datetime(
    value: Any,
) -> Optional[str]:

    if value is None:
        return None

    if isinstance(
        value,
        datetime,
    ):
        return value.isoformat()

    return str(value)


def get_id(
    obj: Any,
) -> Any:
    return getattr(
        obj,
        "id",
        None,
    )


# ============================================================
# Package helpers
# ============================================================

def get_package_title(
    package: Any,
) -> str:

    return str(
        get_value(
            package,
            "title",
            "name",
            default="Package",
        )
    )


def get_package_price_paise(
    package: Any,
) -> int:

    return to_int(
        get_value(
            package,
            "price_paise",
            default=0,
        )
    )


def paise_to_rupees(
    value: Any,
) -> float:

    return round(
        to_float(value) / 100.0,
        2,
    )


# ============================================================
# Payment helpers
# ============================================================

def get_payment_amount_paise(
    payment: Any,
) -> int:

    return to_int(
        get_value(
            payment,
            "amount_paise",
            default=0,
        )
    )


def get_payment_amount_inr(
    payment: Any,
) -> float:

    return paise_to_rupees(
        get_payment_amount_paise(
            payment
        )
    )


def get_payment_status(
    payment: Any,
) -> str:

    return normalize_status(
        get_value(
            payment,
            "status",
            default="",
        )
    )


def is_successful_payment(
    payment: Any,
) -> bool:

    return (
        get_payment_status(payment)
        in SUCCESSFUL_PAYMENT_STATUSES
    )


# ============================================================
# Subscription helpers
# ============================================================

def get_subscription_status(
    subscription: Any,
) -> str:

    return normalize_status(
        get_value(
            subscription,
            "status",
            default="",
        )
    )


def is_active_subscription(
    subscription: Any,
    now: datetime,
) -> bool:

    status = get_subscription_status(
        subscription
    )

    if status not in ACTIVE_SUBSCRIPTION_STATUSES:
        return False

    expiry_date = get_value(
        subscription,
        "expiry_date",
        default=None,
    )

    if isinstance(
        expiry_date,
        datetime,
    ):
        if expiry_date < now:
            return False

    return True


# ============================================================
# Dashboard
# ============================================================

@router.get("/dashboard")
async def get_package_sales_dashboard(
    months: int = Query(
        default=12,
        ge=1,
        le=36,
        description="Number of months to include in the report.",
    ),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(
        require_admin_or_super_admin
    ),
) -> Dict[str, Any]:

    now = datetime.utcnow()

    start_date = now - timedelta(
        days=months * 31
    )

    # ========================================================
    # LOAD PACKAGES
    # ========================================================

    packages_result = await db.execute(
        select(Package)
    )

    packages = list(
        packages_result.scalars().all()
    )

    # ========================================================
    # LOAD PAYMENTS
    # ========================================================

    payments_result = await db.execute(
        select(Payment)
    )

    payments = list(
        payments_result.scalars().all()
    )

    # ========================================================
    # LOAD SUBSCRIPTIONS
    # ========================================================

    subscriptions_result = await db.execute(
        select(Subscription)
    )

    subscriptions = list(
        subscriptions_result.scalars().all()
    )

    # ========================================================
    # ACTIVE PACKAGES
    # ========================================================

    active_packages = sum(
        1
        for package in packages
        if bool(
            get_value(
                package,
                "is_active",
                default=True,
            )
        )
    )

    # ========================================================
    # SUCCESSFUL PAYMENTS
    # ========================================================

    successful_payments = [
        payment
        for payment in payments
        if is_successful_payment(payment)
    ]

    # ========================================================
    # TOTAL REVENUE
    # ========================================================

    total_revenue_paise = sum(
        get_payment_amount_paise(
            payment
        )
        for payment in successful_payments
    )

    total_revenue = paise_to_rupees(
        total_revenue_paise
    )

    total_sales = len(
        successful_payments
    )

    # ========================================================
    # ACTIVE SUBSCRIPTIONS
    # ========================================================

    active_subscriptions = [
        subscription
        for subscription in subscriptions
        if is_active_subscription(
            subscription,
            now,
        )
    ]

    # ========================================================
    # UNIQUE ACTIVE STUDENTS
    # ========================================================

    active_student_ids = set()

    for subscription in active_subscriptions:

        user_id = get_value(
            subscription,
            "user_id",
            default=None,
        )

        if user_id is not None:
            active_student_ids.add(
                str(user_id)
            )

    # ========================================================
    # PACKAGE PERFORMANCE
    # ========================================================

    package_rows: List[
        Dict[str, Any]
    ] = []

    for package in packages:

        package_id = get_id(
            package
        )

        package_id_string = (
            str(package_id)
            if package_id is not None
            else None
        )

        # ----------------------------------------------------
        # Successful payments for this package
        # ----------------------------------------------------

        package_payments = []

        for payment in successful_payments:

            payment_package_id = get_value(
                payment,
                "package_id",
                default=None,
            )

            if (
                payment_package_id is not None
                and package_id_string is not None
                and str(payment_package_id)
                == package_id_string
            ):
                package_payments.append(
                    payment
                )

        sales_count = len(
            package_payments
        )

        package_revenue_paise = sum(
            get_payment_amount_paise(
                payment
            )
            for payment in package_payments
        )

        package_revenue = paise_to_rupees(
            package_revenue_paise
        )

        # ----------------------------------------------------
        # Subscriptions for this package
        # ----------------------------------------------------

        package_subscriptions = []

        for subscription in subscriptions:

            subscription_package_id = (
                get_value(
                    subscription,
                    "package_id",
                    default=None,
                )
            )

            if (
                subscription_package_id is not None
                and package_id_string is not None
                and str(
                    subscription_package_id
                )
                == package_id_string
            ):
                package_subscriptions.append(
                    subscription
                )

        # ----------------------------------------------------
        # Student counts
        # ----------------------------------------------------

        total_student_ids = set()

        active_student_ids_for_package = set()

        expired_student_ids_for_package = set()

        for subscription in package_subscriptions:

            user_id = get_value(
                subscription,
                "user_id",
                default=None,
            )

            if user_id is None:
                continue

            user_id_string = str(
                user_id
            )

            total_student_ids.add(
                user_id_string
            )

            if is_active_subscription(
                subscription,
                now,
            ):

                active_student_ids_for_package.add(
                    user_id_string
                )

            else:

                expired_student_ids_for_package.add(
                    user_id_string
                )

        # ----------------------------------------------------
        # Package result
        # ----------------------------------------------------

        package_rows.append(
            {
                "id": package_id,

                "title": get_package_title(
                    package
                ),

                "description": get_value(
                    package,
                    "description",
                    default=None,
                ),

                "tier": str(
                    get_value(
                        package,
                        "expiry_type",
                        default="",
                    )
                ),

                "price": paise_to_rupees(
                    get_package_price_paise(
                        package
                    )
                ),

                "price_paise": get_package_price_paise(
                    package
                ),

                "discount_paise": to_int(
                    get_value(
                        package,
                        "discount_paise",
                        default=0,
                    )
                ),

                "validity_days": get_value(
                    package,
                    "validity_days",
                    default=None,
                ),

                "expiry_type": str(
                    get_value(
                        package,
                        "expiry_type",
                        default="DURATION",
                    )
                ),

                "is_active": bool(
                    get_value(
                        package,
                        "is_active",
                        default=True,
                    )
                ),

                "created_at": serialize_datetime(
                    get_value(
                        package,
                        "created_at",
                        default=None,
                    )
                ),

                "sales_count": sales_count,

                "revenue": package_revenue,

                "revenue_paise": package_revenue_paise,

                "active_students": len(
                    active_student_ids_for_package
                ),

                "total_students": len(
                    total_student_ids
                ),

                "expired_students": len(
                    expired_student_ids_for_package
                ),
            }
        )

    # ========================================================
    # MONTHLY REVENUE
    # ========================================================

    monthly_revenue: List[
        Dict[str, Any]
    ] = []

    cursor = datetime(
        start_date.year,
        start_date.month,
        1,
    )

    while cursor <= now:

        if cursor.month == 12:

            next_month = datetime(
                cursor.year + 1,
                1,
                1,
            )

        else:

            next_month = datetime(
                cursor.year,
                cursor.month + 1,
                1,
            )

        month_revenue_paise = 0

        month_sales = 0

        for payment in successful_payments:

            created_at = get_value(
                payment,
                "created_at",
                default=None,
            )

            if not isinstance(
                created_at,
                datetime,
            ):
                continue

            if (
                created_at >= cursor
                and created_at < next_month
            ):

                month_sales += 1

                month_revenue_paise += (
                    get_payment_amount_paise(
                        payment
                    )
                )

        monthly_revenue.append(
            {
                "month": cursor.strftime(
                    "%Y-%m"
                ),

                "label": cursor.strftime(
                    "%b %Y"
                ),

                "sales": month_sales,

                "revenue": paise_to_rupees(
                    month_revenue_paise
                ),

                "revenue_paise": month_revenue_paise,
            }
        )

        cursor = next_month

    # ========================================================
    # PAYMENT STATUS SUMMARY
    # ========================================================

    payment_status_map: Dict[
        str,
        Dict[str, Any],
    ] = {}

    for payment in payments:

        status = get_payment_status(
            payment
        )

        if not status:
            status = "UNKNOWN"

        if status not in payment_status_map:

            payment_status_map[
                status
            ] = {
                "status": status,
                "count": 0,
                "amount": 0.0,
                "amount_paise": 0,
            }

        amount_paise = (
            get_payment_amount_paise(
                payment
            )
        )

        payment_status_map[
            status
        ]["count"] += 1

        payment_status_map[
            status
        ]["amount_paise"] += (
            amount_paise
        )

        payment_status_map[
            status
        ]["amount"] = paise_to_rupees(
            payment_status_map[
                status
            ]["amount_paise"]
        )

    payment_status = list(
        payment_status_map.values()
    )

    # ========================================================
    # LOAD USERS
    # ========================================================

    users_result = await db.execute(
        select(User)
    )

    users = list(
        users_result.scalars().all()
    )

    users_by_id = {
        str(get_id(user)): user
        for user in users
        if get_id(user) is not None
    }

    # ========================================================
    # PACKAGES MAP
    # ========================================================

    packages_by_id = {
        str(get_id(package)): package
        for package in packages
        if get_id(package) is not None
    }

    # ========================================================
    # RECENT SALES
    # ========================================================

    sorted_payments = sorted(
        payments,
        key=lambda payment: (
            get_value(
                payment,
                "created_at",
                default=datetime.min,
            )
            if isinstance(
                get_value(
                    payment,
                    "created_at",
                    default=None,
                ),
                datetime,
            )
            else datetime.min
        ),
        reverse=True,
    )

    recent_sales = []

    for payment in sorted_payments[:25]:

        user_id = get_value(
            payment,
            "user_id",
            default=None,
        )

        package_id = get_value(
            payment,
            "package_id",
            default=None,
        )

        user = (
            users_by_id.get(
                str(user_id)
            )
            if user_id is not None
            else None
        )

        package = (
            packages_by_id.get(
                str(package_id)
            )
            if package_id is not None
            else None
        )

        recent_sales.append(
            {
                "id": get_id(
                    payment
                ),

                "user_id": user_id,

                "student_name": (
                    str(
                        get_value(
                            user,
                            "full_name",
                            default="Student",
                        )
                    )
                    if user
                    else "Student"
                ),

                "student_email": (
                    get_value(
                        user,
                        "email",
                        default=None,
                    )
                    if user
                    else None
                ),

                "package_id": package_id,

                "package_title": (
                    get_package_title(
                        package
                    )
                    if package
                    else "Package"
                ),

                "amount": get_payment_amount_inr(
                    payment
                ),

                "amount_paise": (
                    get_payment_amount_paise(
                        payment
                    )
                ),

                "currency": str(
                    get_value(
                        payment,
                        "currency",
                        default="INR",
                    )
                ),

                "status": str(
                    get_value(
                        payment,
                        "status",
                        default="UNKNOWN",
                    )
                ),

                "razorpay_order_id": get_value(
                    payment,
                    "razorpay_order_id",
                    default=None,
                ),

                "razorpay_payment_id": get_value(
                    payment,
                    "razorpay_payment_id",
                    default=None,
                ),

                "created_at": serialize_datetime(
                    get_value(
                        payment,
                        "created_at",
                        default=None,
                    )
                ),
            }
        )

    # ========================================================
    # TOP PACKAGE BY REVENUE
    # ========================================================

    top_package = None

    if package_rows:

        top_package = max(
            package_rows,
            key=lambda item: item[
                "revenue_paise"
            ],
        )

    # ========================================================
    # TOP PACKAGE BY ENROLLMENTS
    # ========================================================

    top_package_by_students = None

    if package_rows:

        top_package_by_students = max(
            package_rows,
            key=lambda item: item[
                "total_students"
            ],
        )

    # ========================================================
    # PACKAGE SUMMARY
    # ========================================================

    total_package_students = sum(
        item["total_students"]
        for item in package_rows
    )

    total_active_package_students = sum(
        item["active_students"]
        for item in package_rows
    )

    # ========================================================
    # RESPONSE
    # ========================================================

    return {
        "generated_at": now.isoformat(),

        "report_period": {
            "months": months,
            "start_date": start_date.isoformat(),
            "end_date": now.isoformat(),
        },

        "overview": {
            "active_packages": active_packages,

            "total_packages": len(
                packages
            ),

            "total_sales": total_sales,

            "total_revenue": total_revenue,

            "total_revenue_paise": total_revenue_paise,

            "gross_revenue": total_revenue,

            "active_students": len(
                active_subscriptions
            ),

            "unique_active_students": len(
                active_student_ids
            ),

            "total_package_students": (
                total_package_students
            ),

            "total_active_package_students": (
                total_active_package_students
            ),

            # There is currently no expense/cost field
            # in the models supplied by you, so we do not
            # fabricate a profit value.
            "profit": None,
        },

        "top_package": top_package,

        "top_package_by_students": (
            top_package_by_students
        ),

        "packages": package_rows,

        "monthly_revenue": monthly_revenue,

        "payment_status": payment_status,

        "recent_sales": recent_sales,
    }