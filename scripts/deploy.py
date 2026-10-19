#!/usr/bin/env python3
"""Deploy BeniRender and verify the worker SSH host key through AWS SSM."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from ipaddress import ip_address


ROOT = Path(__file__).resolve().parents[1]
INFRA = ROOT / "infra"
INVENTORY = ROOT / "ansible" / "inventory.yml"
PLAYBOOK = ROOT / "ansible" / "site.yml"

AWS_PROFILE = "renderfarm"
AWS_REGION = "us-east-1"
SSH_KEY = Path.home() / ".ssh" / "benirender-worker"
KNOWN_HOSTS = Path.home() / ".ssh" / "known_hosts"

SSM_WAIT_SECONDS = 300
COMMAND_WAIT_SECONDS = 120
SSH_SCAN_TIMEOUT_SECONDS = 15



def run(command: list[str], *, timeout: int | None = None) -> str:
    """Run a command with live output; raise if it fails."""
    print(f"\n$ {' '.join(command)}", flush=True)

    result = subprocess.run(
        command,
        text=True,
        stdout=None,  # Inherit terminal stdout; display output immediately.
        stderr=None,  # Inherit terminal stderr.
        timeout=timeout,
        check=False,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"Command failed with exit code {result.returncode}: "
            f"{command[0]}"
        )

    return ""


def capture(command: list[str], *, timeout: int | None = None) -> str:
    """Run a command and return its captured stdout."""
    print(f"\n$ {' '.join(command)}", flush=True)

    result = subprocess.run(
        command,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )

    if result.returncode != 0:
        if result.stdout:
            print(result.stdout, file=sys.stderr)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        raise RuntimeError(
            f"Command failed with exit code {result.returncode}: "
            f"{command[0]}"
        )

    return result.stdout.strip()

def aws_json(*args: str) -> dict:
    output = capture([
        "aws",
        "--profile", AWS_PROFILE,
        "--region", AWS_REGION,
        *args,
        "--output", "json",
    ])
    return json.loads(output)

def tofu_output(name: str) -> str:
    return capture([
        "tofu",
        f"-chdir={INFRA}",
        "output",
        "-raw",
        name,
    ])


def wait_for_ssm(instance_id: str) -> None:
    """Wait until AWS Systems Manager reports the instance online."""
    print(f"\nWaiting up to {SSM_WAIT_SECONDS}s for SSM registration...")

    deadline = time.monotonic() + SSM_WAIT_SECONDS

    while time.monotonic() < deadline:
        result = aws_json(
            "ssm", "describe-instance-information",
            "--filters", f"Key=InstanceIds,Values={instance_id}",
        )

        instances = result.get("InstanceInformationList", [])
        if instances and instances[0].get("PingStatus") == "Online":
            print("SSM reports the worker is Online.")
            return

        time.sleep(5)

    raise TimeoutError(
        "The worker did not become available through SSM. "
        "Check its instance profile, SSM Agent, network access, "
        "and your AWS permissions. No SSH or Ansible connection was attempted."
    )


def get_host_key_from_ssm(instance_id: str) -> tuple[str, str]:
    """Retrieve the worker's Ed25519 public host key using SSM."""
    result = aws_json(
        "ssm", "send-command",
        "--instance-ids", instance_id,
        "--document-name", "AWS-RunShellScript",
        "--parameters",
        json.dumps({
            "commands": [
                "test -r /etc/ssh/ssh_host_ed25519_key.pub "
                "&& cat /etc/ssh/ssh_host_ed25519_key.pub"
            ]
        }),
    )

    command_id = result["Command"]["CommandId"]
    print(f"\nRequested host key through SSM (command {command_id}).")

    deadline = time.monotonic() + COMMAND_WAIT_SECONDS

    while time.monotonic() < deadline:
        try:
            invocation = aws_json(
                "ssm", "get-command-invocation",
                "--command-id", command_id,
                "--instance-id", instance_id,
            )
        except (RuntimeError, json.JSONDecodeError):
            # SSM invocation details can take a moment to become available.
            time.sleep(2)
            continue

        status = invocation.get("Status")

        if status == "Success":
            lines = invocation.get("StandardOutputContent", "").splitlines()
            for line in lines:
                fields = line.strip().split()
                if len(fields) >= 2 and fields[0] == "ssh-ed25519":
                    return fields[0], fields[1]

            raise RuntimeError(
                "SSM succeeded but did not return a valid Ed25519 public key."
            )

        if status in {"Failed", "TimedOut", "Cancelled", "Cancelling"}:
            raise RuntimeError(
                f"SSM host-key retrieval ended with status {status}: "
                f"{invocation.get('StandardErrorContent', '').strip()}"
            )

        time.sleep(2)

    raise TimeoutError("Timed out waiting for the SSM host-key command.")


def scan_ssh_host_key(ip: str) -> tuple[str, str]:
    """Observe the public SSH endpoint's Ed25519 key; do not trust it yet."""
    print(f"\nObserving the SSH endpoint key at {ip}...")

    result = subprocess.run(
        [
            "ssh-keyscan",
            "-T", "5",
            "-t", "ed25519",
            ip,
        ],
        text=True,
        capture_output=True,
        timeout=SSH_SCAN_TIMEOUT_SECONDS,
    )

    for line in result.stdout.splitlines():
        fields = line.strip().split()
        if len(fields) >= 3 and fields[1] == "ssh-ed25519":
            return fields[1], fields[2]

    detail = result.stderr.strip()
    raise RuntimeError(
        f"Could not obtain an Ed25519 SSH host key from {ip}. "
        f"Check network access and security-group rules. {detail}"
    )


