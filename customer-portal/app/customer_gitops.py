"""Render and publish the customer-owned GitOps bootstrap tree."""
# ruff: noqa: E501

from __future__ import annotations

import ipaddress
import tempfile
from pathlib import Path

import git

from app.config import Settings
from app.git_url import git_auth_environment


class CustomerGitOpsError(ValueError):
    """A customer repository cannot safely receive the generated tree."""


def _application(name: str, repo_url: str, path: str, namespace: str, wave: int) -> str:
    create_namespace = (
        "\n      - CreateNamespace=true" if name not in {"portal-access", "argocd-ingress"} else ""
    )
    extra = (
        "\n      - RespectIgnoreDifferences=true\n  ignoreDifferences:\n"
        "    - kind: Secret\n      name: argocd-secret\n      namespace: argocd\n"
        "      jsonPointers:\n        - /data"
        if name == "argocd"
        else ""
    )
    return f"""---
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: {name}
  namespace: argocd
  annotations:
    argocd.argoproj.io/compare-options: ServerSideDiff=true
    argocd.argoproj.io/sync-wave: \"{wave}\"
spec:
  project: default
  source:
    repoURL: {repo_url}
    targetRevision: main
    path: {path}
  destination:
    server: https://kubernetes.default.svc
    namespace: {namespace}
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:{create_namespace}
      - PruneLast=true
      - ServerSideApply=true{extra}
"""


def render_tree(*, repo_url: str, slug: str, hostname: str, ingress_vip: str, interface: str, acme_contact: str) -> dict[str, str]:
    """Return a complete, credential-free tree for one cluster."""
    try:
        ipaddress.IPv4Address(ingress_vip)
    except ipaddress.AddressValueError as err:
        raise CustomerGitOpsError("generated ingress VIP is not an IPv4 address") from err
    if not interface:
        raise CustomerGitOpsError("node interface is not configured")
    cluster = f"clusters/{slug}"
    ingress = f"{cluster}/addons/argocd-ingress"
    apps = f"{cluster}/argocd-apps"
    return {
        "README.md": f"""# Customer Kubernetes clusters

This private repository contains workload-cluster GitOps state for this customer and environment. Operator-only declarations, generated Ansible inventory, OpenStack state, kubeconfigs, private keys, tokens, cloud credentials, and plaintext Kubernetes Secrets do not belong here.

## Bootstrap order

1. Initialize the pinned `k8s-manifests/` submodule.
2. Apply Envoy Gateway, cert-manager, and Argo CD from that base.
3. Apply `{ingress}/` and verify the generated Service has private VIP `{ingress_vip}`.
4. Create the dedicated read-only repository credential in the cluster; never commit it.
5. Apply `{apps}/customer-cluster-apps.yaml` to hand the tree to Argo CD.
""",
        ".gitmodules": "[submodule \"k8s-manifests\"]\n\tpath = k8s-manifests\n\turl = https://platform.sunet.se/VDC/customer-clusters-k8s-bases.git\n",
        f"{ingress}/README.md": "# Argo CD ingress\n\nThis overlay uses the operator-reserved private ingress VIP and worker interface. Apply it only after Envoy Gateway, cert-manager, and Argo CD are available.\n",
        f"{ingress}/kustomization.yaml": "---\napiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\nresources:\n  - cilium-load-balancer.yaml\n  - envoy-proxy.yaml\n  - gateway.yaml\n  - issuer.yaml\n  - certificate.yaml\n  - routes.yaml\n",
        f"{ingress}/cilium-load-balancer.yaml": f"""---
apiVersion: cilium.io/v2
kind: CiliumLoadBalancerIPPool
metadata:
  name: customer-ingress
spec:
  blocks:
    - start: \"{ingress_vip}\"
      stop: \"{ingress_vip}\"
  serviceSelector:
    matchLabels:
      customer-clusters.sunet.se/load-balancer-pool: ingress
---
apiVersion: cilium.io/v2alpha1
kind: CiliumL2AnnouncementPolicy
metadata:
  name: customer-ingress
spec:
  loadBalancerIPs: true
  interfaces:
    - ^{interface}$
  nodeSelector:
    matchExpressions:
      - key: node-role.kubernetes.io/control-plane
        operator: DoesNotExist
  serviceSelector:
    matchLabels:
      customer-clusters.sunet.se/load-balancer-pool: ingress
""",
        f"{ingress}/envoy-proxy.yaml": f"""---
apiVersion: gateway.envoyproxy.io/v1alpha1
kind: EnvoyProxy
metadata:
  name: public
  namespace: envoy-gateway-system
spec:
  ipFamily: IPv4
  provider:
    type: Kubernetes
    kubernetes:
      envoyDeployment:
        replicas: 2
      envoyPDB:
        minAvailable: 1
      envoyService:
        type: LoadBalancer
        allocateLoadBalancerNodePorts: false
        externalTrafficPolicy: Cluster
        loadBalancerIP: {ingress_vip}
        labels:
          customer-clusters.sunet.se/load-balancer-pool: ingress
""",
        f"{ingress}/gateway.yaml": f"""---
apiVersion: gateway.networking.k8s.io/v1
kind: Gateway
metadata:
  name: public
  namespace: envoy-gateway-system
spec:
  gatewayClassName: envoy
  infrastructure:
    parametersRef:
      group: gateway.envoyproxy.io
      kind: EnvoyProxy
      name: public
  listeners:
    - name: argocd-http
      hostname: {hostname}
      protocol: HTTP
      port: 80
      allowedRoutes:
        namespaces:
          from: All
    - name: argocd-https
      hostname: {hostname}
      protocol: HTTPS
      port: 443
      allowedRoutes:
        namespaces:
          from: All
      tls:
        mode: Terminate
        certificateRefs:
          - group: \"\"
            kind: Secret
            name: argocd-gateway-tls
""",
        f"{ingress}/issuer.yaml": f"""---
apiVersion: cert-manager.io/v1
kind: Issuer
metadata:
  name: letsencrypt
  namespace: envoy-gateway-system
spec:
  acme:
    email: {acme_contact}
    server: https://acme-v02.api.letsencrypt.org/directory
    privateKeySecretRef:
      name: letsencrypt-account-key
    solvers:
      - http01:
          gatewayHTTPRoute:
            parentRefs:
              - group: gateway.networking.k8s.io
                kind: Gateway
                name: public
                namespace: envoy-gateway-system
                sectionName: argocd-http
""",
        f"{ingress}/certificate.yaml": f"""---
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: argocd-gateway
  namespace: envoy-gateway-system
spec:
  secretName: argocd-gateway-tls
  issuerRef:
    group: cert-manager.io
    kind: Issuer
    name: letsencrypt
  dnsNames:
    - {hostname}
""",
        f"{ingress}/routes.yaml": f"""---
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: argocd
  namespace: argocd
spec:
  parentRefs:
    - name: public
      namespace: envoy-gateway-system
      sectionName: argocd-https
  hostnames: [{hostname}]
  rules:
    - backendRefs:
        - name: argocd-server
          port: 80
---
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: argocd-http-redirect
  namespace: argocd
spec:
  parentRefs:
    - name: public
      namespace: envoy-gateway-system
      sectionName: argocd-http
  hostnames: [{hostname}]
  rules:
    - filters:
        - type: RequestRedirect
          requestRedirect:
            scheme: https
            statusCode: 301
""",
        f"{apps}/kustomization.yaml": "---\napiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\nresources:\n  - customer-cluster-apps.yaml\n  - envoy-gateway.yaml\n  - cert-manager.yaml\n  - argocd.yaml\n  - portal-access.yaml\n  - argocd-ingress.yaml\n",
        f"{apps}/customer-cluster-apps.yaml": _application("customer-cluster-apps", repo_url, f"{cluster}/argocd-apps", "argocd", 0),
        f"{apps}/envoy-gateway.yaml": _application("envoy-gateway", repo_url, "k8s-manifests/envoy-gateway", "envoy-gateway-system", -30),
        f"{apps}/cert-manager.yaml": _application("cert-manager", repo_url, "k8s-manifests/cert-manager", "cert-manager", -20),
        f"{apps}/argocd.yaml": _application("argocd", repo_url, "k8s-manifests/argocd", "argocd", -20),
        f"{apps}/portal-access.yaml": _application("portal-access", repo_url, "k8s-manifests/portal-access", "argocd", -10),
        f"{apps}/argocd-ingress.yaml": _application("argocd-ingress", repo_url, f"{cluster}/addons/argocd-ingress", "envoy-gateway-system", 0),
    }


