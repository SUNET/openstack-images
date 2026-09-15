"""Structured rendering of the reviewed six-Application customer bootstrap."""

from __future__ import annotations

import ipaddress
import re
from typing import Any
from urllib.parse import urlsplit

import yaml

from app.gitops_types import ClusterInputs, CustomerGitOpsError

DEFAULT_BASES_URL = "https://platform.sunet.se/VDC/customer-clusters-k8s-bases.git"
BASE_PATH = "k8s-manifests"
BASE_APPLICATIONS = ("envoy-gateway", "cert-manager", "argocd", "portal-access")
INGRESS_RESOURCES = (
    "cilium-load-balancer.yaml",
    "envoy-proxy.yaml",
    "gateway.yaml",
    "issuer.yaml",
    "certificate.yaml",
    "routes.yaml",
)
APPLICATIONS = (
    "customer-cluster-apps",
    "envoy-gateway",
    "cert-manager",
    "argocd",
    "portal-access",
    "argocd-ingress",
)
POOL_LABEL = "customer-clusters.sunet.se/load-balancer-pool"
ENVOY_NAMESPACE = "envoy-gateway-system"
SHARED_README = """# Customer Kubernetes clusters

This private repository contains workload-cluster GitOps state for this customer
and environment. Each cluster has its own overlay under `clusters/`.

Operator-only declarations, generated Ansible inventory, OpenStack state,
kubeconfigs, private keys, tokens, cloud credentials, and plaintext Kubernetes
Secrets do not belong here.

## Bootstrap order

1. Initialize the reviewed, pinned `k8s-manifests/` submodule.
2. Apply Envoy Gateway, cert-manager, and Argo CD from that base.
3. Apply the cluster's `addons/argocd-ingress/` overlay and verify its Service.
4. Create the dedicated read-only repository credential in the cluster.
5. Apply the cluster's `argocd-apps/customer-cluster-apps.yaml` to hand the tree
   to Argo CD.

The ingress overlay README describes the traffic flow and bootstrap prerequisites.
"""


def validate_url(value: str) -> str:
    """Check URL syntax; the caller additionally enforces its allowed HTTPS origin."""
    try:
        parsed = urlsplit(value)
        valid = (
            isinstance(value, str)
            and parsed.scheme == "https"
            and parsed.hostname
            and (parsed.port is None or 1 <= parsed.port <= 65535)
            and not parsed.username
            and not parsed.password
            and "@" not in parsed.netloc
            and not parsed.query
            and not parsed.fragment
            and re.fullmatch(r"/[A-Za-z0-9_./~-]+", parsed.path)
            and not any(part in {".", "..", ""} for part in parsed.path.split("/")[1:])
            and not any(char.isspace() or ord(char) < 32 for char in value)
        )
    except (TypeError, ValueError, AttributeError):
        valid = False
    if not valid:
        raise CustomerGitOpsError(
            "A credential-free HTTPS repository URL is required", "invalid_url"
        )
    return value


def validate_slug(slug: str) -> None:
    if not isinstance(slug, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,61}[a-z0-9]|[a-z]", slug):
        raise CustomerGitOpsError("Invalid cluster slug", "invalid_input")


def _validate_inputs(inputs: ClusterInputs) -> None:
    validate_slug(inputs.slug)
    hostname = inputs.hostname
    if (
        not isinstance(hostname, str)
        or len(hostname) > 253
        or "." not in hostname
        or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in hostname.split(".")
        )
    ):
        raise CustomerGitOpsError("Invalid Argo CD hostname", "invalid_input")
    try:
        if not isinstance(inputs.ingress_vip, str):
            raise ipaddress.AddressValueError()
        ipaddress.IPv4Address(inputs.ingress_vip)
    except ipaddress.AddressValueError:
        raise CustomerGitOpsError(
            "Generated ingress VIP is not an IPv4 address", "invalid_input"
        ) from None
    if not isinstance(inputs.interface, str) or not re.fullmatch(
        r"[A-Za-z0-9_][A-Za-z0-9_.:-]{0,14}", inputs.interface
    ):
        raise CustomerGitOpsError("Invalid worker interface", "invalid_input")
    if not isinstance(inputs.acme_contact, str) or not re.fullmatch(
        r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?",
        inputs.acme_contact,
    ):
        raise CustomerGitOpsError("Invalid ACME contact email", "invalid_input")


def gitmodules(bases_url: str) -> str:
    validate_url(bases_url)
    return f'[submodule "{BASE_PATH}"]\n\tpath = {BASE_PATH}\n\turl = {bases_url}\n'


class _Dumper(yaml.SafeDumper):
    def ignore_aliases(self, data: Any) -> bool:
        return True


def dump_documents(documents: list[dict[str, Any]]) -> str:
    return yaml.dump_all(
        documents,
        Dumper=_Dumper,
        explicit_start=True,
        sort_keys=False,
        allow_unicode=True,
        width=100,
    )


def _resource(
    api_version: str, kind: str, name: str, spec: dict[str, Any], namespace: str | None = None
) -> dict[str, Any]:
    metadata = {"name": name}
    if namespace:
        metadata["namespace"] = namespace
    return {"apiVersion": api_version, "kind": kind, "metadata": metadata, "spec": spec}


