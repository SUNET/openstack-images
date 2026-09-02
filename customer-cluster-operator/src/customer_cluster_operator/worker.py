"""OpenStack-only provisioning Job entry point."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from .constants import CLOUDS_MOUNT, SSH_MOUNT
from .errors import ValidationError
from .inventory import publish_inventory, render_inventory
from .openstack import Provisioner, read_public_keys, scoped_connection


def load_input(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"cannot load provisioning input: {exc}") from exc
    if not isinstance(value, dict) or value.get("schemaVersion") != 1:
        raise ValidationError("unsupported provisioning input schema")
    return value


def run(data: dict[str, Any], *, token: str, clouds_file: str = CLOUDS_MOUNT) -> dict[str, Any]:
    key = data["ssh"]["authorizedKeysConfigMap"]["key"]
    public_keys = read_public_keys(Path(SSH_MOUNT) / key)
    connection = scoped_connection(data, clouds_file)
    resources = Provisioner(connection, data, public_keys).provision()
    path, commit = publish_inventory(
        data["git"], data["cluster"]["slug"], render_inventory(resources), token
    )
    return {
        "cluster": data["cluster"]["slug"],
        "controllers": len(resources["controllers"]),
        "workers": len(resources["workers"]),
        "jumphostFloatingIp": resources["jumphost"]["floating_ip"],
        "inventoryPath": path,
        "inventoryCommit": commit,
    }


def write_termination_result(
    result: dict[str, Any], path: Path = Path("/dev/termination-log")
) -> None:
    path.write_text(json.dumps(result, sort_keys=True, separators=(",", ":")))


def main() -> None:
    try:
        data = load_input(
            Path(os.getenv("INPUT_FILE", "/var/run/customer-cluster/input/input.json"))
        )
        result = run(
            data,
            token=os.getenv("GIT_TOKEN", ""),
            clouds_file=os.getenv("OS_CLIENT_CONFIG_FILE", CLOUDS_MOUNT),
        )
        write_termination_result(result)
        print(json.dumps(result, sort_keys=True))
    except Exception as exc:
        print(f"Provisioning failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
