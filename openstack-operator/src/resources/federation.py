"""Federation mapping management for OpenStack projects."""

import json
import logging
from typing import Any

from openstack_client import OpenStackClient
from utils import make_group_name

logger = logging.getLogger(__name__)


def generate_mapping_rule(
    project_name: str,
    users: list[str],
    domain: str,
) -> dict[str, Any]:
    """Generate a federation mapping rule for a project.

    Creates a rule that maps OIDC users to an ephemeral Keystone user
    and adds them to the project's group.

    Args:
        project_name: OpenStack project name
        users: List of user identifiers (e.g., emails)
        domain: Domain for users and groups

    Returns:
        Mapping rule dict
    """
    group_name = make_group_name(project_name)

    return {
        "local": [
            {
                "user": {
                    "name": "{0}",
                    "domain": {"name": domain},
                    "type": "ephemeral",
                }
            },
            {
                "group": {
                    "name": group_name,
                    "domain": {"name": domain},
                }
            },
        ],
        "remote": [
            {"type": "HTTP_OIDC_SUB"},
            {"type": "HTTP_OIDC_SUB", "any_one_of": users},
        ],
    }


class FederationManager:
    """Manages federation mappings across multiple OpenstackProject CRs."""

    def __init__(
        self,
        client: OpenStackClient,
        idp_name: str,
        idp_remote_id: str,
        sso_domain: str,
    ) -> None:
        """Initialize federation manager.

        Args:
            client: OpenStack client
            idp_name: Identity provider name
            idp_remote_id: Remote ID for the IdP (e.g., OIDC issuer URL)
            sso_domain: Domain for SSO users
        """
        self.client = client
        self.idp_name = idp_name
        self.idp_remote_id = idp_remote_id
        self.sso_domain = sso_domain
        self.mapping_name = f"{idp_name}_oidc_mapping"

    def ensure_identity_provider(self) -> None:
        """Ensure the identity provider exists.

        When creating a new IdP, resolves the sso_domain to a domain_id
        so federated users land in the correct domain instead of Keystone
        auto-generating a dedicated federated domain.
        """
        idp = self.client.get_identity_provider(self.idp_name)
        if not idp:
            domain_id = None
            domain = self.client.get_domain(self.sso_domain)
            if domain:
                domain_id = domain.id
            else:
                logger.warning(
                    "SSO domain %r not found, IdP will use auto-generated domain",
                    self.sso_domain,
                )
            self.client.create_identity_provider(
                self.idp_name, [self.idp_remote_id], domain_id=domain_id
            )
            logger.info(f"Created identity provider: {self.idp_name}")

    def ensure_federation_protocol(self) -> None:
        """Ensure the federation protocol exists."""
        protocol = self.client.get_federation_protocol(self.idp_name, "openid")
        if not protocol:
            self.client.create_federation_protocol(
                self.idp_name, "openid", self.mapping_name
            )
            logger.info(f"Created federation protocol: openid")

    def get_current_mapping_rules(self) -> list[dict[str, Any]]:
        """Get current mapping rules from OpenStack."""
        mapping = self.client.get_mapping(self.mapping_name)
        if mapping:
            return mapping.rules or []
        return []

    def update_mapping(self, rules: list[dict[str, Any]]) -> None:
        """Update or create the federation mapping."""
        mapping = self.client.get_mapping(self.mapping_name)
        if mapping:
            self.client.update_mapping(self.mapping_name, rules)
            logger.info(f"Updated mapping: {self.mapping_name}")
        else:
            self.client.create_mapping(self.mapping_name, rules)
            logger.info(f"Created mapping: {self.mapping_name}")

    def add_project_mapping(
        self,
        project_name: str,
        users: list[str],
    ) -> None:
        """Add or update mapping rule for a project.

        Args:
            project_name: OpenStack project name
            users: List of user identifiers
        """
        if not users:
            logger.debug(f"No users for project {project_name}, skipping mapping")
            return

        self.ensure_identity_provider()

        # Get current rules
        current_rules = self.get_current_mapping_rules()

        # Find and remove existing rule for this project
        group_name = make_group_name(project_name)
        new_rules = [
            rule
            for rule in current_rules
            if not self._rule_matches_group(rule, group_name)
        ]

        # Add new rule for this project
        new_rule = generate_mapping_rule(project_name, users, self.sso_domain)
        new_rules.append(new_rule)

        # Update mapping
        self.update_mapping(new_rules)

        # Ensure protocol exists
        self.ensure_federation_protocol()

        logger.info(
            f"Updated federation mapping for project {project_name} "
            f"with {len(users)} users"
        )

    def remove_project_mapping(self, project_name: str) -> None:
        """Remove mapping rule for a project.

        Args:
            project_name: OpenStack project name
        """
        current_rules = self.get_current_mapping_rules()
        group_name = make_group_name(project_name)

        new_rules = [
            rule
            for rule in current_rules
            if not self._rule_matches_group(rule, group_name)
        ]

        if len(new_rules) == len(current_rules):
            logger.debug(
                f"No federation mapping found for project {project_name}"
            )
            return

        if not new_rules:
            # Keystone doesn't allow empty mapping rules
            # Keep the mapping but log a warning
            logger.warning(
                f"Cannot remove last mapping rule for {project_name}, "
                "Keystone requires at least one rule"
            )
            return

        self.update_mapping(new_rules)
        logger.info(f"Removed federation mapping for project {project_name}")

    def _rule_matches_group(
        self, rule: dict[str, Any], group_name: str
    ) -> bool:
        """Check if a mapping rule is for a specific group."""
        local = rule.get("local", [])
        for item in local:
            group = item.get("group", {})
            if group.get("name") == group_name:
                return True
        return False


def sync_federation_mapping(
    client: OpenStackClient,
    idp_name: str,
    idp_remote_id: str,
    sso_domain: str,
    project_users: dict[str, list[str]],
) -> None:
    """Sync federation mapping with all project users.

    This is a full reconciliation that rebuilds the mapping from scratch.

    Args:
        client: OpenStack client
        idp_name: Identity provider name
        idp_remote_id: Remote ID for the IdP
        sso_domain: Domain for SSO users
        project_users: Dict mapping project names to user lists
    """
    manager = FederationManager(client, idp_name, idp_remote_id, sso_domain)
    manager.ensure_identity_provider()

    rules = []
    for project_name, users in project_users.items():
        if users:
            rule = generate_mapping_rule(project_name, users, sso_domain)
            rules.append(rule)

    if not rules:
        logger.warning(
            "No project mappings to sync, skipping (Keystone requires at least one rule)"
        )
        return

    manager.update_mapping(rules)
    manager.ensure_federation_protocol()

    logger.info(
        f"Synced federation mapping with {len(rules)} project rules"
    )
