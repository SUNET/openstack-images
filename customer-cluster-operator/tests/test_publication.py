"""Publication integration tests use only explicitly injected local bare Git transports."""

from __future__ import annotations

import base64
import os
import subprocess
import traceback
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import yaml

from customer_cluster_operator import publication, publication_git
from customer_cluster_operator.errors import InventoryConflict, ValidationError
from customer_cluster_operator.inventory import (
    publish_inventory,
    render_cluster_policy,
    render_inventory,
)
from customer_cluster_operator.publication_policy import HOSTS_MARKER

URL = "https://git.example.org/clusters.git"
TOKEN = "publication-test-secret"
HOSTS = "clusters/example/generated/ansible/hosts.yml"
POLICY = "inventory/clusters/example.yml"
SOURCE = "clusters/example/cluster.yaml"


def declaration(data):
    return yaml.safe_dump({
        "apiVersion": "customer-clusters.sunet.se/v1alpha1",
        "kind": "ManagedCluster",
        "metadata": {"name": data["cluster"]["slug"]},
        "spec": {
            "dns": {
                "apiHostname": data["inventory"]["apiHostname"],
                "argocdHostname": data["inventory"]["argocdHostname"],
                "apiAlias": "ignored-alias.example.org",
            },
            "profileRef": {"name": data["inventory"]["profileName"]},
            "openstack": {"projectName": data["project"]["name"]},
            "workerGroups": data["nodes"]["workers"] // 3,
        },
    }).encode()


@dataclass
class Repository:
    root: Path
    remote: Path
    env: dict[str, str]
    writers: int = 0
    calls: list[list[str]] = field(default_factory=list)

    def git(self, *args, cwd=None, input=None):
        return subprocess.run(
            ["git", *args], cwd=cwd or self.remote, env=self.env, input=input,
            check=True, capture_output=True, timeout=30,
        ).stdout

    def writer(self):
        self.writers += 1
        work = self.root / f"writer-{self.writers}"
        self.git("clone", "--quiet", str(self.remote), str(work), cwd=self.root)
        return work

    def change(self, files):
        work = self.writer()
        for name, content in files.items():
            path = work / name
            if content is None:
                path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content.encode() if isinstance(content, str) else content)
        self.git("add", "--all", cwd=work)
        self.git("commit", "--quiet", "-m", "Reviewed source change", cwd=work)
        self.git("push", "--quiet", "origin", "HEAD:refs/heads/main", cwd=work)
        return self.head

    def mode(self, path, mode, content=b"/outside/checkout"):
        work = self.writer()
        self.git("rm", "--cached", "--ignore-unmatch", "-r", "--", path, cwd=work)
        identity = (
            self.head.encode() if mode == "160000"
            else self.git("hash-object", "-w", "--stdin", input=content, cwd=work).strip()
        )
        self.git("update-index", "--add", "--cacheinfo", f"{mode},{identity.decode()},{path}",
                 cwd=work)
        self.git("commit", "--quiet", "-m", "Change target mode", cwd=work)
        self.git("push", "--quiet", "origin", "HEAD:refs/heads/main", cwd=work)

    @property
    def head(self):
        return self.git("rev-parse", "refs/heads/main").decode().strip()

    def read(self, path):
        return self.git("show", f"refs/heads/main:{path}")

    def paths(self):
        return set(self.git("ls-tree", "-r", "--name-only", "main").decode().splitlines())

    def changed_paths(self):
        return set(self.git("diff-tree", "--no-commit-id", "--name-only", "-r", "main")
                   .decode().splitlines())


@pytest.fixture
def data(provisioning_input):
    return deepcopy(provisioning_input.data)


@pytest.fixture
def hosts():
    return render_inventory({
        "jumphost": {"floating_ip": "192.0.2.1"},
        "controllers": [{"name": f"controller-{i}", "ip": f"10.0.0.{10 + i}"}
                        for i in range(1, 4)],
        "workers": [{"name": f"worker-{i}", "ip": f"10.0.0.{20 + i}"} for i in range(1, 7)],
        "api_vip": "10.0.0.5", "ingress_vip": "10.0.0.6",
        "api_floating_ip": "192.0.2.2", "ingress_floating_ip": "192.0.2.3",
    })


