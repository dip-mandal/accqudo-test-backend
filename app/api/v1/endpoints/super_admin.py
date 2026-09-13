from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, text, func, desc
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List, Dict, Any
from datetime import datetime, timezone, timedelta

from app.core.database import get_db, Base, engine
from app.core.security import get_current_user
from app.models.user import User
from app.models.audit import AuditTrafficLog

router = APIRouter(tags=["Super Admin Studio"])

async def verify_super_admin(current_user: User = Depends(get_current_user)) -> User:
    """Strictly restrict access to designated super admin and authorized admin roles."""
    super_admin_emails = ["dm9475511@gmail.com"]
    user_role = getattr(current_user, "role", None)
    is_admin_priv = getattr(current_user, "is_admin", False) or user_role in ["SUPER_ADMIN", "super_admin"]

    if current_user.email not in super_admin_emails and not is_admin_priv:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied. Super Admin privileges required."
        )
    return current_user

@router.get("/super-admin/overview")
async def get_system_overview(db: AsyncSession = Depends(get_db), admin: User = Depends(verify_super_admin)):
    """High-level system vitals and counts."""
    users_count = await db.scalar(select(func.count(User.id)))
    traffic_count = await db.scalar(select(func.count(AuditTrafficLog.id)))
    
    # Active traffic last 24 hours using created_at column mapping
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    recent_traffic = await db.scalar(
        select(func.count(AuditTrafficLog.id)).where(AuditTrafficLog.created_at >= yesterday)
    )

    return {
        "total_users": users_count or 0,
        "total_audit_logs": traffic_count or 0,
        "traffic_last_24h": recent_traffic or 0,
        "database_engine": str(engine.url).split("://")[0],
    }

@router.get("/super-admin/traffic")
async def get_traffic_analytics(db: AsyncSession = Depends(get_db), admin: User = Depends(verify_super_admin)):
    """Detailed traffic logs with IP, User-Agent, Latency, and timeline aggregations."""
    stmt = select(AuditTrafficLog).order_by(desc(AuditTrafficLog.id)).limit(100)
    result = await db.execute(stmt)
    logs = result.scalars().all()

    total_requests = await db.scalar(select(func.count(AuditTrafficLog.id)))
    unique_ips = await db.scalar(select(func.count(func.distinct(AuditTrafficLog.ip_address))))
    
    formatted_logs = [{
        "id": l.id,
        "path": l.path,
        "method": l.method,
        "status_code": l.status_code,
        "ip_address": l.ip_address,
        "user_agent": l.user_agent,
        "response_time_ms": l.response_time_ms,
        "timestamp": l.created_at.isoformat() if getattr(l, "created_at", None) else None
    } for l in logs]

    return {
        "total_requests": total_requests or 0,
        "unique_visitors": unique_ips or 0,
        "logs": formatted_logs
    }

@router.get("/super-admin/database-schema")
async def inspect_database_schema(db: AsyncSession = Depends(get_db), admin: User = Depends(verify_super_admin)):
    """Inspect all tables, columns, and structural relations dynamically from SQLAlchemy Base metadata."""
    schema_info = []
    for table_name, table in Base.metadata.tables.items():
        columns = [{
            "name": col.name,
            "type": str(col.type),
            "nullable": col.nullable,
            "primary_key": col.primary_key,
            "foreign_keys": [str(fk.target_fullname) for fk in col.foreign_keys]
        } for col in table.columns]
        
        schema_info.append({
            "table_name": table_name,
            "columns_count": len(columns),
            "columns": columns
        })
    return {"tables": schema_info}

@router.get("/super-admin/admins")
async def list_admins(db: AsyncSession = Depends(get_db), admin: User = Depends(verify_super_admin)):
    """Fetch all users who hold admin rights."""
    try:
        stmt = select(User).where((User.role == "admin") | (User.email == "dm9475511@gmail.com"))
        result = await db.execute(stmt)
        admins = result.scalars().all()
    except Exception:
        stmt = select(User)
        result = await db.execute(stmt)
        admins = [u for u in result.scalars().all() if getattr(u, "role", "") == "admin" or u.email == "dm9475511@gmail.com"]

    return [{"id": a.id, "full_name": a.full_name, "email": a.email, "created_at": str(getattr(a, "created_at", ""))} for a in admins]

@router.post("/super-admin/admins/toggle")
async def toggle_admin_privilege(payload: Dict[str, Any], db: AsyncSession = Depends(get_db), admin: User = Depends(verify_super_admin)):
    """Promote or demote user admin status by email address."""
    target_email = payload.get("email")
    make_admin = payload.get("make_admin", True)

    user_stmt = select(User).where(User.email == target_email)
    target_user = (await db.execute(user_stmt)).scalar_one_or_none()

    if not target_user:
        raise HTTPException(status_code=404, detail=f"User with email '{target_email}' not found.")

    if hasattr(target_user, "role"):
        target_user.role = "admin" if make_admin else "student"
    if hasattr(target_user, "is_admin"):
        target_user.is_admin = make_admin
        
    await db.commit()

    return {"success": True, "message": f"User {target_email} privilege updated successfully."}







@router.post("/super-admin/team/toggle")
async def toggle_team_privilege(
    payload: Dict[str, Any],
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(verify_super_admin),
):
    """
    Promote or demote a user to/from Team access by email address.

    Request:
    {
        "email": "user@example.com",
        "make_team": true
    }

    make_team=true  -> Team access
    make_team=false -> Student access
    """
    target_email = payload.get("email")
    make_team = payload.get("make_team", True)

    if not target_email or not str(target_email).strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email address is required.",
        )

    target_email = str(target_email).strip().lower()

    user_stmt = select(User).where(User.email == target_email)
    target_user = (
        await db.execute(user_stmt)
    ).scalar_one_or_none()

    if not target_user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User with email '{target_email}' not found.",
        )

    # Never allow the designated Super Admin to be accidentally
    # downgraded through the Team privilege endpoint.
    super_admin_emails = ["dm9475511@gmail.com"]

    if target_user.email in super_admin_emails:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The designated Super Admin cannot be modified through this endpoint.",
        )

    # Team users should have TEAM role.
    # When removing Team access, return them to STUDENT.
    if hasattr(target_user, "role"):
        target_user.role = "team" if make_team else "student"

    # Team privilege does NOT grant administrator privileges.
    # Keep is_admin unchanged if the user already has admin rights.

    await db.commit()
    await db.refresh(target_user)

    return {
        "success": True,
        "message": (
            f"User {target_email} granted Team access successfully."
            if make_team
            else f"User {target_email} Team access revoked successfully."
        ),
        "user": {
            "id": target_user.id,
            "full_name": target_user.full_name,
            "email": target_user.email,
            "role": getattr(target_user, "role", None),
            "is_admin": getattr(target_user, "is_admin", False),
        },
    }
    
    
    



@router.get("/super-admin/team")
async def list_team_members(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(verify_super_admin),
):
    """Fetch all users who currently hold Team access."""
    try:
        stmt = select(User).where(
            User.role.in_(["team", "TEAM"])
        )

        result = await db.execute(stmt)
        team_members = result.scalars().all()

    except Exception:
        # Fallback for databases/models where role filtering
        # behaves differently.
        stmt = select(User)
        result = await db.execute(stmt)

        team_members = [
            u
            for u in result.scalars().all()
            if str(getattr(u, "role", "")).lower() == "team"
        ]

    return [
        {
            "id": member.id,
            "full_name": member.full_name,
            "email": member.email,
            "created_at": str(
                getattr(member, "created_at", "")
            ),
        }
        for member in team_members
    ]