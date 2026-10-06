from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    status,
)
from fastapi.responses import StreamingResponse

from sqlalchemy import (
    select,
    func,
    desc,
    inspect,
)
from sqlalchemy.ext.asyncio import AsyncSession

from typing import (
    Dict,
    Any,
    AsyncGenerator,
)

from datetime import datetime, timezone, timedelta

import csv
import io
import re


from app.core.database import (
    get_db,
    Base,
    engine,
)

from app.core.security import get_current_user

from app.models.user import User
from app.models.audit import AuditTrafficLog


router = APIRouter(
    tags=["Super Admin Studio"]
)


# ============================================================
# CONSTANTS
# ============================================================

SUPER_ADMIN_EMAILS = {
    "dm9475511@gmail.com",
}


# These are the status codes that the Super Admin UI is
# explicitly allowed to delete individually.
DELETABLE_TRAFFIC_STATUSES = {
    200,
    400,
    401,
    403,
    404,
    405,
    422,
    500,
}


# ============================================================
# SUPER ADMIN AUTHORIZATION
# ============================================================

async def verify_super_admin(
    current_user: User = Depends(get_current_user),
) -> User:
    """
    Strictly restrict access to designated Super Admin and
    authorized administrator accounts.
    """

    email = (
        str(getattr(current_user, "email", "") or "")
        .strip()
        .lower()
    )

    user_role = str(
        getattr(current_user, "role", "") or ""
    ).strip().lower()

    is_admin_privileged = (
        bool(getattr(current_user, "is_admin", False))
        or user_role in {
            "super_admin",
            "superadmin",
            "admin",
        }
    )

    if (
        email not in SUPER_ADMIN_EMAILS
        and not is_admin_privileged
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied. Super Admin privileges required.",
        )

    return current_user


# ============================================================
# SYSTEM OVERVIEW
# ============================================================

@router.get("/super-admin/overview")
async def get_system_overview(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(verify_super_admin),
):
    """
    High-level system vitals and counts.
    """

    users_count = await db.scalar(
        select(func.count(User.id))
    )

    traffic_count = await db.scalar(
        select(func.count(AuditTrafficLog.id))
    )

    yesterday = (
        datetime.now(timezone.utc)
        - timedelta(days=1)
    )

    recent_traffic = await db.scalar(
        select(func.count(AuditTrafficLog.id)).where(
            AuditTrafficLog.created_at >= yesterday
        )
    )

    return {
        "total_users": users_count or 0,
        "total_audit_logs": traffic_count or 0,
        "traffic_last_24h": recent_traffic or 0,
        "database_engine": str(engine.url).split("://")[0],
    }


# ============================================================
# TRAFFIC ANALYTICS
# ============================================================

@router.get("/super-admin/traffic")
async def get_traffic_analytics(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(verify_super_admin),
):
    """
    Detailed traffic logs.

    Returns:
    - total request count
    - unique visitor IP count
    - status-code aggregation
    - latest 100 logs
    """

    # --------------------------------------------------------
    # Latest logs
    # --------------------------------------------------------

    stmt = (
        select(AuditTrafficLog)
        .order_by(desc(AuditTrafficLog.id))
        .limit(100)
    )

    result = await db.execute(stmt)

    logs = result.scalars().all()

    # --------------------------------------------------------
    # Total requests
    # --------------------------------------------------------

    total_requests = await db.scalar(
        select(func.count(AuditTrafficLog.id))
    )

    # --------------------------------------------------------
    # Unique IP addresses
    # --------------------------------------------------------

    unique_ips = await db.scalar(
        select(
            func.count(
                func.distinct(
                    AuditTrafficLog.ip_address
                )
            )
        )
    )

    # --------------------------------------------------------
    # Status-code summary
    # --------------------------------------------------------

    status_result = await db.execute(
        select(
            AuditTrafficLog.status_code,
            func.count(AuditTrafficLog.id),
        )
        .group_by(AuditTrafficLog.status_code)
        .order_by(AuditTrafficLog.status_code)
    )

    status_summary = [
        {
            "status_code": int(status_code),
            "count": int(count),
        }
        for status_code, count in status_result.all()
    ]

    # --------------------------------------------------------
    # Format latest logs
    # --------------------------------------------------------

    formatted_logs = []

    for log in logs:
        formatted_logs.append(
            {
                "id": log.id,
                "path": log.path,
                "method": log.method,
                "status_code": log.status_code,
                "ip_address": log.ip_address,
                "user_agent": log.user_agent,
                "response_time_ms": log.response_time_ms,
                "timestamp": (
                    log.created_at.isoformat()
                    if getattr(log, "created_at", None)
                    else None
                ),
            }
        )

    return {
        "total_requests": total_requests or 0,
        "unique_visitors": unique_ips or 0,
        "status_summary": status_summary,
        "logs": formatted_logs,
    }


