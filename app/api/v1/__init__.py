from fastapi import APIRouter

from app.api.v1 import keys, plans, tenants

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(tenants.router)
api_router.include_router(plans.router)
api_router.include_router(keys.router)
