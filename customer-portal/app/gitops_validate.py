"""Strict manifest boundaries and offline Kustomize validation of immutable bases."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import asdict
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from app.gitops_git import GitRepository, isolated_environment
from app.gitops_render import (
    APPLICATIONS,
    BASE_APPLICATIONS,
    BASE_PATH,
    ENVOY_NAMESPACE,
    INGRESS_RESOURCES,
    render_tree,
    validate_slug,
)
from app.gitops_types import ClusterInputs, CustomerGitOpsError, ValidationResult
from app.repository_schemas import canonical_repository_url


class _StrictLoader(yaml.SafeLoader):
    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.AliasEvent):
            raise yaml.YAMLError("YAML aliases are not supported")
        return super().compose_node(parent, index)

    def construct_mapping(self, node: Any, deep: bool = False) -> dict[str, Any]:
        mapping: dict[str, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in mapping:
                raise yaml.YAMLError("Duplicate or non-string YAML key")
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def documents(content: str) -> list[dict[str, Any]]:
    try:
        docs = list(yaml.load_all(content, Loader=_StrictLoader))
        if not docs or any(not isinstance(doc, dict) for doc in docs):
            raise yaml.YAMLError("Expected Kubernetes objects")
        return docs
    except (yaml.YAMLError, ValueError, RecursionError, TypeError):
        raise CustomerGitOpsError(
            "Invalid or ambiguous managed YAML", "invalid_manifests"
        ) from None


def semantic(content: str) -> str:
    """Compare typed YAML with Forgejo identity normalization only on Application sources."""
    try:
        docs = documents(content)
        for doc in docs:
            if doc.get("apiVersion") != "argoproj.io/v1alpha1" or doc.get("kind") != "Application":
                continue
            spec = doc.get("spec")
            source = spec.get("source") if isinstance(spec, dict) else None
            if isinstance(source, dict) and "repoURL" in source:
                if not isinstance(source["repoURL"], str):
                    raise ValueError()
                source["repoURL"] = canonical_repository_url(source["repoURL"])
        return json.dumps(docs, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError):
        raise CustomerGitOpsError("Invalid managed YAML values", "invalid_manifests") from None


def known_paths(slug: str) -> set[str]:
    validate_slug(slug)
    prefix = f"clusters/{slug}"
    return (
        {"README.md", ".gitmodules"}
        | {
            f"{prefix}/addons/argocd-ingress/{name}"
            for name in (*INGRESS_RESOURCES, "kustomization.yaml", "README.md")
        }
        | {f"{prefix}/argocd-apps/{name}.yaml" for name in (*APPLICATIONS, "kustomization")}
    )


def validate_paths(
    files: dict[str, str], slug: str | None = None, *, complete: bool = True
) -> str:
    if not isinstance(files, dict) or not files or len(files) > 17:
        raise CustomerGitOpsError(
            "Expected a single complete generated cluster tree", "invalid_manifests"
        )
    if any(
        not isinstance(path, str) or not isinstance(content, str)
        for path, content in files.items()
    ):
        raise CustomerGitOpsError(
            "Generated paths and contents must be strings", "invalid_manifests"
        )
    clusters = {
        path.split("/")[1] for path in files if path.startswith("clusters/") and "/" in path[9:]
    }
    if slug is None:
        if len(clusters) != 1:
            raise CustomerGitOpsError(
                "Expected exactly one cluster in the generated tree", "invalid_manifests"
            )
        slug = clusters.pop()
    allowed = known_paths(slug)
    if not set(files) <= allowed or any(
        len(content.encode("utf-8")) > 256 * 1024 for content in files.values()
    ):
        raise CustomerGitOpsError("Unknown or oversized generated path", "invalid_manifests")
    required = {path for path in allowed if path.endswith(".yaml")}
    if complete and not required <= set(files):
        raise CustomerGitOpsError(
            "Generated cluster manifests are incomplete", "invalid_manifests"
        )
    return slug


def validate_tree(files: dict[str, str], repo_url: str, bases_url: str) -> ClusterInputs:
    """Accept only the reviewed schema and internally consistent customer inputs.

    Formatting and equivalent Forgejo owner/repository URL spelling are
    insignificant. Cluster paths, hostnames, pruning and topology protections
    remain exact structured comparisons.
    """
    slug = validate_paths(files)
    ingress = f"clusters/{slug}/addons/argocd-ingress"
    parsed = {
        path: documents(content) for path, content in files.items() if path.endswith(".yaml")
    }
    try:
        pool, l2 = parsed[f"{ingress}/cilium-load-balancer.yaml"]
        pattern = l2["spec"]["interfaces"][0]
        interface = re.sub(r"\\([.:-])", r"\1", pattern[1:-1])
        if pattern != f"^{re.escape(interface)}$":
            raise ValueError()
        inputs = ClusterInputs(
            slug=slug,
            hostname=parsed[f"{ingress}/gateway.yaml"][0]["spec"]["listeners"][0]["hostname"],
            ingress_vip=pool["spec"]["blocks"][0]["start"],
            interface=interface,
            acme_contact=parsed[f"{ingress}/issuer.yaml"][0]["spec"]["acme"]["email"],
        )
        expected = render_tree(repo_url=repo_url, bases_url=bases_url, **asdict(inputs))
        for path in parsed:
            if semantic(files[path]) != semantic(expected[path]):
                raise ValueError()
        for path, content in files.items():
            if not path.endswith(".yaml") and content != expected[path]:
                raise ValueError()
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        raise CustomerGitOpsError(
            "Managed manifests differ from the reviewed schema "
            "or contain inconsistent customer values",
            "incompatible_manifests",
        ) from None
    return inputs


def canonical_tree(files: dict[str, str], repo_url: str, bases_url: str) -> dict[str, str]:
    inputs = validate_tree(files, repo_url, bases_url)
    canonical = render_tree(repo_url=repo_url, bases_url=bases_url, **asdict(inputs))
    return {path: canonical[path] for path in files}


def _local_reference(root: Path, directory: Path, reference: Any) -> Path:
    if not isinstance(reference, str) or not re.fullmatch(r"[A-Za-z0-9_./-]+", reference):
        raise CustomerGitOpsError("Bases must use local Kustomize resources only", "unsafe_bases")
    path = (directory / reference).resolve()
    if not path.is_relative_to(root) or not path.exists():
        raise CustomerGitOpsError("Bases contain an escaping or missing resource", "unsafe_bases")
    return path


def _check_kustomization(root: Path, directory: Path, visited: set[Path]) -> None:
    if directory in visited:
        return
    visited.add(directory)
    paths = [
        directory / name
        for name in ("kustomization.yaml", "kustomization.yml", "Kustomization")
        if (directory / name).is_file()
    ]
    if len(paths) != 1:
        raise CustomerGitOpsError("Bases are missing an unambiguous Kustomization", "unsafe_bases")
    docs = documents(paths[0].read_text(encoding="utf-8"))
    if len(docs) != 1:
        raise CustomerGitOpsError("Invalid base Kustomization", "unsafe_bases")
    doc = docs[0]
    # No plugins, Helm, generators, remote bases, custom transformers or open-ended
    # file loading. This is the vocabulary used by the reviewed vendored bases.
    allowed = {
        "apiVersion",
        "kind",
        "resources",
        "patches",
        "images",
        "namespace",
        "labels",
        "commonLabels",
        "commonAnnotations",
        "namePrefix",
        "nameSuffix",
    }
    if not doc.keys() <= allowed or doc.get("kind") != "Kustomization":
        raise CustomerGitOpsError("Unsupported base Kustomization feature", "unsafe_bases")
    resources = doc.get("resources", [])
    patches = doc.get("patches", [])
    if not isinstance(resources, list) or not isinstance(patches, list):
        raise CustomerGitOpsError("Invalid base resources or patches", "unsafe_bases")
    for ref in resources:
        path = _local_reference(root, directory, ref)
        if path.is_dir():
            _check_kustomization(root, path, visited)
    for patch in patches:
        if not isinstance(patch, dict) or not patch.keys() <= {
            "path",
            "patch",
            "target",
            "options",
        }:
            raise CustomerGitOpsError("Unsupported base patch", "unsafe_bases")
        if "path" in patch:
            path = _local_reference(root, directory, patch["path"])
            if not path.is_file():
                raise CustomerGitOpsError("Invalid base patch path", "unsafe_bases")


def _materialize_base(repo: GitRepository, revision: str, root: Path) -> None:
    total = 0
    for path, entry in repo.tree(revision).items():
        parts = PurePosixPath(path).parts
        if entry.mode == "160000" or entry.mode == "120000" or ".gitmodules" in parts:
            raise CustomerGitOpsError(
                "Bases may not contain submodules or symlinks", "unsafe_bases"
            )
        if entry.kind == "tree" or parts[0] not in BASE_APPLICATIONS:
            continue
        if (
            entry.mode not in {"100644", "100755"}
            or not re.fullmatch(r"[A-Za-z0-9_./-]+", path)
            or any(part in {"..", ".", ".git"} for part in path.split("/"))
            or path.startswith("/")
        ):
            raise CustomerGitOpsError("Unsafe file in the pinned bases", "unsafe_bases")
        content = repo.blob(entry, maximum=16 * 1024 * 1024)
        total += len(content.encode())
        if total > 128 * 1024 * 1024:
            raise CustomerGitOpsError("Bases exceed publisher limits", "repository_too_large")
        destination = root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")


def _build(executable: str, directory: Path) -> list[dict[str, Any]]:
    try:
        result = subprocess.run(
            [executable, "build", str(directory)],
            env=isolated_environment(),
            cwd=directory,
            capture_output=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise CustomerGitOpsError(
            "Kustomize validation failed or timed out", "kustomize_failed"
        ) from None
    if result.returncode or len(result.stdout) > 32 * 1024 * 1024:
        raise CustomerGitOpsError("Kustomize validation failed", "kustomize_failed")
    try:
        return documents(result.stdout.decode("utf-8"))
    except UnicodeError:
        raise CustomerGitOpsError("Invalid Kustomize output", "kustomize_failed") from None


def validate_envoy_protections(docs: list[dict[str, Any]]) -> None:
    try:
        objects = {(doc["kind"], doc["metadata"]["name"]): doc for doc in docs}
        if len(objects) != len(docs):
            raise ValueError()
        protected = (
            {
                (kind, "eg-gateway-helm-certgen")
                for kind in ("ServiceAccount", "Role", "RoleBinding", "Job")
            }
            | {
                (kind, "eg-gateway-helm-certgen:envoy-gateway-system")
                for kind in ("ClusterRole", "ClusterRoleBinding")
            }
            | {
                (
                    "MutatingWebhookConfiguration",
                    "envoy-gateway-topology-injector.envoy-gateway-system",
                )
            }
        )
        for key in protected:
            obj = objects[key]
            annotations = obj["metadata"]["annotations"]
            sync_options = {
                value.strip()
                for value in annotations["argocd.argoproj.io/sync-options"].split(",")
            }
            compare_options = {
                value.strip()
                for value in annotations["argocd.argoproj.io/compare-options"].split(",")
            }
            hooks = {value.strip() for value in annotations["helm.sh/hook"].split(",")}
            if (
                "Prune=false" not in sync_options
                or "Prune=true" in sync_options
                or "IgnoreExtraneous" not in compare_options
                or not {"pre-install", "pre-upgrade"} <= hooks
            ):
                raise ValueError()
        deployment = objects[("Deployment", "envoy-gateway")]
        spec = deployment["spec"]
        labels = spec["template"]["metadata"]["labels"]
        constraints = spec["template"]["spec"]["topologySpreadConstraints"]
        if spec["replicas"] < 2 or not any(
            constraint["maxSkew"] == 1
            and constraint["topologyKey"] == "kubernetes.io/hostname"
            and constraint["whenUnsatisfiable"] == "ScheduleAnyway"
            and constraint["labelSelector"]["matchLabels"]
            and constraint["labelSelector"]["matchLabels"].items() <= labels.items()
            for constraint in constraints
        ):
            raise ValueError()
        pdb = objects[("PodDisruptionBudget", "envoy-gateway")]["spec"]
        if (
            pdb["minAvailable"] != 1
            or not pdb["selector"]["matchLabels"].items() <= labels.items()
        ):
            raise ValueError()
        webhook = objects[
            (
                "MutatingWebhookConfiguration",
                "envoy-gateway-topology-injector.envoy-gateway-system",
            )
        ]
        hook = next(
            item
            for item in webhook["webhooks"]
            if item["name"] == "topology.webhook.gateway.envoyproxy.io"
        )
        service = hook["clientConfig"]["service"]
        if (
            service["name"] != "envoy-gateway"
            or service["namespace"] != ENVOY_NAMESPACE
            or service["path"] != "/inject-pod-topology"
            or service["port"] != 9443
            or not any("pods/binding" in rule["resources"] for rule in hook["rules"])
        ):
            raise ValueError()
        crd = objects[("CustomResourceDefinition", "envoyproxies.gateway.envoyproxy.io")]
        version = next(
            version
            for version in crd["spec"]["versions"]
            if version["name"] == "v1alpha1" and version["served"]
        )
        schema = version["schema"]["openAPIV3Schema"]
        for field in (
            "spec",
            "provider",
            "kubernetes",
            "envoyDeployment",
            "pod",
            "topologySpreadConstraints",
        ):
            schema = schema["properties"][field]
        if schema["type"] != "array":
            raise ValueError()
    except (KeyError, IndexError, TypeError, ValueError, AttributeError, StopIteration):
        raise CustomerGitOpsError(
            "Pinned bases lack the reviewed Envoy hook, pruning or topology protections",
            "unsafe_bases",
        ) from None


def validate_kustomize(
    root: Path,
    files: dict[str, str],
    bases_url: str,
    bases_revision: str,
) -> ValidationResult:
    executable = shutil.which("kustomize")
    if executable is None:
        raise CustomerGitOpsError("The kustomize executable is required", "kustomize_missing")
    try:
        result = subprocess.run(
            [executable, "version"],
            env=isolated_environment(),
            capture_output=True,
            timeout=10,
            check=False,
        )
        version = result.stdout.decode("ascii").strip()
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        raise CustomerGitOpsError(
            "Cannot identify the Kustomize executable", "kustomize_failed"
        ) from None
    if result.returncode or not re.fullmatch(r"v?\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?", version):
        raise CustomerGitOpsError("Cannot identify the Kustomize executable", "kustomize_failed")
    slug = validate_paths(files)
    work = root / "validation"
    work.mkdir()
    for path, content in files.items():
        if not path.endswith(".yaml"):
            continue
        destination = work / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
    base = GitRepository.public_base(root, bases_url, bases_revision)
    base_root = work / BASE_PATH
    base_root.mkdir()
    _materialize_base(base, bases_revision, base_root)
    paths = [f"clusters/{slug}/addons/argocd-ingress", f"clusters/{slug}/argocd-apps"]
    paths.extend(f"{BASE_PATH}/{name}" for name in BASE_APPLICATIONS)
    for path in paths:
        _check_kustomization(work, work / path, set())
        output = _build(executable, work / path)
        if path == f"{BASE_PATH}/envoy-gateway":
            validate_envoy_protections(output)
    return {"kustomize_version": version, "kustomizations": paths, "envoy_protections": True}
