class ServiceError(Exception):
    """Base class for business-rule failures. Routers translate these to HTTP errors."""


class NotFoundError(ServiceError):
    pass


class ConflictError(ServiceError):
    pass
