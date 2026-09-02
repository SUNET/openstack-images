"""Domain-specific failures safe to expose in status or worker logs."""


class ValidationError(ValueError):
    """Desired state is invalid and should not be retried unchanged."""


class OwnershipError(RuntimeError):
    """An existing external resource is not owned by this cluster."""