# ============================================================
# TRAFFIC LOG EXPORT
# ============================================================

def _safe_log_value(value: Any) -> str:
    """
    Convert a database value into a safe single-line value.

    Newline characters are escaped so a malicious User-Agent
    or path cannot create fake additional log entries.
    """

    if value is None:
        return "-"

    value = str(value)

    value = value.replace(
        "\r",
        "\\r",
    ).replace(
        "\n",
        "\\n",
    )

    return value


def _safe_filename_timestamp() -> str:
    """
    Generates a filesystem-safe UTC timestamp.
    """

    return datetime.now(
        timezone.utc
    ).strftime(
        "%Y%m%d-%H%M%S"
    )


@router.get("/super-admin/traffic/export")
async def export_traffic_logs(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(verify_super_admin),
):
    """
    Download the complete audit_traffic_logs table as a .log file.

    Every database record is exported.
    """

    async def generate_log() -> AsyncGenerator[str, None]:
        # ----------------------------------------------------
        # Header
        # ----------------------------------------------------

        yield (
            "# Accqudo Traffic Audit Log\n"
            f"# Exported UTC: "
            f"{datetime.now(timezone.utc).isoformat()}\n"
            "# Source: audit_traffic_logs\n"
            "# Format: id | timestamp | method | path | "
            "status_code | ip_address | response_time_ms | user_agent\n"
            "# ------------------------------------------------------------\n"
        )

        # ----------------------------------------------------
        # Stream all rows ordered by ID
        # ----------------------------------------------------

        result = await db.stream(
            select(AuditTrafficLog).order_by(
                AuditTrafficLog.id.asc()
            )
        )

        async for log in result.scalars():

            timestamp = (
                log.created_at.isoformat()
                if getattr(log, "created_at", None)
                else "-"
            )

            line = (
                f"id={_safe_log_value(log.id)} | "
                f"timestamp={_safe_log_value(timestamp)} | "
                f"method={_safe_log_value(log.method)} | "
                f"path={_safe_log_value(log.path)} | "
                f"status_code={_safe_log_value(log.status_code)} | "
                f"ip_address={_safe_log_value(log.ip_address)} | "
                f"response_time_ms={_safe_log_value(log.response_time_ms)} | "
                f"user_agent={_safe_log_value(log.user_agent)}\n"
            )

            yield line

    filename = (
        f"accqudo-traffic-logs-"
        f"{_safe_filename_timestamp()}.log"
    )

    return StreamingResponse(
        generate_log(),
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{filename}"'
            ),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


# ============================================================
# DELETE TRAFFIC LOGS BY STATUS
# ============================================================

