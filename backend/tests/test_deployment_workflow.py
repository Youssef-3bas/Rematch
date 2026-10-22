from pathlib import Path


WORKFLOW = (
    Path(__file__).resolve().parents[2]
    / ".github"
    / "workflows"
    / "rematch-api-AutoDeployTrigger-a3fce886-ff6a-4c0e-8c2f-a81b4b50f027.yml"
).read_text(encoding="utf-8")


def test_acr_push_uses_oidc_backed_azure_cli_docker_authentication():
    assert "uses: azure/login@v2" in WORKFLOW
    assert "permissions:\n      id-token: write" in WORKFLOW
    assert "az acr login --name rematchbackend --only-show-errors" in WORKFLOW
    assert WORKFLOW.index("uses: azure/login@v2") < WORKFLOW.index(
        "az acr login --name rematchbackend"
    ) < WORKFLOW.index('docker push "$IMAGE"')
    assert "docker build --file backend/Dockerfile --tag \"$IMAGE\" backend" in WORKFLOW
    assert 'docker push "$IMAGE"' in WORKFLOW
    assert "docker/login-action" not in WORKFLOW
    assert "REGISTRY_USERNAME" not in WORKFLOW
    assert "REGISTRY_PASSWORD" not in WORKFLOW


def test_workflow_uses_configured_registry_and_commit_tag_for_existing_app():
    assert 'printf \'REGISTRY_URL=%s\\n\' "$REGISTRY_URL" >> "$GITHUB_ENV"' in WORKFLOW
    assert 'printf \'IMAGE=%s/rematch-api:%s\\n\' "$REGISTRY_URL" "$EXPECTED_SHA" >> "$GITHUB_ENV"' in WORKFLOW
    assert "${{ vars.REMATCHAPI_REGISTRY_URL }}/rematch-api:${{ github.sha }}" not in WORKFLOW
    assert 'if [[ "$REGISTRY_URL" != "rematchbackend.azurecr.io" ]]' in WORKFLOW
    assert "--name rematch-api" in WORKFLOW
    assert "--resource-group Rematch-RG" in WORKFLOW
    assert '--image "$IMAGE"' in WORKFLOW
    assert "az acr repository show-tags" in WORKFLOW


def test_workflow_configures_managed_identity_before_verifying_pull_role():
    assert "az containerapp identity assign" in WORKFLOW
    assert "--system-assigned" in WORKFLOW
    assert "az containerapp registry set" in WORKFLOW
    assert "--identity system" in WORKFLOW
    assert "verify_aca_deployment.py verify-registry" in WORKFLOW
    assert "verify_aca_deployment.py verify-role" in WORKFLOW
    assert WORKFLOW.index('docker push "$IMAGE"') < WORKFLOW.index(
        "az containerapp identity assign"
    ) < WORKFLOW.index("az containerapp registry set") < WORKFLOW.index(
        "verify_aca_deployment.py verify-role"
    )
    assert 'if [[ -z "$registry_identity" && -z "$registry_password_ref" ]]' not in WORKFLOW


def test_workflow_preserves_live_settings_and_verifies_revision_and_public_auth():
    assert "verify_aca_deployment.py template-patch" in WORKFLOW
    assert "az rest --method patch" in WORKFLOW
    assert 'https://management.azure.com${app_id}?api-version=2025-07-01' in WORKFLOW
    assert "Content-Type=application/merge-patch+json" in WORKFLOW
    assert "az containerapp revision show" in WORKFLOW
    assert "verify_aca_deployment.py verify-revision" in WORKFLOW
    assert '"https://${fqdn}/openapi.json"' in WORKFLOW
    assert '"https://${fqdn}/api/v1/health"' in WORKFLOW
    assert "HTTP 401 with a Bearer challenge" in WORKFLOW
