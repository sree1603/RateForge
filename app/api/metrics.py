from fastapi import APIRouter, HTTPException, Request, Response

from app.core.config import get_settings
from app.core.security import verify_admin_token
from app.observability.metrics import render

router = APIRouter()


def _presented_token(request: Request) -> str | None:
    """Accept the admin token as X-Admin-Token or as `Authorization: Bearer <token>`,
    because that is what a Prometheus scrape config can send."""
    token = request.headers.get("x-admin-token")
    if token:
        return token
    scheme, _, credentials = request.headers.get("authorization", "").partition(" ")
    return credentials if scheme.lower() == "bearer" and credentials else None


@router.get("/metrics", include_in_schema=False)
async def metrics(request: Request) -> Response:
    if not get_settings().metrics_public:
        token = _presented_token(request)
        if token is None or not verify_admin_token(token):
            raise HTTPException(401, "Invalid or missing credentials")
    body, content_type = render()
    return Response(content=body, headers={"Content-Type": content_type})
