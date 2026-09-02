"""Runtime configuration read from the environment."""

import os
from dataclasses import dataclass

from .errors import ValidationError


@dataclass(frozen=True)
class Settings:
    operator_namespace: str
    worker_image: str
    worker_service_account: str
    reconcile_interval: int
    verification_interval: int

    @classmethod
    def from_env(cls) -> "Settings":
        image = os.getenv("WORKER_IMAGE", "").strip()
        if not image:
            raise ValidationError("WORKER_IMAGE must be configured")
        verification_interval = int(os.getenv("VERIFICATION_INTERVAL_SECONDS", "900"))
        if verification_interval < 60:
            raise ValidationError("VERIFICATION_INTERVAL_SECONDS must be at least 60")
        return cls(
            operator_namespace=os.getenv("OPERATOR_NAMESPACE", "openstack-operator"),
            worker_image=image,
            worker_service_account=os.getenv("WORKER_SERVICE_ACCOUNT", "customer-cluster-worker"),
            reconcile_interval=int(os.getenv("RECONCILE_INTERVAL_SECONDS", "30")),
            verification_interval=verification_interval,
        )
