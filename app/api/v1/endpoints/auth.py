from datetime import datetime, timedelta
from typing import Optional
import bcrypt
import jwt
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
import firebase_admin
from firebase_admin import auth as fb_auth, credentials

from app.core.config import settings
from app.core.database import get_db
from app.models.user import User
from app.services.otp_service import OTPService
from app.services.email_service import EmailService

router = APIRouter()


# --- Native Bcrypt Helpers (Avoids passlib compatibility issues) ---
def hash_password(password: str) -> str:
    pwd_bytes = password.encode("utf-8")[:72]
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(pwd_bytes, salt).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        pwd_bytes = plain_password.encode("utf-8")[:72]
        return bcrypt.checkpw(pwd_bytes, hashed_password.encode("utf-8"))
    except Exception:
        return False


# --- Firebase Admin Lazy Initialization ---
_firebase_initialized = False

def ensure_firebase():
    global _firebase_initialized
    if not _firebase_initialized:
        try:
            if getattr(settings, "FIREBASE_CREDENTIALS_PATH", None):
                cred = credentials.Certificate(settings.FIREBASE_CREDENTIALS_PATH)
                firebase_admin.initialize_app(cred)
            else:
                firebase_admin.initialize_app()
            _firebase_initialized = True
        except Exception:
            _firebase_initialized = True


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + (
        expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    to_encode.update({"exp": expire})
    secret = str(getattr(settings, "JWT_SECRET", settings.SECRET_KEY))
    algorithm = str(getattr(settings, "JWT_ALGORITHM", settings.ALGORITHM))
    return jwt.encode(to_encode, secret, algorithm=algorithm)


# --- Request/Response Schemas ---
class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RegisterSendOtpRequest(BaseModel):
    email: EmailStr
    password: str
    full_name: str


class VerifyOtpRegisterRequest(BaseModel):
    email: EmailStr
    otp: str


class ResendOtpRequest(BaseModel):
    email: EmailStr
    purpose: str = "registration"  # "registration" or "reset"


class ForgotPasswordSendOtpRequest(BaseModel):
    email: EmailStr


class VerifyForgotPasswordOtpRequest(BaseModel):
    email: EmailStr
    otp: str


class ResetPasswordFinalRequest(BaseModel):
    email: EmailStr
    new_password: str


class GoogleLoginRequest(BaseModel):
    id_token: str


# --- Endpoints (Mounted under prefix="/auth") ---

@router.post("/login")
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)):
    """Authenticate existing user with email and password."""
    stmt = select(User).where(User.email == payload.email.lower().strip())
    user = (await db.execute(stmt)).scalar_one_or_none()

    if not user or not user.hashed_password or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password."
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is currently inactive. Contact support."
        )

    user_role = user.role.value if hasattr(user.role, "value") else str(user.role)
    token = create_access_token({"sub": str(user.id), "role": user_role})

    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "id": user.id,
            "email": user.email,
            "full_name": user.full_name,
            "role": user_role,
        },
    }


@router.post("/register/send-otp")
async def register_send_otp(payload: RegisterSendOtpRequest, db: AsyncSession = Depends(get_db)):
    """Initiate registration: store temporary profile in Redis and send 6-digit OTP."""
    stmt = select(User).where(User.email == payload.email.lower().strip())
    existing = (await db.execute(stmt)).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="An account with this email already exists."
        )

    otp = OTPService.generate_otp()
    user_dict = {
        "email": payload.email.lower().strip(),
        "hashed_password": hash_password(payload.password),
        "full_name": payload.full_name.strip(),
        "role": "STUDENT",
    }

    await OTPService.store_registration_draft(payload.email, user_dict, otp, ttl_seconds=300)
    await EmailService.send_otp_email(payload.email, payload.full_name, otp, purpose="registration")

    return {"status": "success", "message": "Verification code sent to your email."}


@router.post("/register/verify-otp")
async def register_verify_otp(payload: VerifyOtpRegisterRequest, db: AsyncSession = Depends(get_db)):
    """Validate OTP from Redis, commit user to MySQL, and issue JWT."""
    user_data = await OTPService.verify_registration_otp(payload.email, payload.otp)
    if not user_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired verification code."
        )

    stmt = select(User).where(User.email == user_data["email"])
    if (await db.execute(stmt)).scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Account already registered."
        )

    new_user = User(
        email=user_data["email"],
        hashed_password=user_data["hashed_password"],
        full_name=user_data["full_name"],
        role=user_data["role"],
        is_active=True,
    )
    db.add(new_user)
    await db.commit()
    await db.refresh(new_user)

    user_role = new_user.role.value if hasattr(new_user.role, "value") else str(new_user.role)
    token = create_access_token({"sub": str(new_user.id), "role": user_role})

    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "id": new_user.id,
            "email": new_user.email,
            "full_name": new_user.full_name,
            "role": user_role,
        },
    }


