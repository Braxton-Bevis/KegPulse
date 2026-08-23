class DomainError(ValueError):
    """A user-facing domain rule was violated."""


class ConflictError(DomainError):
    """The requested mutation conflicts with authoritative state."""


class NotFoundError(DomainError):
    """A requested entity does not exist."""


class AuthorizationError(DomainError):
    """Administrative authorization is required or invalid."""


class MeasurementRejectedError(DomainError):
    """A device reading is structurally valid but cannot represent physical flow."""