@pytest.fixture
def repository(tmp_path, monkeypatch, data):
    env = {**publication_git._git_env(URL, "fixture", "fixture-token"),
           "GIT_ALLOW_PROTOCOL": "file"}
    repo = Repository(tmp_path, tmp_path / "remote.git", env)
    repo.git("init", "--bare", "--initial-branch=main", str(repo.remote), cwd=tmp_path)
    repo.change({
        SOURCE: declaration(data),
        "kustomization.yaml": "resources: [clusters/example]\n",
        "inventory/environments/test.yml": "# Reviewed environment\nall: {}\n",
        "inventory/profiles/standard-v1.yml": "# Reviewed profile\nall: {}\n",
        ".gitmodules": '[submodule "untouched"]\n path = vendor/untouched\n'
                       ' url = https://invalid.example/never-contact.git\n',
    })

    def transport(url):
        assert url == URL
        return str(repo.remote), "file"

    real_run = publication_git._run_git

    def recorded(args, cwd, env, **kwargs):
        repo.calls.append(args)
        return real_run(args, cwd, env, **kwargs)

    monkeypatch.setattr(publication_git, "_transport", transport)
    monkeypatch.setattr(publication_git, "_run_git", recorded)
    monkeypatch.setattr(publication.time, "sleep", lambda _: None)
    return repo


def publish(data, hosts, **kwargs):
    return publish_inventory(
        data["git"], data["cluster"]["slug"], hosts, TOKEN,
        cluster_policy=render_cluster_policy(data), provisioning_data=data, **kwargs,
    )


def assert_no_publication_writes(repository):
    assert not any(args[0] in {"hash-object", "update-index", "commit", "push"}
                   for args in repository.calls)


def test_first_publication_is_one_two_file_commit(repository, data, hosts):
    before = repository.head
    untouched = {path: repository.read(path) for path in repository.paths()}
    path, commit = publish(data, hosts)
    assert path == HOSTS
    assert commit == repository.head
    assert repository.git("rev-parse", "main^").decode().strip() == before
    assert repository.changed_paths() == {HOSTS, POLICY}
    assert repository.read(HOSTS) == hosts.encode()
    assert repository.read(POLICY) == render_cluster_policy(data).encode()
    assert {path: repository.read(path) for path in untouched} == untouched
    assert sum(args[0] == "commit" for args in repository.calls) == 1
    assert sum(args[0] == "push" for args in repository.calls) == 1
    assert repository.calls[-1][0] == "ls-remote"


def test_second_cluster_preserves_first_cluster(repository, data, hosts):
    publish(data, hosts)
    first = {path: repository.read(path) for path in (HOSTS, POLICY)}
    second = deepcopy(data)
    second["cluster"] = {"slug": "second", "uid": "different-uid"}
    second["inventory"].update(apiHostname="api.second.example.org",
                               argocdHostname="argocd.second.example.org")
    repository.change({"clusters/second/cluster.yaml": declaration(second)})
    path, commit = publish(second, hosts)
    assert commit == repository.head
    assert path == "clusters/second/generated/ansible/hosts.yml"
    assert repository.changed_paths() == {path, "inventory/clusters/second.yml"}
    assert {path: repository.read(path) for path in first} == first


def test_unrelated_submodules_and_attributes_are_preserved_without_checkout(
    repository, data, hosts, monkeypatch,
):
    repository.mode("vendor/untouched", "160000")
    repository.change({".gitattributes": "*.yml text eol=crlf filter=untrusted\n"})
    original_link = repository.git("ls-tree", "main", "--", "vendor/untouched")
    originals = {path: repository.read(path) for path in repository.paths()
                 if path != "vendor/untouched"}
    run = publication_git._run_git

    def no_worktree(args, cwd, env, **kwargs):
        if args[0] in {"commit", "push"}:
            assert not (cwd / "vendor/untouched").exists()
            assert not (cwd / ".gitmodules").exists()
            assert not (cwd / ".gitattributes").exists()
        return run(args, cwd, env, **kwargs)

    monkeypatch.setattr(publication_git, "_run_git", no_worktree)
    publish(data, hosts)
    assert repository.git("ls-tree", "main", "--", "vendor/untouched") == original_link
    assert {path: repository.read(path) for path in originals} == originals
    assert repository.read(HOSTS) == hosts.encode()
    assert repository.read(POLICY) == render_cluster_policy(data).encode()
    assert repository.changed_paths() == {HOSTS, POLICY}
    assert not any(args[0] in {"checkout", "submodule"} for args in repository.calls)


