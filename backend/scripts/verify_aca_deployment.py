"""Validate Azure Container Apps deployment data without exposing credentials."""

from __future__ import annotations

import argparse
import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any


class DeploymentValidationError(ValueError):
    """Raised when live Azure configuration is unsafe to deploy against."""


def _properties(document: dict[str, Any]) -> dict[str, Any]:
    properties = document.get("properties")
    return properties if isinstance(properties, dict) else document


def _configuration(document: dict[str, Any]) -> dict[str, Any]:
    properties = _properties(document)
    configuration = properties.get("configuration")
    return configuration if isinstance(configuration, dict) else {}


def acr_pull_role(document: dict[str, Any]) -> str:
    """Return the correct pull role from the ACR's actual role-assignment mode."""
    properties = _properties(document)
    mode = document.get("roleAssignmentMode") or properties.get("roleAssignmentMode")
    if not isinstance(mode, str) or not mode.strip():
        raise DeploymentValidationError(
            "Azure CLI ACR output omitted roleAssignmentMode; cannot verify registry authorization mode."
        )
    normalized = mode.replace("_", "").replace("-", "").replace(" ", "").casefold()
    if normalized in {"rbac", "legacyregistrypermissions"}:
        return "AcrPull"
    if normalized in {"rbacabac", "abacrepositorypermissions"}:
        return "Container Registry Repository Reader"
    raise DeploymentValidationError(f"Unsupported ACR role-assignment mode: {mode}.")


def system_principal_id(document: dict[str, Any]) -> str:
    identity = document.get("identity")
    if not isinstance(identity, dict):
        raise DeploymentValidationError("Container App system-assigned identity is missing.")
    identity_types = {part.casefold() for part in str(identity.get("type", "")).split(",")}
    principal_id = identity.get("principalId")
    if "systemassigned" not in identity_types or not isinstance(principal_id, str) or not principal_id:
        raise DeploymentValidationError("Container App system-assigned identity has no principal ID.")
    return principal_id


def verify_registry_auth(document: dict[str, Any], server: str) -> str:
    """Require the named ACR entry to use the app's system identity, without a password."""
    principal_id = system_principal_id(document)
    registries = _configuration(document).get("registries")
    if not isinstance(registries, list):
        raise DeploymentValidationError("Container App registry configuration is missing or malformed.")
    expected = server.rstrip("/").casefold()
    entries = [
        entry
        for entry in registries
        if isinstance(entry, dict)
        and isinstance(entry.get("server"), str)
        and entry["server"].rstrip("/").casefold() == expected
    ]
    if len(entries) != 1:
        raise DeploymentValidationError(
            f"Expected one registry configuration for {server}; found {len(entries)}."
        )
    entry = entries[0]
    if str(entry.get("identity", "")).casefold() != "system":
        raise DeploymentValidationError("Container App registry authentication is not using its system identity.")
    if entry.get("passwordSecretRef"):
        raise DeploymentValidationError("Container App registry configuration still references a password secret.")
    return principal_id


def verify_pull_role(document: Any, principal_id: str, role_name: str) -> None:
    assignments = document.get("value", []) if isinstance(document, dict) else document
    if not isinstance(assignments, list):
        raise DeploymentValidationError("Azure role assignment response is malformed.")
    for assignment in assignments:
        if not isinstance(assignment, dict):
            continue
        assigned_principal = assignment.get("principalId") or assignment.get("principal_id")
        assigned_role = assignment.get("roleDefinitionName") or assignment.get("role_definition_name")
        if (
            isinstance(assigned_principal, str)
            and assigned_principal.casefold() == principal_id.casefold()
            and isinstance(assigned_role, str)
            and assigned_role.casefold() == role_name.casefold()
        ):
            return
    raise DeploymentValidationError(
        f"Container App identity is missing {role_name} on the existing registry; assign the role at the ACR scope."
    )


def _containers(document: dict[str, Any]) -> list[dict[str, Any]]:
    containers = _properties(document).get("template", {}).get("containers")
    if not isinstance(containers, list) or not containers or any(not isinstance(item, dict) for item in containers):
        raise DeploymentValidationError("Container App template has no valid app containers.")
    return containers


def build_container_template_patch(document: dict[str, Any], image: str) -> dict[str, Any]:
    """Patch only template containers, preserving live env, probes, resources, and app config."""
    if not isinstance(document.get("location"), str) or not document["location"]:
        raise DeploymentValidationError("Container App response did not contain its Azure location.")
    ingress = _configuration(document).get("ingress")
    if not isinstance(ingress, dict) or ingress.get("targetPort") != 8000:
        raise DeploymentValidationError("Existing Container App ingress targetPort must be 8000.")

    containers = deepcopy(_containers(document))
    # `az containerapp show` may include the read-only `imageType` field on
    # container objects. It is not accepted by the Container Apps PATCH
    # request schema, so omit it from every container in the outgoing template.
    for container in containers:
        container.pop("imageType", None)
    named = [container for container in containers if container.get("name") == "rematch-api"]
    if len(named) == 1:
        target = named[0]
    elif len(containers) == 1:
        target = containers[0]
    else:
        raise DeploymentValidationError(
            "Cannot safely identify the API container in a multi-container app; review the live template manually."
        )
    target["name"] = "rematch-api"
    target["image"] = image
    return {
        "location": document["location"],
        "properties": {"template": {"containers": containers}},
    }


def verify_revision(document: dict[str, Any], image: str) -> None:
    properties = _properties(document)
    containers = properties.get("template", {}).get("containers", [])
    matches = [item for item in containers if isinstance(item, dict) and item.get("name") == "rematch-api"]
    if len(matches) != 1 or matches[0].get("image") != image:
        raise DeploymentValidationError("Latest revision does not use the expected rematch-api SHA image.")
    if properties.get("healthState") != "Healthy":
        raise DeploymentValidationError("Latest revision is not Healthy yet.")
    if properties.get("runningState") != "Running":
        raise DeploymentValidationError("Latest revision is not Running yet.")
    if properties.get("provisioningState") != "Provisioned":
        raise DeploymentValidationError("Latest revision is not Provisioned yet.")


def _read(path: str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    acr = subparsers.add_parser("acr-role")
    acr.add_argument("input")
    registry = subparsers.add_parser("verify-registry")
    registry.add_argument("input")
    registry.add_argument("--server", required=True)
    role = subparsers.add_parser("verify-role")
    role.add_argument("input")
    role.add_argument("--principal-id", required=True)
    role.add_argument("--role-name", required=True)
    patch = subparsers.add_parser("template-patch")
    patch.add_argument("input")
    patch.add_argument("--image", required=True)
    patch.add_argument("--output", required=True)
    revision = subparsers.add_parser("verify-revision")
    revision.add_argument("input")
    revision.add_argument("--image", required=True)
    args = parser.parse_args()

    try:
        if args.command == "acr-role":
            print(acr_pull_role(_read(args.input)))
        elif args.command == "verify-registry":
            print(verify_registry_auth(_read(args.input), args.server))
        elif args.command == "verify-role":
            verify_pull_role(_read(args.input), args.principal_id, args.role_name)
        elif args.command == "template-patch":
            patch_doc = build_container_template_patch(_read(args.input), args.image)
            Path(args.output).write_text(json.dumps(patch_doc), encoding="utf-8")
        elif args.command == "verify-revision":
            verify_revision(_read(args.input), args.image)
    except (DeploymentValidationError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"Deployment validation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
