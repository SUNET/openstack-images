"""Shared, credential-free contracts for the customer GitOps publisher."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypedDict


class CustomerGitOpsError(ValueError):
    """A repository cannot safely receive the approved generated tree."""

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ClusterInputs:
    slug: str
    hostname: str
    ingress_vip: str
    interface: str
    acme_contact: str


class ValidationResult(TypedDict):
    kustomize_version: str
    kustomizations: list[str]
    envoy_protections: bool


class Preview(TypedDict):
    version: int
    repo_url: str
    slug: str
    expected_head: str | None
    files: dict[str, str]
    observed: dict[str, str | None]
    observed_bases_revision: str | None
    bases_revision: str
    bases_url: str
    diff: str
    diff_kind: str
    formatting_paths: list[str]
    action: Literal["initialize", "add", "update", "adopt", "noop"]
    validation: ValidationResult
    fingerprint: str
