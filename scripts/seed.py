"""Create the example plans (Basic, Pro, Enterprise). Safe to run repeatedly.

docker compose run --rm api python scripts/seed.py
"""

import asyncio

from app.db.session import SessionLocal, engine
from app.repositories import tenants as tenant_repo
from app.schemas.tenants import PlanCreate
from app.services import policy

EXAMPLE_PLANS = [
    PlanCreate(name="Basic", requests_per_window=60, window_seconds=60),
    PlanCreate(name="Pro", requests_per_window=300, window_seconds=60),
    PlanCreate(name="Enterprise", requests_per_window=1000, window_seconds=60),
]


async def main() -> None:
    async with SessionLocal() as session:
        for plan in EXAMPLE_PLANS:
            if await tenant_repo.get_plan_by_name(session, plan.name):
                print(f"exists   {plan.name}")
                continue
            await policy.create_plan(session, plan)
            print(f"created  {plan.name}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
