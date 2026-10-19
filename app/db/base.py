from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base for all ORM models. Import models in app/models/__init__.py
    so Alembic autogenerate can see them."""