@router.delete(
    "/super-admin/traffic/status/{status_code}"
)
async def delete_traffic_logs_by_status(
    status_code: int,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(verify_super_admin),
):
    """
    Delete traffic logs belonging to one explicitly allowed
    HTTP status code.

    Example:

        DELETE /super-admin/traffic/status/404

    Only these status codes are permitted:

        200
        400
        401
        403
        404
        405
        422
        500
    """

    # --------------------------------------------------------
    # Security allow-list
    # --------------------------------------------------------

    if status_code not in DELETABLE_TRAFFIC_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "This status code cannot be deleted through "
                "the traffic management endpoint."
            ),
        )

    # --------------------------------------------------------
    # Count first
    # --------------------------------------------------------

    existing_count = await db.scalar(
        select(
            func.count(AuditTrafficLog.id)
        ).where(
            AuditTrafficLog.status_code == status_code
        )
    )

    existing_count = int(existing_count or 0)

    if existing_count == 0:
        return {
            "success": True,
            "status_code": status_code,
            "deleted_count": 0,
            "message": (
                f"No traffic logs found with status "
                f"{status_code}."
            ),
        }

    # --------------------------------------------------------
    # Delete
    # --------------------------------------------------------

    await db.execute(
        AuditTrafficLog.__table__.delete().where(
            AuditTrafficLog.status_code == status_code
        )
    )

    await db.commit()

    return {
        "success": True,
        "status_code": status_code,
        "deleted_count": existing_count,
        "message": (
            f"Deleted {existing_count} traffic log "
            f"record(s) with status {status_code}."
        ),
    }


# ============================================================
# DATABASE SCHEMA INSPECTION
# ============================================================

@router.get("/super-admin/database-schema")
async def inspect_database_schema(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(verify_super_admin),
):
    """
    Inspect database structure using SQLAlchemy metadata.

    Returns:

    - tables
    - columns
    - data types
    - nullable
    - primary keys
    - foreign keys
    - indexes
    - unique constraints
    - table row counts
    """

    schema_info = []

    # --------------------------------------------------------
    # Iterate over registered SQLAlchemy tables
    # --------------------------------------------------------

    for table_name, table in Base.metadata.tables.items():

        columns = []

        for col in table.columns:

            columns.append(
                {
                    "name": col.name,
                    "type": str(col.type),
                    "nullable": bool(col.nullable),
                    "primary_key": bool(col.primary_key),
                    "autoincrement": (
                        str(col.autoincrement)
                    ),
                    "default": (
                        str(col.default.arg)
                        if col.default is not None
                        and hasattr(col.default, "arg")
                        else None
                    ),
                    "server_default": (
                        str(col.server_default.arg)
                        if col.server_default is not None
                        and hasattr(
                            col.server_default,
                            "arg",
                        )
                        else None
                    ),
                    "foreign_keys": [
                        str(fk.target_fullname)
                        for fk in col.foreign_keys
                    ],
                }
            )

        # ----------------------------------------------------
        # Indexes
        # ----------------------------------------------------

        indexes = []

        for index in table.indexes:
            indexes.append(
                {
                    "name": index.name,
                    "unique": bool(index.unique),
                    "columns": [
                        column.name
                        for column in index.columns
                    ],
                }
            )

        # ----------------------------------------------------
        # Unique constraints
        # ----------------------------------------------------

        unique_constraints = []

        for constraint in table.constraints:
            if (
                constraint.__class__.__name__
                == "UniqueConstraint"
            ):
                unique_constraints.append(
                    {
                        "name": constraint.name,
                        "columns": [
                            column.name
                            for column in constraint.columns
                        ],
                    }
                )

        # ----------------------------------------------------
        # Primary key information
        # ----------------------------------------------------

        primary_key_columns = [
            column.name
            for column in table.primary_key.columns
        ]

        # ----------------------------------------------------
        # Foreign key information
        # ----------------------------------------------------

        foreign_keys = []

        for constraint in table.foreign_key_constraints:
            foreign_keys.append(
                {
                    "name": constraint.name,
                    "columns": [
                        column.name
                        for column in constraint.columns
                    ],
                    "references": [
                        str(
                            element.target_fullname
                        )
                        for element in constraint.elements
                    ],
                }
            )

        # ----------------------------------------------------
        # Row count
        # ----------------------------------------------------

        try:
            row_count = await db.scalar(
                select(
                    func.count()
                ).select_from(table)
            )

            row_count = int(row_count or 0)

        except Exception:
            row_count = None

        schema_info.append(
            {
                "table_name": table_name,
                "columns_count": len(columns),
                "row_count": row_count,
                "primary_key_columns": primary_key_columns,
                "columns": columns,
                "indexes": indexes,
                "unique_constraints": unique_constraints,
                "foreign_keys": foreign_keys,
            }
        )

    # --------------------------------------------------------
    # Traffic-specific inspection
    # --------------------------------------------------------

    traffic_inspection = None

    traffic_table = Base.metadata.tables.get(
        AuditTrafficLog.__tablename__
    )

    if traffic_table is not None:

        traffic_total = await db.scalar(
            select(
                func.count(AuditTrafficLog.id)
            )
        )

        traffic_unique_ips = await db.scalar(
            select(
                func.count(
                    func.distinct(
                        AuditTrafficLog.ip_address
                    )
                )
            )
        )

        traffic_status_result = await db.execute(
            select(
                AuditTrafficLog.status_code,
                func.count(AuditTrafficLog.id),
            )
            .group_by(
                AuditTrafficLog.status_code
            )
            .order_by(
                AuditTrafficLog.status_code
            )
        )

        traffic_status_summary = [
            {
                "status_code": int(status_code),
                "count": int(count),
            }
            for status_code, count
            in traffic_status_result.all()
        ]

        traffic_inspection = {
            "table_name": AuditTrafficLog.__tablename__,
            "total_rows": int(
                traffic_total or 0
            ),
            "unique_ip_addresses": int(
                traffic_unique_ips or 0
            ),
            "status_summary": traffic_status_summary,
            "columns": [
                {
                    "name": column.name,
                    "type": str(column.type),
                    "nullable": bool(column.nullable),
                    "primary_key": bool(column.primary_key),
                }
                for column in traffic_table.columns
            ],
            "indexes": [
                {
                    "name": index.name,
                    "unique": bool(index.unique),
                    "columns": [
                        column.name
                        for column in index.columns
                    ],
                }
                for index in traffic_table.indexes
            ],
        }

    return {
        "database_engine": str(
            engine.url
        ).split("://")[0],

        "generated_at": datetime.now(
            timezone.utc
        ).isoformat(),

        "table_count": len(schema_info),

        "tables": schema_info,

        "traffic_table_inspection": traffic_inspection,
    }


