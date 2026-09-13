from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

import jwt
from jwt.exceptions import PyJWTError

from app.core.config import settings
from app.core.database import get_db
from app.models.user import User


# ============================================================
# OAuth2 Bearer Token
# ============================================================

oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl=f"{getattr(settings, 'API_V1_PREFIX', '/api/v1')}/auth/login"
)


# ============================================================
# JWT CONFIG
# ============================================================

JWT_SECRET = str(
    getattr(settings, "JWT_SECRET", None)
    or getattr(settings, "SECRET_KEY", "")
)

JWT_ALGORITHM = str(
    getattr(settings, "JWT_ALGORITHM", None)
    or getattr(settings, "ALGORITHM", "HS256")
)


# ============================================================
# JWT DECODER
# ============================================================

def decode_access_token(token: str) -> dict:
    """
    Decode and validate an Accqudo access JWT.

    The login endpoint creates tokens using:
        JWT_SECRET
        JWT_ALGORITHM

    Therefore authentication must use the exact same values.
    """

    if not token:
        raise ValueError("Missing access token")

    try:
        payload = jwt.decode(
            token,
            JWT_SECRET,
            algorithms=[JWT_ALGORITHM],
            options={
                "verify_signature": True,
                "verify_exp": True,
            },
        )

        if not isinstance(payload, dict):
            raise ValueError("Invalid JWT payload")

        return payload

    except PyJWTError as exc:
        # Do not expose JWT internals to the client.
        raise ValueError("Invalid or expired access token") from exc


# ============================================================
# CURRENT USER
# ============================================================

async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """
    Authenticate the current user using the Bearer JWT.

    Expected JWT payload:

        {
            "sub": "8",
            "role": "TEAM",
            "exp": 1234567890
        }
    """

    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials. Please log in again.",
        headers={
            "WWW-Authenticate": "Bearer",
        },
    )

    # --------------------------------------------------------
    # 1. Make sure a token exists
    # --------------------------------------------------------

    if not token:
        raise credentials_exception

    # --------------------------------------------------------
    # 2. Decode JWT
    # --------------------------------------------------------

    try:
        payload = decode_access_token(token)
    except (ValueError, TypeError):
        raise credentials_exception

    # --------------------------------------------------------
    # 3. Extract user ID
    # --------------------------------------------------------

    user_id_raw = payload.get("sub")

    # Backward compatibility in case an older token uses user_id.
    if user_id_raw is None:
        user_id_raw = payload.get("user_id")

    if user_id_raw is None:
        raise credentials_exception

    try:
        user_id = int(user_id_raw)
    except (ValueError, TypeError):
        raise credentials_exception

    # --------------------------------------------------------
    # 4. Find user in database
    # --------------------------------------------------------

    stmt = select(User).where(User.id == user_id)

    result = await db.execute(stmt)

    user = result.scalar_one_or_none()

    if user is None:
        raise credentials_exception

    # --------------------------------------------------------
    # 5. Check account status
    # --------------------------------------------------------

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is deactivated.",
        )

    # --------------------------------------------------------
    # 6. Authentication successful
    # --------------------------------------------------------

    return user


# ============================================================
# TEAM / ADMIN / SUPER ADMIN
# ============================================================

async def require_team_or_admin(
    current_user: User = Depends(get_current_user),
) -> User:
    """
    Allows:

        TEAM
        ADMIN
        SUPER_ADMIN
    """

    user_role = str(
        getattr(current_user, "role", "")
    ).upper().strip()

    allowed_roles = {
        "TEAM",
        "ADMIN",
        "SUPER_ADMIN",
    }

    if user_role not in allowed_roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Forbidden: Staff privileges "
                "('TEAM', 'ADMIN', or 'SUPER_ADMIN') required."
            ),
        )

    return current_user


# ============================================================
# ADMIN ONLY
# ============================================================

async def require_admin_only(
    current_user: User = Depends(get_current_user),
) -> User:
    """
    Allows only:

        ADMIN
        SUPER_ADMIN
    """

    user_role = str(
        getattr(current_user, "role", "")
    ).upper().strip()

    allowed_admin_roles = {
        "ADMIN",
        "SUPER_ADMIN",
    }

    if user_role not in allowed_admin_roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Forbidden: Administrator privileges required. "
                "Team members cannot access this panel."
            ),
        )

    return current_user