def publish_tree(*, repo_url: str, username: str, token: str, files: dict[str, str], settings: Settings) -> None:
    """Commit a fresh cluster tree; refuse to overwrite any existing path."""
    env = git_auth_environment(repo_url, username, token)
    with tempfile.TemporaryDirectory(prefix="customer-gitops-") as temporary:
        checkout = Path(temporary) / "repository"
        try:
            repo = git.Repo.clone_from(repo_url, checkout, branch="main", env=env)
        except git.GitCommandError as exc:
            # Forgejo repositories are deliberately created empty.  Initialize
            # the first main branch locally instead of requiring a manual seed.
            message = str(exc).lower()
            if "remote branch main not found" not in message:
                raise
            repo = git.Repo.init(checkout, initial_branch="main")
            repo.create_remote("origin", repo_url)
        cluster_paths = {Path(path).parts[1] for path in files if path.startswith("clusters/")}
        for slug in cluster_paths:
            if (checkout / "clusters" / slug).exists():
                raise CustomerGitOpsError(f"GitOps tree for cluster '{slug}' already exists")
        for path, content in files.items():
            if path == ".gitmodules":
                continue
            destination = checkout / path
            if destination.exists() and path in {"README.md", ".gitmodules"}:
                raise CustomerGitOpsError(f"repository already contains {path}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content)
        if not (checkout / "k8s-manifests").exists():
            if not settings.customer_cluster_bases_revision:
                raise CustomerGitOpsError("CUSTOMER_CLUSTER_BASES_REVISION must pin the bases submodule")
            repo.git.submodule("add", settings.customer_cluster_bases_url, "k8s-manifests")
            repo.git.submodule("update", "--init", "--recursive")
            base = git.Repo(checkout / "k8s-manifests")
            base.git.checkout(settings.customer_cluster_bases_revision)
        repo.git.add("-A")
        if not repo.is_dirty(index=True, working_tree=True, untracked_files=True):
            return
        repo.index.commit(
            "Add customer cluster GitOps bootstrap",
            author=git.Actor(settings.git_author_name, settings.git_author_email),
            committer=git.Actor(settings.git_author_name, settings.git_author_email),
        )
        with repo.git.custom_environment(**env):
            repo.remotes.origin.push("HEAD:main")
