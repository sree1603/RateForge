"""Create the example plans and a demo upstream/route. Safe to run repeatedly.

docker compose run --rm api python scripts/seed.py
"""

import argparse
import asyncio

from app.db.session import SessionLocal, engine
from app.repositories import routing as routing_repo
from app.repositories import tenants as tenant_repo
from app.schemas.routing import RouteCreate, UpstreamCreate
from app.schemas.tenants import PlanCreate
from app.services import keys as key_service
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


async def create_demo_key(session) -> None:
    """Ensure a 'demo' tenant on the Basic plan exists and print a fresh API key for it."""
    tenant = await tenant_repo.get_tenant_by_name(session, "demo")
    if tenant is None:
        tenant = await policy.create_tenant(session, "demo")
        print("created  tenant demo")
    basic = await tenant_repo.get_plan_by_name(session, "Basic")
    await policy.assign_plan(session, tenant.id, basic.id)
    _, raw = await key_service.create_key(
        session, tenant_id=tenant.id, name="demo", expires_in_days=None
    )
    print("\nAPI key for tenant 'demo' (Basic plan). It is shown only once:\n")
    print(f"  {raw}\n")


async def main(demo_key: bool = False) -> None:
    async with SessionLocal() as session:
        for plan in EXAMPLE_PLANS:
            if await tenant_repo.get_plan_by_name(session, plan.name):
                print(f"exists   {plan.name}")
                continue
            await policy.create_plan(session, plan)
            print(f"created  {plan.name}")
        await seed_echo_route(session)
        if demo_key:
            await create_demo_key(session)
    await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--demo-key",
        action="store_true",
        help="also create a tenant 'demo' on the Basic plan and print a new API key for it",
    )
    asyncio.run(main(parser.parse_args().demo_key))
