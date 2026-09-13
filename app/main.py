import os
import sys
import time
import platform
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Depends, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# 1. Import models once so Base metadata registers without conflicts
from app.core.database import engine, Base, get_db
import app.models.user
import app.models.exam
import app.models.question
import app.models.test
import app.models.attempt
import app.models.enrollment
import app.models.subscription  # Canonical packages & subscriptions
from app.models.audit import AuditTrafficLog

from app.core.config import settings
from app.core.redis import get_redis
from app.api.v1.api import api_router

# Track server boot time for uptime telemetry
SERVER_START_TIME = time.time()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle events: initialize database tables and warm up cache connections."""
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    except Exception as exc:
        print(f"[FATAL STARTUP DB ERROR] Could not verify/create tables: {exc}")
    yield


app = FastAPI(
    title=settings.APP_NAME,
    openapi_url=f"{settings.API_V1_PREFIX}/openapi.json",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)


# --- 1. Real Traffic Audit Middleware ---
# Registered first so CORSMiddleware sits outermost in the execution stack.
@app.middleware("http")
async def log_real_traffic_middleware(request: Request, call_next):
    start_time = time.time()
    response: Response = await call_next(request)
    latency_ms = round((time.time() - start_time) * 1000, 2)

    # Exclude routine ping checks to keep audit telemetry meaningful
    path = request.url.path
    if path not in ["/health", "/system/info", "/docs", "/openapi.json", "/redoc"]:
        try:
            # Resolve actual client IP behind Cloudflare Tunnel / reverse proxies
            client_ip = (
                request.headers.get("cf-connecting-ip")
                or request.headers.get("x-forwarded-for")
                or (request.client.host if request.client else "127.0.0.1")
            )
            client_ip = client_ip.split(",")[0].strip()
            user_agent = request.headers.get("user-agent", "")[:500]

            async with engine.begin() as conn:
                await conn.execute(
                    AuditTrafficLog.__table__.insert().values(
                        path=path[:255],
                        method=request.method,
                        status_code=response.status_code,
                        ip_address=client_ip,
                        user_agent=user_agent,
                        response_time_ms=latency_ms,
                    )
                )
        except Exception:
            # Never let audit telemetry break client response delivery
            pass

    return response


# --- 2. Production & Development CORS Configuration ---
# Must be added AFTER log_real_traffic_middleware so preflights pass through cleanly.
origins = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
    "https://accqudo.com",
    "https://www.accqudo.com",
    "https://api.accqudo.com",
]

if hasattr(settings, "CORS_ORIGINS") and settings.CORS_ORIGINS:
    for o in settings.CORS_ORIGINS:
        val = str(o).strip().rstrip("/")
        if val and val not in origins:
            origins.append(val)

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_origin_regex=r"^https?://([a-zA-Z0-9-]+\.)*accqudo\.com$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition", "Content-Length", "X-Total-Count"],
)

# Register Versioned API Routes
app.include_router(api_router, prefix=settings.API_V1_PREFIX)


# --- 3. Root & Diagnostic Telemetry Endpoints ---

@app.get("/", tags=["System & Health"])
async def root_status():
    """Service landing status."""
    return {
        "service": settings.APP_NAME,
        "status": "online",
        "api_v1": f"{settings.API_V1_PREFIX}",
        "docs": "/docs",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/health", tags=["System & Health"])
async def health_check(db: AsyncSession = Depends(get_db)):
    """Deep health check verifying MySQL database and Redis/Valkey connections."""
    db_status = "unhealthy"
    redis_status = "unhealthy"
    redis_ping_ms = 0.0

    # 1. Check MySQL
    try:
        await db.execute(text("SELECT 1"))
        db_status = "connected"
    except Exception as e:
        db_status = f"error: {str(e)}"

    # 2. Check Redis / Valkey
    try:
        t0 = time.time()
        r = get_redis()
        pong = await r.ping()
        if pong:
            redis_status = "connected"
            redis_ping_ms = round((time.time() - t0) * 1000, 2)
    except Exception as e:
        redis_status = f"error: {str(e)}"

    uptime_seconds = int(time.time() - SERVER_START_TIME)
    is_healthy = (db_status == "connected") and (redis_status == "connected")

    payload = {
        "status": "healthy" if is_healthy else "degraded",
        "environment": getattr(settings, "ENVIRONMENT", "development"),
        "uptime_seconds": uptime_seconds,
        "dependencies": {
            "mysql": {"status": db_status},
            "redis": {"status": redis_status, "latency_ms": redis_ping_ms},
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    return JSONResponse(
        status_code=status.HTTP_200_OK if is_healthy else status.HTTP_503_SERVICE_UNAVAILABLE,
        content=payload,
    )


@app.get("/system/info", tags=["System & Health"])
async def system_information():
    """Environment, hardware runtime, and router topology metadata."""
    routes = []
    for route in app.routes:
        if hasattr(route, "path") and hasattr(route, "methods"):
            routes.append({"path": route.path, "methods": list(route.methods)})

    return {
        "app_name": settings.APP_NAME,
        "environment": getattr(settings, "ENVIRONMENT", "development"),
        "python_version": sys.version.split(" ")[0],
        "platform": platform.platform(),
        "pid": os.getpid(),
        "allocated_routes_count": len(routes),
        "server_time_utc": datetime.now(timezone.utc).isoformat(),
        "routes": routes,
    }