def existing_ed25519_keys(alias: str) -> set[tuple[str, str]]:
    """Read keys already pinned to this instance alias."""
    if not KNOWN_HOSTS.exists():
        return set()

    result = subprocess.run(
        ["ssh-keygen", "-F", alias, "-f", str(KNOWN_HOSTS)],
        text=True,
        capture_output=True,
    )

    if result.returncode not in (0, 1):
        raise RuntimeError("Could not inspect the SSH known_hosts file.")

    keys = set()
    for line in result.stdout.splitlines():
        if line.startswith("#"):
            continue
        fields = line.split()
        if len(fields) >= 3 and fields[1] == "ssh-ed25519":
            keys.add((fields[1], fields[2]))

    return keys


def pin_verified_host_key(
    alias: str,
    ssm_key: tuple[str, str],
    observed_key: tuple[str, str],
) -> None:
    """Pin only after independently comparing the SSM and SSH endpoint keys."""
    if ssm_key != observed_key:
        raise RuntimeError(
            "SSH HOST-KEY VERIFICATION FAILED: the key returned by SSM "
            "does not match the key observed at the SSH endpoint. "
            "The key was not added to known_hosts; Ansible will not run."
        )

    previous_keys = existing_ed25519_keys(alias)

    if previous_keys and previous_keys != {ssm_key}:
        raise RuntimeError(
            f"SSH HOST-KEY CHANGE DETECTED for {alias}. "
            "The previously pinned key differs from the key confirmed by SSM. "
            "Refusing to overwrite known_hosts automatically. Investigate "
            "the instance and rotate the pin only through a deliberate "
            "recovery procedure."
        )

    if previous_keys == {ssm_key}:
        print(f"Existing pinned host key for {alias} matches.")
        return

    KNOWN_HOSTS.parent.mkdir(parents=True, exist_ok=True)
    KNOWN_HOSTS.touch(mode=0o600, exist_ok=True)

    # The alias is tied to this EC2 instance ID, not its changeable public IP.
    with KNOWN_HOSTS.open("a", encoding="utf-8") as known_hosts:
        known_hosts.write(f"{alias} {ssm_key[0]} {ssm_key[1]}\n")

    KNOWN_HOSTS.chmod(0o600)
    print(f"Verified SSH host key pinned for {alias}.")


def write_inventory(ip: str, alias: str) -> None:
    """Write inventory with strict SSH host-key verification enabled."""
    inventory = f"""\
all:
  children:
    render_workers:
      hosts:
        worker01:
          ansible_host: {ip}
          ansible_user: ec2-user
          ansible_ssh_private_key_file: "{SSH_KEY}"
          ansible_ssh_common_args: >-
            -o HostKeyAlias={alias}
            -o StrictHostKeyChecking=yes
            -o UserKnownHostsFile={KNOWN_HOSTS}
"""
    INVENTORY.parent.mkdir(parents=True, exist_ok=True)
    INVENTORY.write_text(inventory, encoding="utf-8")
    print(f"\nWrote Ansible inventory: {INVENTORY}")


def main() -> None:
    for executable in ("tofu", "aws", "ansible-playbook", "ssh-keyscan"):
        if not shutil.which(executable):
            raise RuntimeError(f"Required executable not found: {executable}")

    if not SSH_KEY.is_file():
        raise RuntimeError(f"SSH private key not found: {SSH_KEY}")

    print("=== BeniRender deployment ===")

    run(["tofu", f"-chdir={INFRA}", "init"])
    run(["tofu", f"-chdir={INFRA}", "apply", "-auto-approve"])

    instance_id = tofu_output("render_worker_instance_id")
    ip = tofu_output("render_worker_public_ip")

    try:
        ip_address(ip)
    except ValueError as exc:
        raise RuntimeError("OpenTofu returned an invalid worker IP address.") from exc

    if not instance_id.startswith("i-"):
        raise RuntimeError("OpenTofu returned an unexpected EC2 instance ID.")

    # Tie the SSH trust entry to this exact EC2 instance.
    alias = f"benirender-{instance_id}"

    wait_for_ssm(instance_id)
    ssm_key = get_host_key_from_ssm(instance_id)
    observed_key = scan_ssh_host_key(ip)

    # Any failure here stops deployment before Ansible can connect.
    pin_verified_host_key(alias, ssm_key, observed_key)
    write_inventory(ip, alias)

    print("\n=== Installing/configuring the worker with Ansible ===")
    run([
        "ansible-playbook",
        "-i", str(INVENTORY),
        str(PLAYBOOK),
    ])

    print("\nDeployment completed successfully.")
    print(f"Instance ID: {instance_id}")
    print(f"Worker IP: {ip}")
    print(f"SSH host-key alias: {alias}")
    print(
        "\nFor direct SSH, use the HostKeyAlias and known_hosts settings "
        "shown below."
    )
    print(
        f"ssh -i {SSH_KEY} "
        f"-o HostKeyAlias={alias} "
        f"-o StrictHostKeyChecking=yes "
        f"-o UserKnownHostsFile={KNOWN_HOSTS} "
        f"ec2-user@{ip}"
    )


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, TimeoutError, subprocess.TimeoutExpired) as exc:
        print(f"\nDEPLOYMENT STOPPED: {exc}", file=sys.stderr)
        sys.exit(1)