def test_noop_confirms_remote_head_without_commit_or_push(repository, data, hosts):
    publish(data, hosts)
    before = repository.head
    repository.calls.clear()
    assert publish(data, hosts) == (HOSTS, before)
    assert repository.head == before
    assert_no_publication_writes(repository)
    assert repository.calls[-1][:3] == ["ls-remote", "--refs", "--exit-code"]


@pytest.mark.parametrize("key,value", [
    ("apiHostname", "api.new.example.org"),
    ("argocdHostname", "argocd.new.example.org"),
    ("profileName", "standard-v2"),
    ("nodeInterface", "ens4"),
    ("pythonInterpreter", "/usr/local/bin/python3.13"),
])
def test_metadata_updates_only_owned_policy(repository, data, hosts, key, value):
    publish(data, hosts)
    data["inventory"][key] = value
    if key in {"apiHostname", "argocdHostname", "profileName"}:
        repository.change({SOURCE: declaration(data)})
    _, commit = publish(data, hosts)
    assert commit == repository.head
    assert repository.changed_paths() == {POLICY}
    assert repository.read(HOSTS) == hosts.encode()
    assert repository.read(POLICY) == render_cluster_policy(data).encode()


def test_hosts_update_supports_original_safedumper_anchors(repository, data, hosts):
    assert "&id001" in hosts and "*id001" in hosts
    publish(data, hosts)
    updated = hosts.replace("10.0.0.21", "10.0.0.41")
    publish(data, updated)
    assert repository.changed_paths() == {HOSTS}
    assert repository.read(HOSTS) == updated.encode()


@pytest.mark.parametrize("comment", [
    "# Carefully reviewed by a human",
    "# Reviewed by the service owner",
    "# Values copied from generated hosts inventory",
    "# Reviewed by customer-cluster-operator maintainers",
    "# See owner: service team for approval; formatVersion and ManagedClusterUID are documented",
])
def test_matching_manual_policy_keeps_exact_bytes_and_is_not_adopted(
    repository, data, hosts, comment,
):
    document = yaml.safe_load(render_cluster_policy(data))
    manual = (f"{comment}\n---\n"
              + yaml.safe_dump(document, sort_keys=True, default_style='"')).replace("\n", "\r\n")
    repository.change({POLICY: manual})
    publish(data, hosts)
    assert repository.changed_paths() == {HOSTS}
    assert repository.read(POLICY) == manual.encode()
    assert b"# ManagedClusterUID:" not in repository.read(POLICY)
    repository.calls.clear()
    publish(data, hosts)
    assert_no_publication_writes(repository)


def test_matching_manual_yaml_merge_remains_manual(repository, data, hosts):
    policy_vars = yaml.safe_load(render_cluster_policy(data))["all"]["vars"]
    manual = yaml.safe_dump({"all": {"<<": {"vars": policy_vars}}}).replace(
        "'<<':", "<<: &reviewed")
    assert yaml.safe_load(manual) == yaml.safe_load(render_cluster_policy(data))
    repository.change({POLICY: manual})
    publish(data, hosts)
    assert repository.read(POLICY) == manual.encode()
    assert repository.changed_paths() == {HOSTS}


