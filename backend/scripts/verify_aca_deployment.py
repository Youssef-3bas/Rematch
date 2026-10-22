"""Validate Azure Container Apps deployment data without exposing credentials."""

from __future__ import annotations

import argparse
import json
import re
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


_SENSITIVE_KEY = re.compile(
    r"token|secret|password|authorization|credential|cookie|api[_-]?key|\bsas\b|\bsig\b",
    re.IGNORECASE,
)
_BEARER_VALUE = re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/-]+=*")
_SENSITIVE_VALUE = re.compile(
    r"(?i)([\"']?[\w.-]*(?:access[_-]?token|bearer[_-]?token|password|secret|authorization|credential|api[_-]?key|client[_-]?secret|sas|sig)[\w.-]*[\"']?\s*[:=]\s*[\"']?)[^\s,;\"'<>]+"
)
_FAILURE_LOG_MARKER = re.compile(
    r"(?i)failed|failure|error|unhealthy|back.?off|denied|timed? out|timeout|probe|image.?pull|crash|exited|refused|not found|fatal|oom|killed"
)


def _sanitize_diagnostic_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if _SENSITIVE_KEY.search(str(key)) else _sanitize_diagnostic_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_diagnostic_value(item) for item in value]
    if isinstance(value, str):
        sanitized = _BEARER_VALUE.sub(r"\1[REDACTED]", value)
        sanitized = _SENSITIVE_VALUE.sub(r"\1[REDACTED]", sanitized)
        sanitized = " ".join("".join(ch for ch in sanitized if ch.isprintable()).split())
        return sanitized[:400]
    return value


def build_http_diagnostic(status_code: str, body: str, request_error: str) -> dict[str, Any]:
    """Build bounded, one-line HTTP diagnostics with credential redaction."""
    try:
        body_value: Any = json.loads(body) if body else None
    except json.JSONDecodeError:
        body_value = body or None
    return {
        "httpStatus": status_code or "000",
        "requestError": _sanitize_diagnostic_value(request_error) or None,
        "bodyPreview": _sanitize_diagnostic_value(body_value),
    }


def _revision_containers(document: Any) -> list[dict[str, Any]]:
    if not isinstance(document, dict):
        return []
    properties = _properties(document)
    template = properties.get("template")
    containers = template.get("containers") if isinstance(template, dict) else None
    if not isinstance(containers, list):
        return []
    return [item for item in containers if isinstance(item, dict)]


def _replica_items(document: Any) -> list[dict[str, Any]] | None:
    if isinstance(document, list):
        return [item for item in document if isinstance(item, dict)]
    if isinstance(document, dict) and isinstance(document.get("value"), list):
        return [item for item in document["value"] if isinstance(item, dict)]
    return None


def _diagnostic_log_lines(document: Any) -> list[str]:
    """Return only sanitized, relevant error lines from Azure log output."""
    messages: list[str] = []

    def visit(value: Any, field: str = "") -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key).casefold() in {"log", "log_s", "message", "reason", "error", "details", "exception", "output"}:
                    visit(item, str(key))
                elif isinstance(item, (dict, list)):
                    visit(item, field)
        elif isinstance(value, list):
            for item in value:
                visit(item, field)
        elif isinstance(value, str) and field:
            for line in value.splitlines():
                cleaned = _sanitize_diagnostic_value(line)
                if cleaned and _FAILURE_LOG_MARKER.search(cleaned):
                    messages.append(cleaned)

    visit(document)
    # Keep the report useful in GitHub annotations while bounding its size.
    return messages[-12:]


