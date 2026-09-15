"""Reviewed resource contracts and hostile input boundaries for structured rendering."""

from __future__ import annotations

import copy
from typing import Any

import pytest
import yaml

from app.customer_gitops import CustomerGitOpsError, render_tree
from app.gitops_render import APPLICATIONS, BASE_APPLICATIONS, INGRESS_RESOURCES
from app.gitops_validate import canonical_tree, documents, semantic, validate_tree

INPUTS = {
    "repo_url": "https://git.example.test/customer/clusters.git",
    "bases_url": "https://git.example.test/public/bases.git",
    "slug": "acme-one",
    "hostname": "argocd.acme-one.k8s.example.test",
    "ingress_vip": "10.42.0.240",
    "interface": "ens3",
    "acme_contact": "noc@example.test",
}
INGRESS = "clusters/acme-one/addons/argocd-ingress"
APPS = "clusters/acme-one/argocd-apps"


def test_six_reviewed_applications_and_pruning_settings() -> None:
    files = render_tree(**INPUTS)
    apps = yaml.safe_load(files[f"{APPS}/kustomization.yaml"])
    assert apps["resources"] == [f"{name}.yaml" for name in APPLICATIONS]
    assert len(apps["resources"]) == 6
    assert "sealed-secrets" not in "".join(files.values())
    expected = {
        "customer-cluster-apps": (f"{APPS}", "argocd", None, False),
        "envoy-gateway": ("k8s-manifests/envoy-gateway", "envoy-gateway-system", "-30", True),
        "cert-manager": ("k8s-manifests/cert-manager", "cert-manager", "-20", True),
        "argocd": ("k8s-manifests/argocd", "argocd", "-20", True),
        "portal-access": ("k8s-manifests/portal-access", "argocd", "-10", False),
        "argocd-ingress": (INGRESS, "envoy-gateway-system", "0", False),
    }
    for name, (path, namespace, wave, create_namespace) in expected.items():
        doc = yaml.safe_load(files[f"{APPS}/{name}.yaml"])
        assert doc["metadata"]["name"] == name
        assert doc["metadata"]["namespace"] == "argocd"
        annotations = doc["metadata"]["annotations"]
        assert annotations["argocd.argoproj.io/compare-options"] == "ServerSideDiff=true"
        assert annotations.get("argocd.argoproj.io/sync-wave") == wave
        assert doc["spec"]["source"] == {
            "repoURL": INPUTS["repo_url"],
            "path": path,
            "targetRevision": "main",
        }
        assert doc["spec"]["destination"] == {
            "server": "https://kubernetes.default.svc",
            "namespace": namespace,
        }
        assert doc["spec"]["syncPolicy"]["automated"] == {"prune": True, "selfHeal": True}
        options = doc["spec"]["syncPolicy"]["syncOptions"]
        assert {"PruneLast=true", "ServerSideApply=true"} <= set(options)
        assert ("CreateNamespace=true" in options) is create_namespace
        assert ("RespectIgnoreDifferences=true" in options) is (name == "argocd")
    argocd = yaml.safe_load(files[f"{APPS}/argocd.yaml"])
    assert argocd["spec"]["ignoreDifferences"] == [
        {
            "kind": "Secret",
            "name": "argocd-secret",
            "namespace": "argocd",
            "jsonPointers": ["/data"],
        }
    ]


