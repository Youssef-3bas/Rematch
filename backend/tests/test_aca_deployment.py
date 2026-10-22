from copy import deepcopy

import pytest

from scripts.verify_aca_deployment import (
    DeploymentValidationError,
    acr_pull_role,
    build_container_template_patch,
    verify_pull_role,
    verify_registry_auth,
    verify_revision,
)


def app_document(container_name="bootstrap-container-pre-deployment"):
    return {
        "location": "eastus",
        "identity": {"type": "SystemAssigned", "principalId": "test-principal"},
        "properties": {
            "configuration": {
                "ingress": {"targetPort": 8000},
                "registries": [
                    {"server": "rematchbackend.azurecr.io", "identity": "system"}
                ],
            },
            "template": {
                "containers": [
                    {
                        "name": container_name,
                        "image": "mcr.microsoft.com/k8se/quickstart:latest",
                        # Present in some `az containerapp show` responses, but
                        # unsupported by the Container Apps PATCH body schema.
                        "imageType": "ContainerImage",
                        "env": [{"name": "API_BEARER_TOKEN", "secretRef": "api-bearer-token"}],
                        "probes": [{"type": "Startup", "httpGet": {"path": "/health/startup", "port": 8000}}],
                        "resources": {"cpu": 0.5, "memory": "1Gi"},
                    }
                ],
                "scale": {"minReplicas": 0, "maxReplicas": 1},
            },
        },
    }


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("LegacyRegistryPermissions", "AcrPull"),
        ("rbac", "AcrPull"),
        ("AbacRepositoryPermissions", "Container Registry Repository Reader"),
    ],
)
def test_acr_role_uses_actual_mode_fields(mode, expected):
    assert acr_pull_role({"properties": {"roleAssignmentMode": mode}}) == expected


def test_unknown_or_missing_acr_mode_fails_closed():
    with pytest.raises(DeploymentValidationError, match="omitted roleAssignmentMode"):
        acr_pull_role({"id": "/registry"})
    with pytest.raises(DeploymentValidationError, match="Unsupported"):
        acr_pull_role({"roleAssignmentMode": "UnknownMode"})


def test_registry_auth_requires_system_identity_and_expected_registry():
    assert verify_registry_auth(app_document(), "rematchbackend.azurecr.io") == "test-principal"
    malformed = app_document()
    malformed["properties"]["configuration"]["registries"] = []
    with pytest.raises(DeploymentValidationError, match="Expected one registry"):
        verify_registry_auth(malformed, "rematchbackend.azurecr.io")


def test_registry_auth_rejects_password_fallback_or_non_system_identity():
    malformed = app_document()
    malformed["properties"]["configuration"]["registries"][0]["passwordSecretRef"] = "acr-password"
    with pytest.raises(DeploymentValidationError, match="password secret"):
        verify_registry_auth(malformed, "rematchbackend.azurecr.io")
    malformed["properties"]["configuration"]["registries"][0] = {
        "server": "rematchbackend.azurecr.io",
        "identity": "some-user-assigned-id",
    }
    with pytest.raises(DeploymentValidationError, match="not using its system identity"):
        verify_registry_auth(malformed, "rematchbackend.azurecr.io")


def test_pull_role_must_match_principal_and_role():
    verify_pull_role(
        [{"principalId": "test-principal", "roleDefinitionName": "AcrPull"}],
        "test-principal",
        "AcrPull",
    )
    with pytest.raises(DeploymentValidationError, match="missing AcrPull"):
        verify_pull_role(
            [{"principalId": "other", "roleDefinitionName": "AcrPull"}],
            "test-principal",
            "AcrPull",
        )


def test_bootstrap_template_patch_renames_only_container_and_preserves_live_settings():
    live = app_document()
    original = deepcopy(live["properties"]["template"]["containers"][0])
    original_configuration = deepcopy(live["properties"]["configuration"])
    original_identity = deepcopy(live["identity"])
    patch = build_container_template_patch(live, "rematchbackend.azurecr.io/rematch-api:abc123")
    patched = patch["properties"]["template"]["containers"][0]
    assert patch["location"] == "eastus"
    assert "identity" not in patch
    assert set(patch["properties"]) == {"template"}
    assert "configuration" not in patch["properties"]
    assert patched["name"] == "rematch-api"
    assert patched["image"] == "rematchbackend.azurecr.io/rematch-api:abc123"
    assert "imageType" not in patched
    assert patched["env"] == original["env"]
    assert patched["probes"] == original["probes"]
    assert patched["resources"] == original["resources"]
    assert set(patch["properties"]["template"]) == {"containers"}
    assert live["properties"]["configuration"] == original_configuration
    assert live["identity"] == original_identity
    assert live["properties"]["template"]["scale"] == {"minReplicas": 0, "maxReplicas": 1}


def test_template_patch_removes_image_type_from_every_submitted_container():
    live = app_document(container_name="rematch-api")
    live["properties"]["template"]["containers"].append(
        {"name": "sidecar", "image": "example/sidecar:stable", "imageType": "ContainerImage"}
    )

    patch = build_container_template_patch(
        live,
        "rematchbackend.azurecr.io/rematch-api:79d0f8033121a49943e49b4204b93c2b59f5618",
    )

    submitted = patch["properties"]["template"]["containers"]
    assert all("imageType" not in container for container in submitted)
    api_container = next(container for container in submitted if container["name"] == "rematch-api")
    assert api_container["image"] == (
        "rematchbackend.azurecr.io/rematch-api:79d0f8033121a49943e49b4204b93c2b59f5618"
    )
    assert api_container["env"] == live["properties"]["template"]["containers"][0]["env"]
    assert api_container["probes"] == live["properties"]["template"]["containers"][0]["probes"]
    assert api_container["resources"] == live["properties"]["template"]["containers"][0]["resources"]


def test_template_patch_refuses_wrong_port_or_ambiguous_containers():
    wrong_port = app_document()
    wrong_port["properties"]["configuration"]["ingress"]["targetPort"] = 80
    with pytest.raises(DeploymentValidationError, match="targetPort must be 8000"):
        build_container_template_patch(wrong_port, "image:sha")
    ambiguous = app_document()
    ambiguous["properties"]["template"]["containers"].append({"name": "sidecar", "image": "sidecar"})
    with pytest.raises(DeploymentValidationError, match="multi-container"):
        build_container_template_patch(ambiguous, "image:sha")


def test_revision_requires_exact_image_and_healthy_running_state():
    revision = {
        "properties": {
            "healthState": "Healthy",
            "runningState": "Running",
            "provisioningState": "Provisioned",
            "template": {
                "containers": [{"name": "rematch-api", "image": "registry/rematch-api:sha"}]
            },
        }
    }
    verify_revision(revision, "registry/rematch-api:sha")
    bad = deepcopy(revision)
    bad["properties"]["template"]["containers"][0]["image"] = "registry/rematch-api:old"
    with pytest.raises(DeploymentValidationError, match="does not use"):
        verify_revision(bad, "registry/rematch-api:sha")
    bad = deepcopy(revision)
    bad["properties"]["healthState"] = "Unhealthy"
    with pytest.raises(DeploymentValidationError, match="not Healthy"):
        verify_revision(bad, "registry/rematch-api:sha")
