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
    assert 'docker build --file backend/Dockerfile --tag "$IMAGE" backend' in WORKFLOW
    assert 'docker push "$IMAGE"' in WORKFLOW
    assert "docker/login-action" not in WORKFLOW
    assert "REGISTRY_USERNAME" not in WORKFLOW
    assert "REGISTRY_PASSWORD" not in WORKFLOW


def test_workflow_uses_configured_registry_and_commit_tag_for_existing_app():
    assert "${{ vars.REMATCHAPI_REGISTRY_URL }}/rematch-api:${{ github.sha }}" in WORKFLOW
    assert 'if [[ "$REGISTRY_URL" != "rematchbackend.azurecr.io" ]]' in WORKFLOW
    assert "az containerapp update" in WORKFLOW
    assert "--name rematch-api" in WORKFLOW
    assert "--resource-group Rematch-RG" in WORKFLOW
    assert '--image "$IMAGE"' in WORKFLOW