def test_reviewed_envoy_topology_service_and_cilium_shape() -> None:
    files = render_tree(**{**INPUTS, "interface": "ens3.100"})
    envoy = yaml.safe_load(files[f"{INGRESS}/envoy-proxy.yaml"])
    provider = envoy["spec"]["provider"]["kubernetes"]
    assert provider["envoyDeployment"] == {
        "replicas": 2,
        "pod": {
            "topologySpreadConstraints": [
                {
                    "maxSkew": 1,
                    "topologyKey": "kubernetes.io/hostname",
                    "whenUnsatisfiable": "ScheduleAnyway",
                    "labelSelector": {
                        "matchLabels": {
                            "gateway.envoyproxy.io/owning-gateway-name": "public",
                            "gateway.envoyproxy.io/owning-gateway-namespace": (
                                "envoy-gateway-system"
                            ),
                        }
                    },
                }
            ]
        },
    }
    assert provider["envoyPDB"] == {"minAvailable": 1}
    service = provider["envoyService"]
    assert service == {
        "type": "LoadBalancer",
        "allocateLoadBalancerNodePorts": False,
        "externalTrafficPolicy": "Cluster",
        "loadBalancerIP": INPUTS["ingress_vip"],
        "labels": {"customer-clusters.sunet.se/load-balancer-pool": "ingress"},
    }
    pool, l2 = list(yaml.safe_load_all(files[f"{INGRESS}/cilium-load-balancer.yaml"]))
    assert pool["spec"]["blocks"] == [
        {"start": INPUTS["ingress_vip"], "stop": INPUTS["ingress_vip"]}
    ]
    assert pool["spec"]["serviceSelector"]["matchLabels"] == service["labels"]
    assert l2["spec"]["serviceSelector"] == pool["spec"]["serviceSelector"]
    assert l2["spec"]["interfaces"] == [r"^ens3\.100$"]
    assert l2["spec"]["nodeSelector"] == {
        "matchExpressions": [
            {
                "key": "node-role.kubernetes.io/control-plane",
                "operator": "DoesNotExist",
            }
        ]
    }
    assert "&id" not in files[f"{INGRESS}/cilium-load-balancer.yaml"]
    validate_tree(files, INPUTS["repo_url"], INPUTS["bases_url"])


def test_reviewed_gateway_certificate_challenge_routes_and_no_credentials() -> None:
    files = render_tree(**INPUTS)
    gateway = yaml.safe_load(files[f"{INGRESS}/gateway.yaml"])
    http, https = gateway["spec"]["listeners"]
    assert http == {
        "name": "argocd-http",
        "hostname": INPUTS["hostname"],
        "protocol": "HTTP",
        "port": 80,
        "allowedRoutes": {"namespaces": {"from": "All"}},
    }
    assert https["tls"] == {
        "mode": "Terminate",
        "certificateRefs": [
            {
                "group": "",
                "kind": "Secret",
                "name": "argocd-gateway-tls",
            }
        ],
    }
    issuer = yaml.safe_load(files[f"{INGRESS}/issuer.yaml"])
    assert issuer["spec"]["acme"]["email"] == INPUTS["acme_contact"]
    assert (
        issuer["spec"]["acme"]["solvers"][0]["http01"]["gatewayHTTPRoute"]["parentRefs"][0][
            "sectionName"
        ]
        == "argocd-http"
    )
    cert = yaml.safe_load(files[f"{INGRESS}/certificate.yaml"])
    assert cert["spec"]["dnsNames"] == [INPUTS["hostname"]]
    route, redirect = list(yaml.safe_load_all(files[f"{INGRESS}/routes.yaml"]))
    for doc in (route, redirect):
        assert doc["spec"]["hostnames"] == [INPUTS["hostname"]]
        assert doc["spec"]["rules"][0]["matches"] == [
            {"path": {"type": "PathPrefix", "value": "/"}}
        ]
    assert route["spec"]["rules"][0]["backendRefs"] == [
        {
            "group": "",
            "kind": "Service",
            "name": "argocd-server",
            "port": 80,
            "weight": 1,
        }
    ]
    assert redirect["spec"]["rules"][0]["filters"] == [
        {
            "type": "RequestRedirect",
            "requestRedirect": {"scheme": "https", "statusCode": 301},
        }
    ]
    for path, text in files.items():
        if path.endswith(".yaml"):
            assert all(doc["kind"] not in {"Secret", "SealedSecret"} for doc in documents(text))


