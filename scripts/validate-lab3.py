"""Check Compose, Dockerfile and Kubernetes interfaces without a cluster.

Requires PyYAML: python -m pip install PyYAML
"""
import re
from collections import Counter
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
K8S = ROOT / "k8s"
PART = {"app.kubernetes.io/part-of": "parkslot"}


def read(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def check():
    compose = read(ROOT / "docker-compose.yml")
    compose_services = compose["services"]
    assert compose["name"] == "parkslot-lab3"
    assert compose_services["gateway"]["ports"] == ["127.0.0.1:8081:8000"]
    business = {p.name for p in (ROOT / "services").iterdir() if (p / "app.py").is_file()}
    assert len(business) == 10
    http = business | {"gateway"}
    assert set(compose_services) == http | {"nats", "tests"}
    assert all(row.startswith("127.0.0.1:") for spec in compose_services.values()
               for row in spec.get("ports", []))

    base = read(K8S / "kustomization.yaml")
    assert base["namespace"] == "parkslot"
    manifests = {}
    for filename in base["resources"]:
        assert "secret" not in filename
        for obj in yaml.safe_load_all((K8S / filename).read_text(encoding="utf-8")):
            identity = obj["kind"], obj["metadata"]["name"]
            assert identity not in manifests, identity
            assert obj["metadata"]["labels"] == PART
            manifests[identity] = obj
    assert Counter(kind for kind, _ in manifests) == {
        "Namespace": 1, "ConfigMap": 2, "PersistentVolumeClaim": 11, "Deployment": 11,
        "StatefulSet": 1, "Service": 13,
    }
    cfg = manifests["ConfigMap", "parkslot-runtime"]["data"]
    assert cfg["NATS_URL"] == "nats://nats:4222" and cfg["DATABASE_PATH"] == "/data/service.db"
    for name in business:
        key = name.removesuffix("-service").upper() + "_URL"
        target = "http://" + name + ":8000"
        assert cfg[key] == target == compose_services[name]["environment"][key]
    assert all(isinstance(v, str) and "localhost" not in v and "127.0.0.1" not in v for v in cfg.values())
    assert not ({"JWT_PRIVATE_KEY", "ADMIN_PASSWORD", "NATS_PASSWORD", "INTERNAL_API_KEY"} & set(cfg))
    nats_config = manifests["ConfigMap", "parkslot-nats"]["data"]["nats.conf"]
    assert nats_config == (ROOT / "infrastructure/nats.conf").read_text(encoding="utf-8")
    assert "$NATS_PASSWORD" in nats_config and "/data/jetstream" in nats_config

    used_claims = set()
    for name in sorted(http | {"nats"}):
        kind = "StatefulSet" if name == "nats" else "Deployment"
        workload = manifests[kind, name]["spec"]
        assert workload["replicas"] == 1 and workload["selector"]["matchLabels"] == {"app": name}
        template = workload["template"]
        labels = template["metadata"]["labels"]
        assert labels["app"] == name and all(labels[k] == v for k, v in PART.items())
        pod = template["spec"]
        app, = pod["containers"]
        assert pod["automountServiceAccountToken"] is False
        assert pod["securityContext"]["runAsNonRoot"] and pod["securityContext"]["runAsUser"] == 10001
        assert app["securityContext"]["readOnlyRootFilesystem"]
        assert not app["securityContext"]["allowPrivilegeEscalation"]
        assert app["image"] == compose_services[name]["image"]
        assert app["imagePullPolicy"] == ("IfNotPresent" if name == "nats" else "Never")
        assert app["envFrom"][0]["configMapRef"]["name"] == "parkslot-runtime"
        secrets = {p["secretRef"]["name"] for p in app["envFrom"] if "secretRef" in p}
        keys = {p["name"]: p["valueFrom"]["secretKeyRef"]["name"] for p in app.get("env", [])
                if "secretKeyRef" in p.get("valueFrom", {})}
        expected = set() if name in ("gateway", "nats") else {"parkslot-shared"}
        if name == "auth-service":
            expected.add("parkslot-auth")
        assert secrets == expected
        assert set(keys.values()) <= {"parkslot-shared"}
        if name == "gateway":
            assert set(keys) == {"JWT_PUBLIC_KEY", "INTERNAL_API_KEY"}
            assert any(p["name"] == "PARKSLOT_POD_NAME" and
                       p["valueFrom"]["fieldRef"]["fieldPath"] == "metadata.name" for p in app["env"])
        if name == "nats":
            assert set(keys) == {"NATS_PASSWORD"}
            assert app["args"] == ["nats-server", "-c", "/etc/parkslot/nats.conf"]

        ports = {p["name"]: p["containerPort"] for p in app["ports"]}
        service = manifests["Service", name]["spec"]
        assert service["type"] == "ClusterIP" and service["selector"] == {"app": name}
        for port in service["ports"]:
            assert ports[port["targetPort"]] == port["port"]
        living, ready = (("/healthz", "/healthz?js-enabled-only=true") if name == "nats"
                         else ("/health", "/ready"))
        for probe, path in (("startupProbe", living), ("livenessProbe", living), ("readinessProbe", ready)):
            assert app[probe]["httpGet"]["path"] == path
            assert app[probe]["httpGet"]["port"] in ports
        for container in pod["containers"] + pod.get("initContainers", []):
            resources = container["resources"]
            for resource, suffix in (("cpu", "m"), ("memory", "Mi")):
                requested = resources["requests"][resource]
                limited = resources["limits"][resource]
                assert requested.endswith(suffix) and limited.endswith(suffix)
                assert 0 < int(requested.removesuffix(suffix)) <= int(limited.removesuffix(suffix))
        if name == "gateway":
            assert "volumes" not in pod and "initContainers" not in pod
            assert workload["strategy"]["type"] == "RollingUpdate"
        else:
            claim, = (v["persistentVolumeClaim"]["claimName"] for v in pod["volumes"]
                      if "persistentVolumeClaim" in v)
            assert claim not in used_claims
            used_claims.add(claim)
            assert manifests["PersistentVolumeClaim", claim]["spec"]["accessModes"] == ["ReadWriteOnce"]
            assert {"name": "data", "mountPath": "/data"} in app["volumeMounts"]
            if name == "nats":
                assert manifests["Service", workload["serviceName"]]["spec"]["clusterIP"] == "None"
                assert any(v.get("configMap", {}).get("name") == "parkslot-nats" for v in pod["volumes"])
            else:
                assert workload["strategy"]["type"] == "Recreate"
                assert compose_services[name]["volumes"] == [claim + ":/data"]
        if name in http:
            dockerfile = ROOT / compose_services[name]["build"]["dockerfile"]
            assert dockerfile.name == "Dockerfile" and dockerfile.is_file()
            assert dockerfile.with_name("app.py").is_file()
            build = dockerfile.read_text(encoding="utf-8")
            context_filter = dockerfile.with_name("Dockerfile.dockerignore").read_text(encoding="utf-8")
            assert "FROM python:3.12.12-slim" in build
            assert "USER 10001:10001" in build and 'ENTRYPOINT ["python", "-m", "uvicorn"]' in build
            assert build.index("RUN pip install") < build.index("COPY parking_common")
            assert "COPY . " not in build and ".env" not in build and "JWT_" not in build
            assert context_filter.startswith("**\n") and "**/.env" in context_filter and "**/*.key" in context_filter

    assert used_claims == {name for kind, name in manifests if kind == "PersistentVolumeClaim"}
    script = (ROOT / "scripts/start-k8s.ps1").read_text(encoding="utf-8")
    service_list = re.search(r"\$services\s*=\s*@\((.*?)\)", script, re.S)[1]
    assert set(re.findall("'([a-z-]+)'", service_list)) == http
    assert "--dry-run=server" in script and "Run-Mini image load --daemon" in script
    print("OK: 10 service Dockerfiles + Gateway, Compose wiring, and 39 Kubernetes objects")
    print("OK: image tags, selectors, ports, 11 PVCs, secret references, health and resource contracts")


if __name__ == "__main__":
    check()
