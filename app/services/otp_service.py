import secrets
import json
import redis.asyncio as aioredis
from app.core.config import settings

class OTPService:
    @staticmethod
    async def _get_redis() -> aioredis.Redis:
        return aioredis.from_url(settings.REDIS_URL, decode_responses=True)

    @classmethod
    def generate_otp(cls) -> str:
        # Generates a secure 6-digit numeric OTP
        return f"{secrets.randbelow(900000) + 100000}"

    @classmethod
    async def store_registration_draft(cls, email: str, user_dict: dict, otp: str, ttl_seconds: int = 300):
        r = await cls._get_redis()
        cache_key = f"otp:reg:{email.lower().strip()}"
        data = {
            "otp": otp,
            "user_data": user_dict,
            "attempts": 0
        }
        await r.setex(cache_key, ttl_seconds, json.dumps(data))
        await r.close()

    @classmethod
    async def verify_registration_otp(cls, email: str, submitted_otp: str) -> dict | None:
        r = await cls._get_redis()
        cache_key = f"otp:reg:{email.lower().strip()}"
        raw = await r.get(cache_key)
        if not raw:
            await r.close()
            return None

        data = json.loads(raw)
        if data["attempts"] >= 4:
            await r.delete(cache_key)
            await r.close()
            return None

        if data["otp"] != submitted_otp.strip():
            data["attempts"] += 1
            ttl = await r.ttl(cache_key)
            if ttl > 0:
                await r.setex(cache_key, ttl, json.dumps(data))
            await r.close()
            return None

        # Success - clean up
        await r.delete(cache_key)
        await r.close()
        return data["user_data"]

    @classmethod
    async def store_password_reset_otp(cls, email: str, otp: str, ttl_seconds: int = 300):
        r = await cls._get_redis()
        cache_key = f"otp:pwd_reset:{email.lower().strip()}"
        data = {"otp": otp, "attempts": 0}
        await r.setex(cache_key, ttl_seconds, json.dumps(data))
        await r.close()

    @classmethod
    async def verify_password_reset_otp(cls, email: str, submitted_otp: str) -> bool:
        r = await cls._get_redis()
        cache_key = f"otp:pwd_reset:{email.lower().strip()}"
        raw = await r.get(cache_key)
        if not raw:
            await r.close()
            return False

        data = json.loads(raw)
        if data["attempts"] >= 4:
            await r.delete(cache_key)
            await r.close()
            return False

        if data["otp"] != submitted_otp.strip():
            data["attempts"] += 1
            ttl = await r.ttl(cache_key)
            if ttl > 0:
                await r.setex(cache_key, ttl, json.dumps(data))
            await r.close()
            return False

        # Store a short-lived reset grant token (valid for 5 min)
        grant_key = f"otp:pwd_grant:{email.lower().strip()}"
        await r.setex(grant_key, 300, "granted")
        await r.delete(cache_key)
        await r.close()
        return True

    @classmethod
    async def consume_reset_grant(cls, email: str) -> bool:
        r = await cls._get_redis()
        grant_key = f"otp:pwd_grant:{email.lower().strip()}"
        valid = await r.get(grant_key)
        if valid:
            await r.delete(grant_key)
            await r.close()
            return True
        await r.close()
        return False
    
    
    
    @classmethod
    async def resend_registration_otp(cls, email: str) -> tuple[str | None, str | None]:
        """Returns (new_otp, user_name) or (None, None) if no draft exists."""
        r = await cls._get_redis()
        cache_key = f"otp:reg:{email.lower().strip()}"
        raw = await r.get(cache_key)
        if not raw:
            await r.close()
            return None, None

        data = json.loads(raw)
        new_otp = cls.generate_otp()
        data["otp"] = new_otp
        data["attempts"] = 0
        await r.setex(cache_key, 300, json.dumps(data))
        await r.close()
        return new_otp, data["user_data"].get("full_name", "Student")

    @classmethod
    async def resend_password_reset_otp(cls, email: str) -> str | None:
        """Refreshes the password reset OTP in Redis."""
        r = await cls._get_redis()
        cache_key = f"otp:pwd_reset:{email.lower().strip()}"
        exists = await r.exists(cache_key)
        if not exists:
            await r.close()
            return None

        new_otp = cls.generate_otp()
        data = {"otp": new_otp, "attempts": 0}
        await r.setex(cache_key, 300, json.dumps(data))
        await r.close()
        return new_otp