def test_shared_readme_is_generic_and_cluster_readme_is_reviewed() -> None:
    files = render_tree(**INPUTS)
    other = render_tree(
        **{
            **INPUTS,
            "slug": "second",
            "hostname": "argocd.second.example.test",
            "ingress_vip": "10.43.0.240",
            "interface": "ens4",
        }
    )
    assert files["README.md"] == other["README.md"]
    assert INPUTS["slug"] not in files["README.md"]
    assert INPUTS["ingress_vip"] not in files["README.md"]
    assert "worker `ens3` interfaces" in files[f"{INGRESS}/README.md"]
    assert "more-specific HTTP-01 challenge route" in files[f"{INGRESS}/README.md"]
    assert "explicit SUNET label" in files[f"{INGRESS}/README.md"]
    assert INPUTS["bases_url"] in files[".gitmodules"]
    assert len(BASE_APPLICATIONS) == 4
    assert yaml.safe_load(files[f"{INGRESS}/kustomization.yaml"])["resources"] == list(
        INGRESS_RESOURCES
    )
    assert files == canonical_tree(files, INPUTS["repo_url"], INPUTS["bases_url"])


@pytest.mark.parametrize(
    "field,value",
    [
        ("slug", "../other"),
        ("slug", "a/b"),
        ("slug", "a\\b"),
        ("slug", "a\nkind: Secret"),
        ("slug", "A"),
        ("slug", "a" * 64),
        ("slug", "--config"),
        ("slug", ""),
        ("hostname", "https://argocd.example.test"),
        ("hostname", "*.example.test"),
        ("hostname", "argocd.example.test\nkind: Secret"),
        ("hostname", "a..example.test"),
        ("ingress_vip", "::1"),
        ("ingress_vip", "10.2.3.4/24"),
        ("ingress_vip", "10.2.3.999"),
        ("interface", "ens3.*"),
        ("interface", "ens3\n- .*"),
        ("interface", "eth0/../bad"),
        ("acme_contact", "noc@example.test\nkind: Secret"),
        ("acme_contact", "not an email"),
        ("repo_url", "https://writer:secret@git.example.test/customer.git"),
        ("repo_url", "file:///tmp/repository"),
        ("repo_url", "ext::run-command"),
        ("repo_url", "https://git.example.test/customer.git?token=secret"),
        ("repo_url", "https://git.example.test/customer/../other.git"),
        ("bases_url", "https://writer:secret@git.example.test/public.git"),
        ("bases_url", "https://git.example.test/public.git#secret"),
    ],
)
def test_hostile_inputs_are_rejected(field: str, value: str) -> None:
    with pytest.raises(CustomerGitOpsError):
        render_tree(**{**INPUTS, field: value})


@pytest.mark.parametrize(
    "content",
    [
        "apiVersion: v1\nkind: Secret\nkind: Gateway\n",
        "a: &a [*a]\n",
        "!!python/object/apply:os.system ['false']\n",
        "---\nnull\n",
        "x: {a: 1, a: 2}\n",
    ],
)
def test_ambiguous_or_executable_yaml_is_rejected(content: str) -> None:
    with pytest.raises(CustomerGitOpsError):
        documents(content)


@pytest.mark.parametrize(
    "path,key,value",
    [
        (f"{APPS}/argocd.yaml", ("spec", "syncPolicy", "automated", "prune"), False),
        (
            f"{INGRESS}/envoy-proxy.yaml",
            ("spec", "provider", "kubernetes", "envoyDeployment", "pod"),
            {},
        ),
        (
            f"{INGRESS}/kustomization.yaml",
            ("resources",),
            ["https://attacker.example.test/secret.yaml"],
        ),
        (
            f"{APPS}/argocd.yaml",
            ("spec", "source", "repoURL"),
            "https://other.example.test/repo.git",
        ),
    ],
)
def test_modified_safety_contract_is_rejected(path: str, key: tuple[str, ...], value: Any) -> None:
    files = render_tree(**INPUTS)
    doc = yaml.safe_load(files[path])
    target = doc
    for part in key[:-1]:
        target = target[part]
    target[key[-1]] = copy.deepcopy(value)
    files[path] = yaml.safe_dump(doc)
    with pytest.raises(CustomerGitOpsError):
        validate_tree(files, INPUTS["repo_url"], INPUTS["bases_url"])