def build_revision_diagnostic(
    revision: Any,
    replicas: Any,
    system_logs: Any,
    console_logs: Any,
    expected_image: str,
    *,
    revision_query_error: str = "",
    replica_query_error: str = "",
    system_log_query_error: str = "",
    console_log_query_error: str = "",
) -> dict[str, Any]:
    """Build a bounded revision/replica failure report without exposing secrets."""
    properties = _properties(revision) if isinstance(revision, dict) else {}
    containers = _revision_containers(revision)
    replica_items = _replica_items(replicas)

    def sanitized_error(value: str) -> Any:
        return _sanitize_diagnostic_value(value) or None

    return {
        "revisionName": _sanitize_diagnostic_value(revision.get("name")) if isinstance(revision, dict) else None,
        "expectedImage": _sanitize_diagnostic_value(expected_image),
        "containers": [
            {
                "name": _sanitize_diagnostic_value(item.get("name")),
                "image": _sanitize_diagnostic_value(item.get("image")),
            }
            for item in containers
        ],
        "provisioningState": properties.get("provisioningState"),
        "runningState": properties.get("runningState"),
        "healthState": properties.get("healthState"),
        "runningStateDetails": _sanitize_diagnostic_value(properties.get("runningStateDetails")),
        "provisioningError": _sanitize_diagnostic_value(properties.get("provisioningError")),
        "replicaCount": len(replica_items) if replica_items is not None else None,
        "replicas": [
            {
                "name": _sanitize_diagnostic_value(item.get("name")),
                "runningState": _sanitize_diagnostic_value(
                    item.get("runningState") or (item.get("properties") or {}).get("runningState")
                    if isinstance(item.get("properties"), dict) or item.get("runningState")
                    else None
                ),
            }
            for item in (replica_items or [])[:10]
        ],
        "revisionQueryError": sanitized_error(revision_query_error),
        "replicaQueryError": sanitized_error(replica_query_error),
        "systemLogQueryError": sanitized_error(system_log_query_error),
        "consoleLogQueryError": sanitized_error(console_log_query_error),
        "systemFailureLogs": _diagnostic_log_lines(system_logs),
        "consoleFailureLogs": _diagnostic_log_lines(console_logs),
    }


def verify_health_response(document: Any, check: str) -> None:
    if document != {"status": "ok", "check": check}:
        raise DeploymentValidationError(f"The {check} health response did not match its expected contract.")


def verify_openapi_response(document: Any) -> None:
    if not isinstance(document, dict) or not isinstance(document.get("openapi"), str) or not isinstance(document.get("paths"), dict):
        raise DeploymentValidationError("The public OpenAPI response is not a valid OpenAPI document.")


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
    http_diagnostic = subparsers.add_parser("http-diagnostic")
    http_diagnostic.add_argument("status_code")
    http_diagnostic.add_argument("body")
    http_diagnostic.add_argument("request_error")
    revision_diagnostic = subparsers.add_parser("revision-diagnostic")
    revision_diagnostic.add_argument("revision")
    revision_diagnostic.add_argument("replicas")
    revision_diagnostic.add_argument("system_logs")
    revision_diagnostic.add_argument("console_logs")
    revision_diagnostic.add_argument("--image", required=True)
    revision_diagnostic.add_argument("--revision-error", default="")
    revision_diagnostic.add_argument("--replica-error", default="")
    revision_diagnostic.add_argument("--system-log-error", default="")
    revision_diagnostic.add_argument("--console-log-error", default="")
    health_response = subparsers.add_parser("verify-health-response")
    health_response.add_argument("input")
    health_response.add_argument("--check", choices=("liveness", "readiness"), required=True)
    openapi_response = subparsers.add_parser("verify-openapi-response")
    openapi_response.add_argument("input")
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
        elif args.command == "http-diagnostic":
            body = Path(args.body).read_text(encoding="utf-8", errors="replace")
            request_error = Path(args.request_error).read_text(encoding="utf-8", errors="replace")
            print(json.dumps(build_http_diagnostic(args.status_code, body, request_error)))
        elif args.command == "revision-diagnostic":
            diagnostic = build_revision_diagnostic(
                _read(args.revision),
                _read(args.replicas),
                _read(args.system_logs),
                _read(args.console_logs),
                args.image,
                revision_query_error=args.revision_error,
                replica_query_error=args.replica_error,
                system_log_query_error=args.system_log_error,
                console_log_query_error=args.console_log_error,
            )
            print(json.dumps(diagnostic))
        elif args.command == "verify-health-response":
            verify_health_response(_read(args.input), args.check)
        elif args.command == "verify-openapi-response":
            verify_openapi_response(_read(args.input))
    except (DeploymentValidationError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"Deployment validation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