def _application(name: str, repo_url: str, cluster: str) -> dict[str, Any]:
    namespace, wave = {
        "customer-cluster-apps": ("argocd", None),
        "envoy-gateway": (ENVOY_NAMESPACE, -30),
        "cert-manager": ("cert-manager", -20),
        "argocd": ("argocd", -20),
        "portal-access": ("argocd", -10),
        "argocd-ingress": (ENVOY_NAMESPACE, 0),
    }[name]
    path = {
        "customer-cluster-apps": f"{cluster}/argocd-apps",
        "argocd-ingress": f"{cluster}/addons/argocd-ingress",
    }.get(name, f"{BASE_PATH}/{name}")
    options = []
    if name in {"envoy-gateway", "cert-manager", "argocd"}:
        options.append("CreateNamespace=true")
    options.append("PruneLast=true")
    if name == "argocd":
        options.append("RespectIgnoreDifferences=true")
    options.append("ServerSideApply=true")
    application = _resource(
        "argoproj.io/v1alpha1",
        "Application",
        name,
        {
            "project": "default",
            "source": {"repoURL": repo_url, "targetRevision": "main", "path": path},
            "destination": {"server": "https://kubernetes.default.svc", "namespace": namespace},
            "syncPolicy": {
                "automated": {"prune": True, "selfHeal": True},
                "syncOptions": options,
            },
        },
        "argocd",
    )
    annotations = {"argocd.argoproj.io/compare-options": "ServerSideDiff=true"}
    if wave is not None:
        annotations["argocd.argoproj.io/sync-wave"] = str(wave)
    application["metadata"]["annotations"] = annotations
    if name == "argocd":
        application["spec"]["ignoreDifferences"] = [
            {
                "kind": "Secret",
                "name": "argocd-secret",
                "namespace": "argocd",
                "jsonPointers": ["/data"],
            }
        ]
    return application


def _kustomization(resources: list[str]) -> str:
    return dump_documents(
        [
            {
                "apiVersion": "kustomize.config.k8s.io/v1beta1",
                "kind": "Kustomization",
                "resources": resources,
            }
        ]
    )