# ============================================================
# ADMIN MANAGEMENT
# ============================================================

@router.get("/super-admin/admins")
async def list_admins(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(verify_super_admin),
):
    """
    Fetch all users who hold admin rights.
    """

    try:

        stmt = select(User).where(
            (User.role.in_([
                "admin",
                "ADMIN",
                "SUPER_ADMIN",
                "super_admin",
            ]))
            | (
                User.email.in_(
                    list(SUPER_ADMIN_EMAILS)
                )
            )
        )

        result = await db.execute(stmt)

        admins = result.scalars().all()

    except Exception:

        stmt = select(User)

        result = await db.execute(stmt)

        admins = [
            user
            for user in result.scalars().all()
            if (
                str(
                    getattr(
                        user,
                        "role",
                        "",
                    )
                ).lower()
                in {
                    "admin",
                    "super_admin",
                }
                or str(
                    getattr(
                        user,
                        "email",
                        "",
                    )
                ).lower()
                in SUPER_ADMIN_EMAILS
            )
        ]

    return [
        {
            "id": user.id,
            "full_name": user.full_name,
            "email": user.email,
            "created_at": str(
                getattr(
                    user,
                    "created_at",
                    "",
                )
            ),
            "role": getattr(
                user,
                "role",
                None,
            ),
            "is_admin": getattr(
                user,
                "is_admin",
                False,
            ),
        }
        for user in admins
    ]


