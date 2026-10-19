#!/usr/bin/env python3
"""Provision BeniRender infrastructure and configure its Blender worker."""

import ipaddress
import subprocess
from pathlib import Path
import hashlib
import os
import tempfile

ROOT = Path(__file__).resolve().parent.parent
INFRA = ROOT / "infra"
INVENTORY = ROOT / "ansible" / "inventory.yml"
PLAYBOOK = ROOT / "ansible" / "site.yml"
SSH_KEY = Path.home() / ".ssh" / "benirender-worker"


def run(command, *, cwd=ROOT, capture_output=False):
    """Run a command, stopping deployment if it fails."""
    print(f"\n>>> {' '.join(map(str, command))}", flush=True)
    return subprocess.run(
        command,
        cwd=cwd,
        check=True,
        text=True,
        capture_output=capture_output,
    )

KNOWN_HOSTS = Path.home() / ".ssh" / "known_hosts"

def establish_host_trust(ip):
    """Fetch the EC2 SSH host key and require fingerprint verification."""
    print(f"\nChecking SSH host key for {ip}...", flush=True)

    result = run(
        ["ssh-keyscan", "-T", "10", "-t", "ed25519", str(ip)],
        capture_output=True,
    )

    keys = [
        line for line in result.stdout.splitlines()
        if line.strip() and not line.startswith("#")
    ]
    if not keys:
        raise SystemExit("Could not retrieve an SSH host key.")

    # Calculate the fingerprint shown by ssh-keygen.
    with tempfile.NamedTemporaryFile(mode="w", delete=False) as temp:
        temp.write("\n".join(keys) + "\n")
        temp_path = Path(temp.name)

    try:
        fingerprint = run(
            ["ssh-keygen", "-lf", str(temp_path), "-E", "sha256"],
            capture_output=True,
        ).stdout.strip()
    finally:
        temp_path.unlink(missing_ok=True)

    print("\nRetrieved host key fingerprint:")
    print(fingerprint)
    print(
        "\nVerify this fingerprint against a trusted source "
        "before approving it."
    )

    answer = input("Have you independently verified this fingerprint? [y/N] ")
    if answer.strip().lower() != "y":
        raise SystemExit("Host key not approved; stopping deployment.")

    KNOWN_HOSTS.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    KNOWN_HOSTS.touch(mode=0o600, exist_ok=True)

    # Replace any stale key for this IP, then add the approved key.
    run(
        ["ssh-keygen", "-R", str(ip), "-f", str(KNOWN_HOSTS)],
        capture_output=True,
    )
    with KNOWN_HOSTS.open("a") as known_hosts:
        known_hosts.write("\n".join(keys) + "\n")

    print("Verified host key added to known_hosts.")


def main():
    # Check local prerequisites.
    for tool in ("tofu", "ansible-playbook"):
        result = subprocess.run(
            ["which", tool], capture_output=True, text=True
        )
        if result.returncode != 0:
            raise SystemExit(f"Required command not found: {tool}")

    if not SSH_KEY.is_file():
        raise SystemExit(f"SSH private key not found: {SSH_KEY}")

    # Initialize and provision AWS infrastructure.
    run(["tofu", "init"], cwd=INFRA)
    run(["tofu", "apply"], cwd=INFRA)

    # Read the public IP from OpenTofu state.
    result = run(
        ["tofu", "output", "-raw", "render_worker_public_ip"],
        cwd=INFRA,
        capture_output=True,
    )
    ip = ipaddress.ip_address(result.stdout.strip())

    if ip.version != 4:
        raise SystemExit(f"Expected an IPv4 address, got: {ip}")

    # Add hosts fingerprint and add it to trusted hosts
    establish_host_trust(str(ip))
    # Generate Ansible inventory from the current infrastructure output.
    INVENTORY.write_text(
        "all:\n"
        "  children:\n"
        "    render_workers:\n"
        "      hosts:\n"
        "        worker01:\n"
        f"          ansible_host: {ip}\n"
        "          ansible_user: ec2-user\n"
        f"          ansible_ssh_private_key_file: {SSH_KEY}\n"
    )
    print(f"\nUpdated inventory: worker01 -> {ip}")

    # Configure the worker and install Blender.
    run([
        "ansible-playbook",
        "-i", str(INVENTORY),
        str(PLAYBOOK),
    ])

    print("\nBeniRender deployment completed successfully!")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        raise SystemExit(
            f"\nDeployment failed (exit code {exc.returncode}). "
            "Fix the error above and rerun the script."
        )
