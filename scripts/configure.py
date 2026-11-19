"""Generate per-installation secrets. Never rotate existing credentials implicitly."""
import argparse
import base64
import os
import secrets
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

ROOT = Path(__file__).resolve().parents[1]


def configure(destination=None, *, k8s_only=False):
    target = Path(destination) if destination else ROOT / ".env"
    if target.exists():
        print("Configuration already exists. Existing keys and password were preserved.")
        return target
    private = Ed25519PrivateKey.generate()
    private_bytes = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    public_bytes = private.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    content = {
        "JWT_PUBLIC_KEY": base64.b64encode(public_bytes).decode(),
        "JWT_PRIVATE_KEY": base64.b64encode(private_bytes).decode(),
        "INTERNAL_API_KEY": secrets.token_urlsafe(40),
        "NATS_USER": "parking", "NATS_PASSWORD": secrets.token_urlsafe(30),
        "ADMIN_EMAIL": "admin@parking.local", "ADMIN_PASSWORD": secrets.token_urlsafe(18),
    }
    if k8s_only:
        # The username and email come from the Kubernetes ConfigMap.
        for key in ("NATS_USER", "ADMIN_EMAIL"):
            del content[key]
    target.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive create avoids replacing keys if two initializers race.
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write("# Local secrets. Do not commit or publish this file.\n")
        for key, value in content.items():
            f.write(f"{key}={value}\n")
    if k8s_only:
        print("Created local Secret input.")
    else:
        print("Created local .env. Admin email and password are in ADMIN_EMAIL / ADMIN_PASSWORD.")
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", nargs="?")
    parser.add_argument("--k8s-only", action="store_true")
    args = parser.parse_args()
    configure(args.destination, k8s_only=args.k8s_only)
