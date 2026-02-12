"""Kopf handlers for cluster-scoped OpenStack resources.

This package contains handlers for:
- OpenstackDomain
- OpenstackFlavor
- OpenstackImage
- OpenstackNetwork (provider networks)

All handlers follow the same patterns:
- Create/update/delete via Kopf decorators
- Status tracking via patch.status
- Registry-based resource tracking for GC
"""

# Import handlers to register them with Kopf
from handlers.domain import *  # noqa: F401, F403
from handlers.flavor import *  # noqa: F401, F403
from handlers.image import *  # noqa: F401, F403
from handlers.network import *  # noqa: F401, F403
from handlers.gc_cluster import *  # noqa: F401, F403