@router.post("/super-admin/admins/toggle")
async def toggle_admin_privilege(
    payload: Dict[str, Any],
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(verify_super_admin),
):
    """
    Promote or demote a user admin status by email.
    """

    target_email = str(
        payload.get("email") or ""
    ).strip().lower()

    make_admin = bool(
        payload.get(
            "make_admin",
            True,
        )
    )

    if not target_email:
        raise HTTPException(
            status_code=400,
            detail="Email address is required.",
        )

    # --------------------------------------------------------
    # Never downgrade designated Super Admin
    # --------------------------------------------------------

    if (
        target_email in SUPER_ADMIN_EMAILS
        and not make_admin
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "The designated Super Admin "
                "cannot be downgraded."
            ),
        )

    user_stmt = select(User).where(
        User.email == target_email
    )

    target_user = (
        await db.execute(user_stmt)
    ).scalar_one_or_none()

    if not target_user:
        raise HTTPException(
            status_code=404,
            detail=(
                f"User with email "
                f"'{target_email}' not found."
            ),
        )

    if hasattr(target_user, "role"):
        target_user.role = (
            "admin"
            if make_admin
            else "student"
        )

    if hasattr(target_user, "is_admin"):
        target_user.is_admin = make_admin

    await db.commit()

    return {
        "success": True,
        "message": (
            f"User {target_email} privilege "
            f"updated successfully."
        ),
    }


# ============================================================
# TEAM MANAGEMENT
# ============================================================

@router.post("/super-admin/team/toggle")
async def toggle_team_privilege(
    payload: Dict[str, Any],
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(verify_super_admin),
):
    """
    Promote or demote a user to/from Team access.

    Request:

    {
        "email": "user@example.com",
        "make_team": true
    }
    """

    target_email = str(
        payload.get("email") or ""
    ).strip().lower()

    make_team = bool(
        payload.get(
            "make_team",
            True,
        )
    )

    if not target_email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email address is required.",
        )

    target_user = (
        await db.execute(
            select(User).where(
                User.email == target_email
            )
        )
    ).scalar_one_or_none()

    if not target_user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"User with email "
                f"'{target_email}' not found."
            ),
        )

    # --------------------------------------------------------
    # Protect Super Admin
    # --------------------------------------------------------

    if target_email in SUPER_ADMIN_EMAILS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "The designated Super Admin cannot "
                "be modified through this endpoint."
            ),
        )

    if hasattr(target_user, "role"):
        target_user.role = (
            "team"
            if make_team
            else "student"
        )

    await db.commit()

    await db.refresh(target_user)

    return {
        "success": True,
        "message": (
            f"User {target_email} granted Team "
            f"access successfully."
            if make_team
            else
            f"User {target_email} Team access "
            f"revoked successfully."
        ),
        "user": {
            "id": target_user.id,
            "full_name": target_user.full_name,
            "email": target_user.email,
            "role": getattr(
                target_user,
                "role",
                None,
            ),
            "is_admin": getattr(
                target_user,
                "is_admin",
                False,
            ),
        },
    }


@router.get("/super-admin/team")
async def list_team_members(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(verify_super_admin),
):
    """
    Fetch all users who currently hold Team access.
    """

    try:

        stmt = select(User).where(
            User.role.in_([
                "team",
                "TEAM",
            ])
        )

        result = await db.execute(stmt)

        team_members = result.scalars().all()

    except Exception:

        stmt = select(User)

        result = await db.execute(stmt)

        team_members = [
            user
            for user in result.scalars().all()
            if str(
                getattr(
                    user,
                    "role",
                    "",
                )
            ).lower()
            == "team"
        ]

    return [
        {
            "id": member.id,
            "full_name": member.full_name,
            "email": member.email,
            "created_at": str(
                getattr(
                    member,
                    "created_at",
                    "",
                )
            ),
        }
        for member in team_members
    ]