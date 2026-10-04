"""Deploy files: manifests parse, the default paths are safe and `docker compose config` accepts them."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"


def load_all(path: Path) -> list[dict]:
    return [doc for doc in yaml.safe_load_all(path.read_text()) if doc]


@pytest.mark.parametrize("path", sorted(DEPLOY.glob("vks/*.yaml")), ids=lambda p: p.name)
def test_kubernetes_manifests_parse(path):
    for doc in load_all(path):
        assert doc["apiVersion"] and doc["kind"]


def test_vks_service_is_not_public_by_default():
    (service,) = load_all(DEPLOY / "vks/service.yaml")
    # LoadBalancer without a verified internal-LB annotation may create a public load balancer.
    assert service["spec"]["type"] in ("ClusterIP", "NodePort")


def test_vks_deployment_runs_non_root_with_the_image_uid():
    (deployment,) = load_all(DEPLOY / "vks/deployment.yaml")
    security = deployment["spec"]["template"]["spec"]["securityContext"]
    assert security["runAsNonRoot"] is True and security["runAsUser"] == 10001
    assert "USER 10001" in (DEPLOY.parent / "Dockerfile").read_text()


def docker_compose_available() -> bool:
    try:
        return subprocess.run(["docker", "compose", "version"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


needs_compose = pytest.mark.skipif(not docker_compose_available(), reason="docker compose is not installed")


def compose_config(tmp_path: Path, subdir: str, *files: str, env: dict | None = None):
    """Run `docker compose config -q` on a copy of deploy/<subdir> where .env was created from .env.example."""
    work = tmp_path / subdir
    shutil.copytree(DEPLOY / subdir, work)
    shutil.copy(work / ".env.example", work / ".env")
    environment = {k: v for k, v in os.environ.items() if k != "CADDY_SITE"} | (env or {})
    args = [arg for f in files for arg in ("-f", f)]
    return subprocess.run(["docker", "compose", *args, "config", "-q"], cwd=work, env=environment,
                          capture_output=True, text=True, timeout=60)


@needs_compose
@pytest.mark.parametrize("subdir", ["vserver", "onprem"])
def test_default_compose_path_works_after_copying_env_example(tmp_path, subdir):
    result = compose_config(tmp_path, subdir)
    assert result.returncode == 0, result.stderr


@needs_compose
def test_vserver_tls_override_needs_caddy_site(tmp_path):
    files = ("docker-compose.yml", "docker-compose.tls.yml")
    assert compose_config(tmp_path / "a", "vserver", *files).returncode != 0
    result = compose_config(tmp_path / "b", "vserver", *files, env={"CADDY_SITE": "10.0.0.5"})
    assert result.returncode == 0, result.stderr


def test_compose_files_pin_image_tags():
    for compose in DEPLOY.glob("*/docker-compose*.yml"):
        for name, service in yaml.safe_load(compose.read_text())["services"].items():
            image = service.get("image", "")
            assert ":latest" not in image and (":" in image or "${" in image), f"{compose.name}/{name}: {image}"
