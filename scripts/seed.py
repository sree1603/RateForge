"""Create the example plans and a demo upstream/route. Safe to run repeatedly.

docker compose run --rm api python scripts/seed.py
"""

import asyncio

from app.db.session import SessionLocal, engine
from app.repositories import routing as routing_repo
from app.repositories import tenants as tenant_repo
from app.schemas.routing import RouteCreate, UpstreamCreate
from app.schemas.tenants import PlanCreate
from app.services import policy, routing

EXAMPLE_PLANS = [
    PlanCreate(name="Basic", requests_per_window=60, window_seconds=60),
    PlanCreate(name="Pro", requests_per_window=300, window_seconds=60),
    PlanCreate(name="Enterprise", requests_per_window=1000, window_seconds=60),
]


ECHO_UPSTREAM = UpstreamCreate(name="echo", base_url="http://echo:9000")


async def seed_echo_route(session) -> None:
    upstream = await routing_repo.get_upstream_by_name(session, ECHO_UPSTREAM.name)
    if upstream is None:
        upstream = await routing.create_upstream(session, ECHO_UPSTREAM)
        print("created  upstream echo")
    else:
        print("exists   upstream echo")
    if await routing_repo.get_route_by_prefix(session, "echo"):
        print("exists   route /gateway/echo")
        return
    await routing.create_route(
        session,
        RouteCreate(
            upstream_id=upstream.id,
            path_prefix="echo",
            allowed_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        ),
    )
    print("created  route /gateway/echo")


async def main() -> None:
    async with SessionLocal() as session:
        for plan in EXAMPLE_PLANS:
            if await tenant_repo.get_plan_by_name(session, plan.name):
                print(f"exists   {plan.name}")
                continue
            await policy.create_plan(session, plan)
            print(f"created  {plan.name}")
        await seed_echo_route(session)
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