def _ingress_documents(inputs: ClusterInputs) -> dict[str, list[dict[str, Any]]]:
    vip, hostname = inputs.ingress_vip, inputs.hostname
    selector = {"matchLabels": {POOL_LABEL: "ingress"}}
    topology_selector = {
        "matchLabels": {
            "gateway.envoyproxy.io/owning-gateway-name": "public",
            "gateway.envoyproxy.io/owning-gateway-namespace": ENVOY_NAMESPACE,
        }
    }
    pool = _resource(
        "cilium.io/v2",
        "CiliumLoadBalancerIPPool",
        "customer-ingress",
        {
            "blocks": [{"start": vip, "stop": vip}],
            "serviceSelector": selector,
        },
    )
    l2 = _resource(
        "cilium.io/v2alpha1",
        "CiliumL2AnnouncementPolicy",
        "customer-ingress",
        {
            "loadBalancerIPs": True,
            "interfaces": [f"^{re.escape(inputs.interface)}$"],
            "nodeSelector": {
                "matchExpressions": [
                    {
                        "key": "node-role.kubernetes.io/control-plane",
                        "operator": "DoesNotExist",
                    }
                ]
            },
            "serviceSelector": selector,
        },
    )
    envoy = _resource(
        "gateway.envoyproxy.io/v1alpha1",
        "EnvoyProxy",
        "public",
        {
            "ipFamily": "IPv4",
            "provider": {
                "type": "Kubernetes",
                "kubernetes": {
                    "envoyDeployment": {
                        "replicas": 2,
                        "pod": {
                            "topologySpreadConstraints": [
                                {
                                    "maxSkew": 1,
                                    "topologyKey": "kubernetes.io/hostname",
                                    "whenUnsatisfiable": "ScheduleAnyway",
                                    "labelSelector": topology_selector,
                                }
                            ]
                        },
                    },
                    "envoyPDB": {"minAvailable": 1},
                    "envoyService": {
                        "type": "LoadBalancer",
                        "allocateLoadBalancerNodePorts": False,
                        "externalTrafficPolicy": "Cluster",
                        "loadBalancerIP": vip,
                        "labels": {POOL_LABEL: "ingress"},
                    },
                },
            },
        },
        ENVOY_NAMESPACE,
    )
    listeners: list[dict[str, Any]] = []
    for protocol, port in (("HTTP", 80), ("HTTPS", 443)):
        listener: dict[str, Any] = {
            "name": f"argocd-{protocol.lower()}",
            "hostname": hostname,
            "protocol": protocol,
            "port": port,
            "allowedRoutes": {"namespaces": {"from": "All"}},
        }
        if protocol == "HTTPS":
            listener["tls"] = {
                "mode": "Terminate",
                "certificateRefs": [
                    {
                        "group": "",
                        "kind": "Secret",
                        "name": "argocd-gateway-tls",
                    }
                ],
            }
        listeners.append(listener)
    gateway = _resource(
        "gateway.networking.k8s.io/v1",
        "Gateway",
        "public",
        {
            "gatewayClassName": "envoy",
            "infrastructure": {
                "parametersRef": {
                    "group": "gateway.envoyproxy.io",
                    "kind": "EnvoyProxy",
                    "name": "public",
                }
            },
            "listeners": listeners,
        },
        ENVOY_NAMESPACE,
    )
    issuer = _resource(
        "cert-manager.io/v1",
        "Issuer",
        "letsencrypt",
        {
            "acme": {
                "email": inputs.acme_contact,
                "server": "https://acme-v02.api.letsencrypt.org/directory",
                "privateKeySecretRef": {"name": "letsencrypt-account-key"},
                "solvers": [
                    {
                        "http01": {
                            "gatewayHTTPRoute": {
                                "parentRefs": [
                                    {
                                        "group": "gateway.networking.k8s.io",
                                        "kind": "Gateway",
                                        "name": "public",
                                        "namespace": ENVOY_NAMESPACE,
                                        "sectionName": "argocd-http",
                                    }
                                ]
                            },
                        }
                    }
                ],
            }
        },
        ENVOY_NAMESPACE,
    )
    certificate = _resource(
        "cert-manager.io/v1",
        "Certificate",
        "argocd-gateway",
        {
            "secretName": "argocd-gateway-tls",
            "issuerRef": {
                "group": "cert-manager.io",
                "kind": "Issuer",
                "name": "letsencrypt",
            },
            "dnsNames": [hostname],
        },
        ENVOY_NAMESPACE,
    )
    routes = []
    for name, section, backend in (
        (
            "argocd",
            "argocd-https",
            {
                "backendRefs": [
                    {
                        "group": "",
                        "kind": "Service",
                        "name": "argocd-server",
                        "port": 80,
                        "weight": 1,
                    }
                ]
            },
        ),
        (
            "argocd-http-redirect",
            "argocd-http",
            {
                "filters": [
                    {
                        "type": "RequestRedirect",
                        "requestRedirect": {"scheme": "https", "statusCode": 301},
                    }
                ]
            },
        ),
    ):
        routes.append(
            _resource(
                "gateway.networking.k8s.io/v1",
                "HTTPRoute",
                name,
                {
                    "parentRefs": [
                        {
                            "group": "gateway.networking.k8s.io",
                            "kind": "Gateway",
                            "name": "public",
                            "namespace": ENVOY_NAMESPACE,
                            "sectionName": section,
                        }
                    ],
                    "hostnames": [hostname],
                    "rules": [
                        {
                            "matches": [{"path": {"type": "PathPrefix", "value": "/"}}],
                            **backend,
                        }
                    ],
                },
                "argocd",
            )
        )
    return dict(
        zip(
            INGRESS_RESOURCES,
            ([pool, l2], [envoy], [gateway], [issuer], [certificate], routes),
            strict=True,
        )
    )


def render_tree(
    *,
    repo_url: str,
    slug: str,
    hostname: str,
    ingress_vip: str,
    interface: str,
    acme_contact: str,
    bases_url: str | None = None,
) -> dict[str, str]:
    """Return only known generated paths, serialized from structured YAML objects."""
    validate_url(repo_url)
    inputs = ClusterInputs(slug, hostname, ingress_vip, interface, acme_contact)
    _validate_inputs(inputs)
    cluster = f"clusters/{slug}"
    ingress = f"{cluster}/addons/argocd-ingress"
    apps = f"{cluster}/argocd-apps"
    files = {
        "README.md": SHARED_README,
        ".gitmodules": gitmodules(bases_url if bases_url is not None else DEFAULT_BASES_URL),
        f"{ingress}/README.md": f"""# Argo CD ingress

This overlay assigns the operator-reserved private ingress VIP from generated
inventory to a two-replica Envoy data plane. Cilium announces that VIP from
worker `{interface}` interfaces, while OpenStack maps the cluster's ingress floating
IP to it.

The shared Gateway terminates a Let's Encrypt certificate for the canonical
Argo CD hostname in the cluster declaration and routes HTTPS to Argo CD's
internal HTTP service. Plain HTTP redirects to HTTPS; cert-manager's
more-specific HTTP-01 challenge route takes precedence during issuance.

The generated Envoy Service is selected by an explicit SUNET label rather than
by its generated name. The one-address Cilium pool therefore cannot be consumed
by an unrelated LoadBalancer Service.

Apply this overlay only after Envoy Gateway, cert-manager, and Argo CD are
available. See the repository README for the bootstrap order.
""",
        f"{ingress}/kustomization.yaml": _kustomization(list(INGRESS_RESOURCES)),
        f"{apps}/kustomization.yaml": _kustomization([f"{app}.yaml" for app in APPLICATIONS]),
    }
    files.update(
        {
            f"{ingress}/{name}": dump_documents(docs)
            for name, docs in _ingress_documents(inputs).items()
        }
    )
    files.update(
        {
            f"{apps}/{app}.yaml": dump_documents([_application(app, repo_url, cluster)])
            for app in APPLICATIONS
        }
    )
    return files
