"""Fail when the backend image and Container Apps ports drift apart."""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

EXPECTED_PORT = 8000
EXPECTED_PROBES = {
    "Startup": "/health/startup",
    "Liveness": "/health/live",
    "Readiness": "/health/ready",
}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _docker_settings(dockerfile: str) -> tuple[int, set[int], str]:
    lines = dockerfile.splitlines()
    logical_lines: list[str] = []
    pending = ""
    for line in lines:
        current = f"{pending}{line.strip()}" if pending else line.strip()
        if current.endswith("\\"):
            pending = current[:-1] + " "
        else:
            logical_lines.append(current)
            pending = ""
    _require(not pending, "Dockerfile ends in an unfinished continuation.")

    env: dict[str, str] = {}
    exposed: set[int] = set()
    command = ""
    for line in logical_lines:
        if line.startswith("ENV "):
            for item in shlex.split(line[4:]):
                if "=" in item:
                    key, value = item.split("=", 1)
                    env[key] = value
        elif line.startswith("EXPOSE "):
            for value in line[7:].split():
                port = value.split("/", 1)[0]
                if port.isdigit():
                    exposed.add(int(port))
        elif line.startswith("CMD "):
            try:
                parsed = json.loads(line[4:])
            except json.JSONDecodeError as exc:
                raise ValueError("Dockerfile CMD must use valid JSON form.") from exc
            _require(
                isinstance(parsed, list) and len(parsed) >= 3,
                "Dockerfile CMD must launch the configured server.",
            )
            command = " ".join(str(part) for part in parsed)

    _require(env.get("PORT", "").isdigit(), "Dockerfile must set numeric ENV PORT.")
    default_port_match = re.search(r"--port\s+\$\{PORT:-([0-9]+)\}", command)
    _require(default_port_match is not None, "Uvicorn must use --port ${PORT:-<default>}.")
    default_port = int(default_port_match.group(1))
    docker_port = int(env["PORT"])
    _require(docker_port == default_port, "Docker ENV PORT and Uvicorn default differ.")
    host_match = re.search(r"--host\s+(\S+)", command)
    _require(host_match is not None and host_match.group(1) == "0.0.0.0", "Uvicorn must bind to 0.0.0.0.")
    return docker_port, exposed, command


def validate_container_ports(dockerfile: str, manifest: str) -> None:
    docker_port, exposed_ports, _command = _docker_settings(dockerfile)
    _require(docker_port == EXPECTED_PORT, f"Backend effective default port must be {EXPECTED_PORT}.")
    _require(exposed_ports == {docker_port}, f"Dockerfile EXPOSE must be {docker_port}.")

    ingress_match = re.search(r"(?m)^      targetPort:\s*(\d+)\s*$", manifest)
    _require(ingress_match is not None, "Container Apps ingress targetPort is missing.")
    target_port = int(ingress_match.group(1))
    _require(target_port == docker_port, "Container Apps ingress targetPort does not match the effective app port.")

    port_env_matches = re.findall(
        r"(?ms)^          - name: PORT\s*\n\s{12}value:\s*['\"]?(\d+)['\"]?\s*$",
        manifest,
    )
    _require(port_env_matches == [str(docker_port)], "Container Apps must set exactly one matching PORT environment value.")

    probe_matches = list(
        re.finditer(r"(?m)^          - type: (Startup|Liveness|Readiness)\s*$", manifest)
    )
    _require(
        [match.group(1) for match in probe_matches] == list(EXPECTED_PROBES),
        "Container Apps must define Startup, Liveness, and Readiness probes exactly once.",
    )
    for index, match in enumerate(probe_matches):
        end = probe_matches[index + 1].start() if index + 1 < len(probe_matches) else len(manifest)
        block = manifest[match.start() : end]
        port_match = re.search(r"(?m)^\s+port:\s*(\d+)\s*$", block)
        path_match = re.search(r"(?m)^\s+path:\s*(\S+)\s*$", block)
        probe_name = match.group(1)
        _require(port_match is not None, f"{probe_name} probe HTTP port is missing.")
        _require(int(port_match.group(1)) == target_port, f"{probe_name} probe port does not match ingress targetPort.")
        _require(
            path_match is not None and path_match.group(1) == EXPECTED_PROBES[probe_name],
            f"{probe_name} probe path is incorrect.",
        )


def main() -> int:
    backend = Path(__file__).resolve().parents[1]
    dockerfile = (backend / "Dockerfile").read_text(encoding="utf-8")
    manifest = (backend / "containerapp.yaml").read_text(encoding="utf-8")
    try:
        validate_container_ports(dockerfile, manifest)
    except ValueError as exc:
        raise SystemExit(f"Container port verification failed: {exc}") from exc
    print("Container port verification passed: image, PORT, ingress, and all HTTP probes use 8000.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
