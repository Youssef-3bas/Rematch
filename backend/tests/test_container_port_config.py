import unittest
from pathlib import Path
import sys


BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "scripts"))

from verify_container_port import validate_container_ports  # noqa: E402


class ContainerPortConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dockerfile = (BACKEND / "Dockerfile").read_text(encoding="utf-8")
        cls.manifest = (BACKEND / "containerapp.yaml").read_text(encoding="utf-8")
        cls.workflow = (
            BACKEND.parent
            / ".github/workflows/rematch-api-AutoDeployTrigger-a3fce886-ff6a-4c0e-8c2f-a81b4b50f027.yml"
        ).read_text(encoding="utf-8")

    def test_current_image_ingress_and_probes_agree_on_port_8000(self):
        validate_container_ports(self.dockerfile, self.manifest)

    def test_rejects_image_effective_port_mismatch(self):
        dockerfile = self.dockerfile.replace("PORT=8000", "PORT=80", 1)
        with self.assertRaisesRegex(ValueError, "Uvicorn default"):
            validate_container_ports(dockerfile, self.manifest)

    def test_rejects_ingress_target_port_mismatch(self):
        manifest = self.manifest.replace("targetPort: 8000", "targetPort: 80", 1)
        with self.assertRaisesRegex(ValueError, "ingress targetPort"):
            validate_container_ports(self.dockerfile, manifest)

    def test_rejects_any_probe_port_mismatch(self):
        manifest = self.manifest.replace("port: 8000", "port: 80", 1)
        with self.assertRaisesRegex(ValueError, "probe port"):
            validate_container_ports(self.dockerfile, manifest)

    def test_rejects_a_loopback_only_container_bind(self):
        dockerfile = self.dockerfile.replace("--host 0.0.0.0", "--host 127.0.0.1", 1)
        with self.assertRaisesRegex(ValueError, "bind to 0.0.0.0"):
            validate_container_ports(dockerfile, self.manifest)

    def test_workflow_builds_and_pushes_the_expected_commit_image(self):
        self.assertIn("az acr login --name rematchbackend --only-show-errors", self.workflow)
        self.assertIn("docker build --file backend/Dockerfile --tag \"$IMAGE\" backend", self.workflow)
        self.assertIn('docker push "$IMAGE"', self.workflow)
        self.assertIn(
            "IMAGE: ${{ vars.REMATCHAPI_REGISTRY_URL }}/rematch-api:${{ github.sha }}",
            self.workflow,
        )
        self.assertNotIn("docker/login-action", self.workflow)
        self.assertNotIn("REGISTRY_USERNAME", self.workflow)
        self.assertNotIn("REGISTRY_PASSWORD", self.workflow)

    def test_workflow_updates_only_the_existing_app_and_avoids_source_build(self):
        self.assertNotIn("azure/container-apps-deploy-action", self.workflow)
        self.assertNotIn("_dockerfilePathKey_", self.workflow)
        self.assertNotIn("--source", self.workflow)
        self.assertIn("az containerapp update", self.workflow)
        self.assertIn("--name rematch-api", self.workflow)
        self.assertIn("--resource-group Rematch-RG", self.workflow)
        self.assertIn("backend/containerapp.yaml", self.workflow)

    def test_workflow_preflights_required_secrets_and_existing_azure_resources(self):
        for secret_name in (
            "REMATCHAPI_AZURE_CLIENT_ID",
            "REMATCHAPI_AZURE_TENANT_ID",
            "REMATCHAPI_AZURE_SUBSCRIPTION_ID",
        ):
            self.assertIn(secret_name, self.workflow)
        self.assertIn("REMATCHAPI_REGISTRY_URL", self.workflow)
        self.assertIn("vars.REMATCHAPI_REGISTRY_URL", self.workflow)
        self.assertIn("az acr show --name rematchbackend", self.workflow)
        self.assertIn("az containerapp show --name rematch-api", self.workflow)
        self.assertIn("target_port\" != \"8000", self.workflow)


if __name__ == "__main__":
    unittest.main()
