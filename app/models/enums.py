from enum import StrEnum


class TenantStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


class KeyStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"