@pytest.mark.parametrize("alteration", ["value", "extra-variable", "extra-group", "empty"])
def test_manual_policy_conflicts_leave_both_remote_files_untouched(
    repository, data, hosts, alteration,
):
    publish(data, hosts)
    document = yaml.safe_load(render_cluster_policy(data))
    if alteration == "value":
        document["all"]["vars"]["customer_cluster_node_interface"] = "ens9"
    elif alteration == "extra-variable":
        document["all"]["vars"]["private_value"] = "must-not-be-logged"
    elif alteration == "extra-group":
        document["manual_group"] = {}
    else:
        document = {}
    repository.change({POLICY: yaml.safe_dump(document)})
    before = repository.head
    original = {path: repository.read(path) for path in (HOSTS, POLICY)}
    repository.calls.clear()
    with pytest.raises(InventoryConflict) as error:
        publish(data, hosts.replace("10.0.0.21", "10.0.0.41"))
    assert "must-not-be-logged" not in str(error.value)
    assert repository.head == before
    assert {path: repository.read(path) for path in original} == original
    assert_no_publication_writes(repository)


@pytest.mark.parametrize("alteration", [
    "other-uid", "missing-uid", "duplicate-uid", "bad-format", "other-owner", "partial-marker",
    "inline-marker",
])
def test_ambiguous_or_foreign_ownership_rejected_even_for_identical_policy(
    repository, data, hosts, alteration,
):
    policy = render_cluster_policy(data)
    if alteration == "other-uid":
        policy = policy.replace(data["cluster"]["uid"], "another-cluster-uid")
    elif alteration == "missing-uid":
        policy = policy.replace(f"# ManagedClusterUID: {data['cluster']['uid']}\n", "")
    elif alteration == "duplicate-uid":
        policy += f"# ManagedClusterUID: {data['cluster']['uid']}\n"
    elif alteration == "bad-format":
        policy = policy.replace("formatVersion: 1", "formatVersion: 2")
    elif alteration == "other-owner":
        policy = policy.replace("# owner: customer-cluster-operator", "# owner: someone-else")
    elif alteration == "inline-marker":
        policy = policy.replace("all:\n", "all: # ManagedClusterUID: another-uid\n")
    else:
        policy = "# ManagedClusterUID: " + data["cluster"]["uid"] + "\n" + yaml.safe_dump(
            yaml.safe_load(policy))
    repository.change({POLICY: policy})
    before = repository.head
    with pytest.raises(InventoryConflict, match="ownership"):
        publish(data, hosts)
    assert repository.head == before
    assert HOSTS not in repository.paths()
    assert_no_publication_writes(repository)


@pytest.mark.parametrize("marker", [
    "# GENERATED FILE:",
    "# GENERATED FILE: foreign-operator",
    "# owner: customer-cluster-operator",
    "# owner:",
    "# owner: customer-cluster-operator\n# owner: customer-cluster-operator",
    "# formatVersion: unsupported",
    "# ManagedClusterUID: another-uid",
    "#   OWNER : customer-cluster-operator",
    "# GENERATED FILE",
    "# owner",
    "# formatVersion",
    "# ManagedClusterUID",
])
def test_partial_or_malformed_ownership_markers_fail_closed(repository, data, hosts, marker):
    policy = marker + "\n" + yaml.safe_dump(yaml.safe_load(render_cluster_policy(data)))
    repository.change({POLICY: policy})
    before = repository.head
    with pytest.raises(InventoryConflict, match="ownership"):
        publish(data, hosts)
    assert repository.head == before
    assert repository.read(POLICY) == policy.encode()
    assert HOSTS not in repository.paths()
    assert_no_publication_writes(repository)


@pytest.mark.parametrize("content", [
    b"all: {}\nall: {}\n", b"---\nall: {}\n---\nall: {}\n",
    b"!!python/object/apply:os.system [do-not-execute]\n", b"\xff\xfe", b"[not, a, mapping]",
])
def test_unsafe_or_ambiguous_yaml_fails_closed(repository, data, hosts, content):
    repository.change({POLICY: content})
    before = repository.head
    with pytest.raises(InventoryConflict):
        publish(data, hosts)
    assert repository.head == before
    assert_no_publication_writes(repository)


