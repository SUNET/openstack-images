"""Consistent YAML rendering for Git-managed Kubernetes resources."""

from typing import Any

import yaml


class IndentedSafeDumper(yaml.SafeDumper):
    """Indent block sequences beneath their mapping key for YAML linters."""

    def increase_indent(self, flow: bool = False, indentless: bool = False):
        return super().increase_indent(flow, indentless=False)


def dump_yaml(document: Any, *, sort_keys: bool = True) -> str:
    """Render safe, consistently indented Kubernetes YAML."""
    return yaml.dump(
        document,
        Dumper=IndentedSafeDumper,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=sort_keys,
    )