@router.post("/resend-otp")
async def resend_otp(payload: ResendOtpRequest, db: AsyncSession = Depends(get_db)):
    """Resend a fresh OTP for a pending registration or password reset."""
    if payload.purpose == "registration":
        new_otp, user_name = await OTPService.resend_registration_otp(payload.email)
        if not new_otp:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No pending registration found for this email. Please submit the form again."
            )
        await EmailService.send_otp_email(payload.email, user_name or "Student", new_otp, purpose="registration")
        return {"status": "success", "message": "A new verification code has been sent to your email."}

    elif payload.purpose == "reset":
        stmt = select(User).where(User.email == payload.email.lower().strip())
        user = (await db.execute(stmt)).scalar_one_or_none()
        if user:
            new_otp = await OTPService.resend_password_reset_otp(payload.email)
            if not new_otp:
                new_otp = OTPService.generate_otp()
                await OTPService.store_password_reset_otp(payload.email, new_otp, ttl_seconds=300)
            await EmailService.send_otp_email(user.email, user.full_name or "Student", new_otp, purpose="reset")
        return {"status": "success", "message": "If this account exists, a new code has been sent."}

    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid purpose specified.")


@router.post("/forgot-password/send-otp")
async def forgot_password_send_otp(payload: ForgotPasswordSendOtpRequest, db: AsyncSession = Depends(get_db)):
    """Dispatch password reset OTP if email exists."""
    stmt = select(User).where(User.email == payload.email.lower().strip())
    user = (await db.execute(stmt)).scalar_one_or_none()

    if not user:
        return {"status": "success", "message": "If an account exists with this email, a reset code was sent."}

    otp = OTPService.generate_otp()
    await OTPService.store_password_reset_otp(user.email, otp, ttl_seconds=300)
    await EmailService.send_otp_email(user.email, user.full_name or "Student", otp, purpose="reset")

    return {"status": "success", "message": "If an account exists with this email, a reset code was sent."}


@router.post("/forgot-password/verify-otp")
async def forgot_password_verify_otp(payload: VerifyForgotPasswordOtpRequest):
    """Verify reset OTP and issue a short-lived reset grant in Redis."""
    is_valid = await OTPService.verify_password_reset_otp(payload.email, payload.otp)
    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired verification code."
        )
    return {"status": "success", "message": "Code verified. You may now enter your new password."}


@router.post("/reset-password")
async def reset_password(payload: ResetPasswordFinalRequest, db: AsyncSession = Depends(get_db)):
    """Consume the verified reset grant and update hashed password in MySQL."""
    is_granted = await OTPService.consume_reset_grant(payload.email)
    if not is_granted:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Reset permission expired or invalid. Please request a new OTP."
        )

    stmt = select(User).where(User.email == payload.email.lower().strip())
    user = (await db.execute(stmt)).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")

    user.hashed_password = hash_password(payload.new_password)
    await db.commit()

    return {"status": "success", "message": "Password reset successfully. You can now log in."}


@router.post("/google-login")
async def google_login(payload: GoogleLoginRequest, db: AsyncSession = Depends(get_db)):
    """Authenticate or auto-provision account using Firebase Google ID Token."""
    ensure_firebase()
    try:
        decoded = fb_auth.verify_id_token(payload.id_token)
        email = decoded.get("email")
        name = decoded.get("name", "Google Student")
        if not email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No email associated with this Google token."
            )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Firebase verification failed: {str(e)}"
        )

    stmt = select(User).where(User.email == email.lower().strip())
    user = (await db.execute(stmt)).scalar_one_or_none()

    if not user:
        user = User(
            email=email.lower().strip(),
            hashed_password=None,
            full_name=name,
            role="STUDENT",
            is_active=True,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)

    user_role = user.role.value if hasattr(user.role, "value") else str(user.role)
    token = create_access_token({"sub": str(user.id), "role": user_role})

    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "id": user.id,
            "email": user.email,
            "full_name": user.full_name,
            "role": user_role,
        },
    }