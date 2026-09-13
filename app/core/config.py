import os
from typing import Optional, List
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    APP_NAME: str = "Accqudo Assessment Platform"
    PROJECT_NAME: str = "Accqudo Assessment Platform"

    ENVIRONMENT: str = "development"
    DEBUG: bool = True

    API_V1_STR: str = "/api/v1"
    API_V1_PREFIX: str = "/api/v1"

    SECRET_KEY: str = "accqudo_super_secret_production_jwt_key_change_in_prod"
    JWT_SECRET: str = "accqudo_super_secret_production_jwt_key_change_in_prod"
    ALGORITHM: str = "HS256"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 10080

    DATABASE_URL: str = "mysql+aiomysql://accqudo_user:accqudo_password@accqudo_mysql:3306/accqudo_db"
    DATABASE_SSL_CA: Optional[str] = None
    
    REDIS_URL: str = "redis://:accqudo_redis_secret@accqudo_redis:6379/0"

    # Razorpay Payment Gateway (Add your test/live keys in .env)
    RAZORPAY_KEY_ID: str = os.getenv("RAZORPAY_KEY_ID", "rzp_test_dummy_key_id")
    RAZORPAY_KEY_SECRET: str = os.getenv("RAZORPAY_KEY_SECRET", "dummy_secret_key")
    RAZORPAY_WEBHOOK_SECRET: Optional[str] = os.getenv("RAZORPAY_WEBHOOK_SECRET", None)

    # SMTP Settings
    SMTP_HOST: str = "smtp.gmail.com"
    SMTP_PORT: int = 587
    SMTP_USER: str = "notifications@accqudo.internal"
    SMTP_PASSWORD: str = ""
    SMTP_FROM_EMAIL: str = "notifications@accqudo.internal"
    SMTP_FROM_NAME: str = "Accqudo Security"
    SMTP_TLS: bool = True
    MAIL_SUPPRESS_SEND: bool = True

    FIREBASE_CREDENTIALS_PATH: Optional[str] = None
    
    # Cloudflare R2
    R2_ACCOUNT_ID: str = ""
    R2_ACCESS_KEY_ID: str = ""
    R2_SECRET_ACCESS_KEY: str = ""
    R2_BUCKET_NAME: str = ""
    R2_PUBLIC_DOMAIN: str = ""


settings = Settings()