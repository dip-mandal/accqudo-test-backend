from fastapi import APIRouter
from app.api.v1.endpoints import (
    attempts,
    auth,
    payments,
    leaderboards,
    analytics,
    admin,
    storage,
    super_admin,
    team,
)

api_router = APIRouter()

# Explicit RESTful prefixes ensuring stable routes across all micro-services
api_router.include_router(auth.router, prefix="/auth", tags=["Authentication"])
api_router.include_router(attempts.router, prefix="/attempts", tags=["Attempts"])
api_router.include_router(payments.router, prefix="/payments", tags=["Payments"])
api_router.include_router(leaderboards.router, prefix="/leaderboards", tags=["Leaderboards"])

api_router.include_router(analytics.router, prefix="/analytics", tags=["Analytics"])


api_router.include_router(admin.router, prefix="/admin", tags=["Admin Studio & CMS"])


api_router.include_router(
    storage.router,
    prefix="/storage",
    tags=["Storage"],
)

# Team router provides prefix="/admin" internally, so include without duplicate prefix
api_router.include_router(team.router, tags=["Team & Authoring"])

api_router.include_router(super_admin.router, tags=["Super Admin"])