@pytest.mark.parametrize("owned", [False, True])
def test_unmarked_or_ambiguous_hosts_are_not_overwritten(repository, data, hosts, owned):
    original = hosts + "# owner: another-operator\n" if owned else hosts.replace(HOSTS_MARKER, "")
    repository.change({HOSTS: original})
    before = repository.head
    with pytest.raises(InventoryConflict, match="operator-owned"):
        publish(data, hosts)
    assert repository.head == before
    assert POLICY not in repository.paths()
    assert_no_publication_writes(repository)


@pytest.mark.parametrize("path,mode", [
    ("clusters", "120000"), ("clusters/example", "120000"),
    ("clusters/example/generated", "120000"), ("clusters/example/generated/ansible", "120000"),
    (HOSTS, "120000"), ("inventory", "120000"), ("inventory/clusters", "120000"),
    (POLICY, "120000"), (SOURCE, "120000"),
    (HOSTS, "100755"), (POLICY, "100755"), (SOURCE, "100755"),
    ("clusters/example/generated", "160000"), ("inventory/clusters", "160000"),
    (HOSTS, "160000"), (POLICY, "160000"),
])
def test_symlinks_gitlinks_and_unsafe_modes_fail_before_writes(repository, data, hosts, path, mode):
    repository.mode(path, mode)
    before = repository.head
    with pytest.raises(InventoryConflict, match="unsafe Git mode"):
        publish(data, hosts)
    assert repository.head == before
    assert_no_publication_writes(repository)


@pytest.mark.parametrize("path,value", [
    (("apiVersion",), "another.example/v1"), (("kind",), "OtherResource"),
    (("metadata", "name"), "other-cluster"), (("metadata", "uid"), "other-uid"),
    (("metadata", "namespace"), "another-namespace"),
    (("metadata", "namespace"), None), (("metadata", "namespace"), ""),
    (("spec", "dns", "apiHostname"), "api.new.example.org"),
    (("spec", "dns", "argocdHostname"), "argocd.new.example.org"),
    (("spec", "dns"), {"apiAlias": "api.example.example.org"}),
    (("spec", "profileRef", "name"), "standard-v2"),
    (("spec", "openstack", "projectName"), "other-project"),
    (("spec", "workerGroups"), 3), (("spec", "workerGroups"), True),
    (("spec", "suspend"), True), (("spec", "suspend"), "false"),
    (("spec", "deletionPolicy"), "Delete"), (("spec", "deletionPolicy"), None),
    (("spec", "deletionPolicy"), "retain"),
])
def test_stale_declaration_is_a_conflict(repository, data, hosts, path, value):
    source = yaml.safe_load(declaration(data))
    cursor = source
    for part in path[:-1]:
        cursor = cursor[part]
    cursor[path[-1]] = value
    repository.change({SOURCE: yaml.safe_dump(source)})
    before = repository.head
    with pytest.raises(InventoryConflict, match="no longer matches"):
        publish(data, hosts)
    assert repository.head == before
    assert HOSTS not in repository.paths() and POLICY not in repository.paths()
    assert_no_publication_writes(repository)


def test_missing_declaration_is_a_conflict(repository, data, hosts):
    repository.change({SOURCE: None})
    before = repository.head
    with pytest.raises(InventoryConflict, match="missing"):
        publish(data, hosts)
    assert repository.head == before
    assert_no_publication_writes(repository)


@pytest.mark.parametrize("explicit_namespace", [False, True])
def test_source_accepts_matching_or_omitted_namespace_and_retain_policy(
    repository, data, hosts, explicit_namespace,
):
    source = yaml.safe_load(declaration(data))
    if explicit_namespace:
        source["metadata"]["namespace"] = data["openstack"]["credentialsSecret"]["namespace"]
    source["spec"]["deletionPolicy"] = "Retain"
    repository.change({SOURCE: yaml.safe_dump(source)})
    assert publish(data, hosts)[1] == repository.head
    assert repository.changed_paths() == {HOSTS, POLICY}


