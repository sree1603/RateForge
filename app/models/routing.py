import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    String,
    func,
    true,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Upstream(Base):
    """An approved backend service. Only administrators can create these."""

    __tablename__ = "upstreams"
    __table_args__ = (
        CheckConstraint("connect_timeout_seconds > 0", name="ck_upstreams_connect_timeout"),
        CheckConstraint("read_timeout_seconds > 0", name="ck_upstreams_read_timeout"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    base_url: Mapped[str] = mapped_column(String(2048))
    connect_timeout_seconds: Mapped[float] = mapped_column(Float, default=3.0)
    read_timeout_seconds: Mapped[float] = mapped_column(Float, default=30.0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Route(Base):
    """Maps /gateway/<path_prefix>/... to an upstream."""

    __tablename__ = "routes"
    __table_args__ = (
        CheckConstraint("cardinality(allowed_methods) > 0", name="ck_routes_methods_not_empty"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    upstream_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("upstreams.id", ondelete="CASCADE"), index=True
    )
    path_prefix: Mapped[str] = mapped_column(String(64), unique=True)
    allowed_methods: Mapped[list[str]] = mapped_column(ARRAY(String(10)))
    # True: /gateway/weather/current -> <base>/current. False: -> <base>/weather/current.
    strip_prefix: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    upstream: Mapped[Upstream] = relationship(lazy="joined")