@pytest.mark.parametrize("value", [1, "true", 1.0])
def test_boolean_protection_is_not_coerced_from_another_yaml_type(value: Any) -> None:
    files = render_tree(**INPUTS)
    doc = yaml.safe_load(files[f"{APPS}/argocd.yaml"])
    doc["spec"]["syncPolicy"]["automated"]["prune"] = value
    files[f"{APPS}/argocd.yaml"] = yaml.safe_dump(doc)
    with pytest.raises(CustomerGitOpsError):
        validate_tree(files, INPUTS["repo_url"], INPUTS["bases_url"])


@pytest.mark.parametrize(
    "url",
    [
        "https://git.example.test/CUSTOMER/Clusters.git",
        "https://git.example.test/Customer/CLUSTERS.GIT",
        "https://git.example.test/Customer/Clusters",
        "https://git.example.test/Customer/Clusters.git/",
        "https://GIT.EXAMPLE.TEST:443/Customer/Clusters.git",
    ],
)
def test_equivalent_forgejo_application_sources_have_one_recorded_template(url: str) -> None:
    expected = render_tree(**INPUTS)
    files = dict(expected)
    for application in APPLICATIONS:
        path = f"{APPS}/{application}.yaml"
        doc = yaml.safe_load(files[path])
        doc["spec"]["source"]["repoURL"] = url
        files[path] = yaml.safe_dump(doc)
        assert semantic(files[path]) == semantic(expected[path])
    assert canonical_tree(files, INPUTS["repo_url"], INPUTS["bases_url"]) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://other.example.test/customer/clusters.git",
        "https://git.example.test:8443/customer/clusters.git",
        "https://git.example.test/other/clusters.git",
        "https://git.example.test/customer/other.git",
        "https://git.example.test/customer/clusters.git.git",
        "https://git.example.test/customer/clusters/extra.git",
        "https://git.example.test/customer//clusters.git",
        "https://git.example.test/C%75stomer/Clusters.git",
        "https://git.example.test/customer/clusters.git?token=secret",
        "https://git.example.test/customer/clusters.git#other",
        "https://user:secret@git.example.test/customer/clusters.git",
    ],
)
def test_source_identity_normalization_does_not_accept_other_repositories(url: str) -> None:
    files = render_tree(**INPUTS)
    doc = yaml.safe_load(files[f"{APPS}/argocd.yaml"])
    doc["spec"]["source"]["repoURL"] = url
    files[f"{APPS}/argocd.yaml"] = yaml.safe_dump(doc)
    with pytest.raises(CustomerGitOpsError):
        validate_tree(files, INPUTS["repo_url"], INPUTS["bases_url"])


@pytest.mark.parametrize(
    "field,value",
    [
        ("path", "k8s-manifests/ArgoCD"),
        ("targetRevision", "MAIN"),
    ],
)
def test_repository_identity_normalization_keeps_other_source_fields_case_sensitive(
    field: str,
    value: str,
) -> None:
    files = render_tree(**INPUTS)
    doc = yaml.safe_load(files[f"{APPS}/argocd.yaml"])
    doc["spec"]["source"][field] = value
    files[f"{APPS}/argocd.yaml"] = yaml.safe_dump(doc)
    with pytest.raises(CustomerGitOpsError):
        validate_tree(files, INPUTS["repo_url"], INPUTS["bases_url"])


def test_repository_identity_normalization_does_not_fold_hostnames_or_arbitrary_fields() -> None:
    gateway = render_tree(**INPUTS)[f"{INGRESS}/gateway.yaml"]
    assert semantic(gateway) != semantic(gateway.replace("argocd.acme-one", "ARGOCD.ACME-ONE"))
    assert semantic("note: https://git.example.test/Customer/Clusters.git\n") != semantic(
        "note: https://git.example.test/customer/clusters.git\n"
    )