@pytest.mark.parametrize("openstack", [
    None, {}, {"credentialsSecret": None}, {"credentialsSecret": {}},
    {"credentialsSecret": {"namespace": None}},
    {"credentialsSecret": {"namespace": 42}},
    {"credentialsSecret": {"namespace": []}},
    {"credentialsSecret": {"namespace": ""}},
    {"credentialsSecret": {"namespace": "MixedCase"}},
    {"credentialsSecret": {"namespace": "invalid.namespace"}},
    {"credentialsSecret": {"namespace": " namespace "}},
    {"credentialsSecret": {"namespace": "x" * 64}},
])
def test_expected_namespace_must_be_a_valid_dns_label(repository, data, hosts, openstack):
    data["openstack"] = openstack
    before = repository.head
    with pytest.raises(ValidationError, match="namespace"):
        publish(data, hosts)
    assert repository.head == before
    assert HOSTS not in repository.paths() and POLICY not in repository.paths()
    assert_no_publication_writes(repository)


def test_namespace_validation_does_not_expose_credential_values(
    repository, data, hosts, caplog, capfd,
):
    values = {
        "namespace": "private.invalid.namespace",
        "name": "private-credential-name",
        "key": "private-credential-key",
        "token": "private-credential-value",
    }
    data["openstack"]["credentialsSecret"] = values
    with pytest.raises(ValidationError, match="namespace") as error:
        publish(data, hosts)
    captured = capfd.readouterr()
    public_text = "".join(traceback.format_exception(error.value))
    public_text += caplog.text + captured.out + captured.err
    assert all(value not in public_text for value in values.values())
    assert_no_publication_writes(repository)


def test_omitted_profile_ref_uses_standard_v1(repository, data, hosts):
    source = yaml.safe_load(declaration(data))
    source["spec"].pop("profileRef")
    repository.change({SOURCE: yaml.safe_dump(source)})
    assert publish(data, hosts)[1] == repository.head


def intercept_once(monkeypatch, command, callback):
    run = publication_git._run_git
    fired = False

    def intercept(args, cwd, env, **kwargs):
        nonlocal fired
        if args[0] == command and not fired:
            fired = True
            callback()
        return run(args, cwd, env, **kwargs)

    monkeypatch.setattr(publication_git, "_run_git", intercept)


def test_concurrent_writer_retries_fresh_clone_and_preserves_unrelated_files(
    repository, data, hosts, monkeypatch,
):
    intercept_once(monkeypatch, "push", lambda: repository.change({"human-note.txt": "keep me\n"}))
    _, commit = publish(data, hosts)
    assert commit == repository.head
    assert repository.read("human-note.txt") == b"keep me\n"
    assert repository.changed_paths() == {HOSTS, POLICY}
    assert sum(args[0] == "push" for args in repository.calls) == 2
    assert sum(args[0] == "clone" for args in repository.calls) == 4
    assert not any("force" in arg or arg.startswith("+")
                   for args in repository.calls for arg in args)


def test_concurrent_source_change_prevents_stale_retry(repository, data, hosts, monkeypatch):
    changed = deepcopy(data)
    changed["inventory"]["apiHostname"] = "api.changed.example.org"
    intercept_once(monkeypatch, "push", lambda: repository.change({SOURCE: declaration(changed)}))
    with pytest.raises(InventoryConflict, match="no longer matches"):
        publish(data, hosts)
    assert repository.read(SOURCE) == declaration(changed)
    assert HOSTS not in repository.paths() and POLICY not in repository.paths()
    assert sum(args[0] == "push" for args in repository.calls) == 1


@pytest.mark.parametrize("stale", [False, True])
def test_noop_checks_current_remote_instead_of_returning_old_head(
    repository, data, hosts, monkeypatch, stale,
):
    publish(data, hosts)
    before = repository.head
    repository.calls.clear()
    changed = deepcopy(data)
    changed["inventory"]["apiHostname"] = "api.changed.example.org"
    files = {SOURCE: declaration(changed)} if stale else {"human-note.txt": "new HEAD\n"}
    intercept_once(monkeypatch, "ls-remote", lambda: repository.change(files))
    if stale:
        with pytest.raises(InventoryConflict):
            publish(data, hosts)
    else:
        assert publish(data, hosts)[1] == repository.head
    assert before != repository.head
    assert_no_publication_writes(repository)


def test_rejected_push_cannot_report_success(repository, data, hosts):
    before = repository.head
    repository.git("config", "receive.hideRefs", "refs/heads/main")
    with pytest.raises(RuntimeError, match="confirm inventory publication"):
        publish(data, hosts, retries=2)
    assert repository.head == before
    assert HOSTS not in repository.paths() and POLICY not in repository.paths()
    assert sum(args[0] == "push" for args in repository.calls) == 2


@pytest.mark.parametrize("advance", [False, True])
def test_lost_push_response_is_recovered_by_confirming_contents(
    repository, data, hosts, monkeypatch, advance,
):
    run = publication_git._run_git

    def lost_response(args, cwd, env, **kwargs):
        result = run(args, cwd, env, **kwargs)
        if args[0] == "push":
            if advance:
                repository.change({"human-note.txt": "after accepted push\n"})
            raise publication_git.GitError("Git publication command failed")
        return result

    monkeypatch.setattr(publication_git, "_run_git", lost_response)
    assert publish(data, hosts, retries=1)[1] == repository.head
    assert repository.read(HOSTS) == hosts.encode()
    assert repository.read(POLICY) == render_cluster_policy(data).encode()
    assert sum(args[0] == "clone" for args in repository.calls) == 2


@pytest.mark.parametrize("path", [HOSTS, POLICY])
def test_lost_response_must_confirm_both_paths_before_success(
    repository, data, hosts, monkeypatch, path,
):
    run = publication_git._run_git

    def lost_response(args, cwd, env, **kwargs):
        result = run(args, cwd, env, **kwargs)
        if args[0] == "push":
            content = repository.read(path)
            changed = content.replace(b"10.0.0.21", b"10.0.0.41") if path == HOSTS else (
                content.replace(b"ens3", b"ens9"))
            repository.change({path: changed})
            raise publication_git.GitError("Git publication command failed")
        return result

    monkeypatch.setattr(publication_git, "_run_git", lost_response)
    with pytest.raises(RuntimeError, match="confirm"):
        publish(data, hosts, retries=1)
    assert repository.changed_paths() == {path}


def test_success_response_without_remote_content_is_not_success(
    repository, data, hosts, monkeypatch,
):
    run = publication_git._run_git

    def ignored_push(args, cwd, env, **kwargs):
        return b"" if args[0] == "push" else run(args, cwd, env, **kwargs)

    monkeypatch.setattr(publication_git, "_run_git", ignored_push)
    before = repository.head
    with pytest.raises(RuntimeError, match="confirm"):
        publish(data, hosts, retries=1)
    assert repository.head == before


@pytest.mark.parametrize("branch", [
    "--upload-pack=malicious", "../main", "feature/../main", "main.lock", "main:other",
    "main\nother", "main//other", "main/", "@{-1}", "main/.private",
])
def test_malicious_branches_are_rejected_before_git(repository, data, hosts, branch):
    data["git"]["branch"] = branch
    with pytest.raises(ValidationError, match="branch"):
        publish(data, hosts)
    assert not repository.calls


@pytest.mark.parametrize("url", [
    "http://git.example.org/repo", "file:///local/repo", "/local/repo", "ssh://git.example.org/repo",
    "https://bot:secret@git.example.org/repo", "https://git.example.org/repo?token=secret",
    "https://git.example.org/repo#fragment", "https://git.example.org/../repo",
    "https://git.example.org/%2e%2e/repo", "https://git.example.org\\other/repo",
    "https://git.example.org/repo\n--option", "https://git.example.org:bad/repo",
])
def test_malicious_urls_are_rejected_before_git(repository, data, hosts, url):
    data["git"]["repoUrl"] = url
    with pytest.raises(ValidationError, match="safe HTTPS"):
        publish(data, hosts)
    assert not repository.calls


@pytest.mark.parametrize("slug", ["../other", "/local/other", "example/../other", "bad\nname"])
def test_traversal_slugs_are_rejected_before_git(repository, data, hosts, slug):
    data["cluster"]["slug"] = slug
    with pytest.raises(ValidationError, match="DNS label"):
        publish(data, hosts)
    assert not repository.calls


def test_empty_token_and_forged_policy_fail_before_git(repository, data, hosts):
    with pytest.raises(ValidationError, match="empty"):
        publish_inventory(
            data["git"], "example", hosts, "", cluster_policy=render_cluster_policy(data),
            provisioning_data=data,
        )
    with pytest.raises(ValidationError, match="does not match"):
        publish_inventory(data["git"], "example", hosts, TOKEN, cluster_policy="all: {}",
                          provisioning_data=data)
    assert not repository.calls


def test_auth_is_scoped_to_child_env_without_ambient_git_config(monkeypatch):
    ambient = {
        "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath",
        "GIT_CONFIG_VALUE_0": "/ambient-hooks", "GIT_TRACE": "1",
        "GIT_TRACE_CURL": "1", "GIT_DIR": "/ambient-repo", "GIT_EXEC_PATH": "/ambient-git",
        "GIT_CONFIG_PARAMETERS": "ambient", "GIT_SSH_COMMAND": "ambient",
        "HOME": "/ambient-home", "HTTPS_PROXY": "https://proxy.invalid",
    }
    for key, value in ambient.items():
        monkeypatch.setenv(key, value)
    env = publication_git._git_env(URL, "bot", TOKEN)
    for key, value in ambient.items():
        assert env.get(key) != value
        assert os.environ[key] == value
    settings = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
                for i in range(int(env["GIT_CONFIG_COUNT"]))}
    assert settings[f"http.{URL}.extraHeader"] == (
        "Authorization: Basic " + base64.b64encode(f"bot:{TOKEN}".encode()).decode())
    assert settings["credential.helper"] == ""
    assert settings["http.followRedirects"] == "false"
    assert settings["core.hooksPath"] == os.devnull
    assert settings["submodule.recurse"] == "false"
    assert env["GIT_CONFIG_GLOBAL"] == env["GIT_CONFIG_SYSTEM"] == os.devnull
    assert env["GIT_ALLOW_PROTOCOL"] == "https"


def test_no_tokens_in_arguments_git_config_artifacts_or_logs(
    repository, data, hosts, monkeypatch, caplog, capfd,
):
    run = publication_git._run_git
    encoded = base64.b64encode(f"{data['git']['username']}:{TOKEN}".encode()).decode()

    def inspect(args, cwd, env, **kwargs):
        assert TOKEN not in repr(args) and encoded not in repr(args)
        if cwd is not None and (cwd / ".git/config").exists():
            config = (cwd / ".git/config").read_text()
            assert TOKEN not in config and encoded not in config
        return run(args, cwd, env, **kwargs)

    monkeypatch.setattr(publication_git, "_run_git", inspect)
    publish(data, hosts)
    captured = capfd.readouterr()
    public_text = caplog.text + captured.out + captured.err
    public_text += repository.git("log", "--format=fuller").decode()
    public_text += repository.read(HOSTS).decode() + repository.read(POLICY).decode()
    assert TOKEN not in public_text and encoded not in public_text


@pytest.mark.parametrize("timeout", [False, True])
def test_git_failures_never_expose_raw_stdout_stderr_or_exception_chains(
    monkeypatch, caplog, capfd, timeout,
):
    def fail(*args, **kwargs):
        assert kwargs["capture_output"] is True
        if timeout:
            raise subprocess.TimeoutExpired("git", 300, output=TOKEN, stderr=TOKEN)
        raise subprocess.CalledProcessError(1, "git", output=TOKEN, stderr=TOKEN)

    monkeypatch.setattr(publication_git.subprocess, "run", fail)
    with pytest.raises(publication_git.GitError) as error:
        publication_git._run_git(["push"], None, {})
    public_text = "".join(traceback.format_exception(error.value)) + caplog.text
    captured = capfd.readouterr()
    assert TOKEN not in public_text + captured.out + captured.err
