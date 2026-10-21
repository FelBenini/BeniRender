
#!/usr/bin/env python3
"""Deploy BeniRender workers and verify SSH host keys through AWS SSM."""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import time
from ipaddress import ip_address
from pathlib import Path

from tools import ROOT, INVENTORY, capture, require_executables, run


INFRA = ROOT / "infra"
PLAYBOOK = ROOT / "ansible" / "site.yml"

AWS_PROFILE = "renderfarm"
AWS_REGION = "us-east-1"
SSH_KEY = Path.home() / ".ssh" / "benirender-worker"
KNOWN_HOSTS = Path.home() / ".ssh" / "known_hosts"

MIN_WORKERS = 1
MAX_WORKERS = 20

SSM_WAIT_SECONDS = 300
COMMAND_WAIT_SECONDS = 120
SSH_SCAN_TIMEOUT_SECONDS = 15


def aws_json(*args: str) -> dict:
    """Run AWS CLI with the configured profile and parse JSON."""
    output = capture([
        "aws",
        "--profile", AWS_PROFILE,
        "--region", AWS_REGION,
        *args,
        "--output", "json",
    ])
    return json.loads(output)


def tofu_output_json(name: str) -> list:
    """Read a list-valued OpenTofu output."""
    output = capture([
        "tofu",
        f"-chdir={INFRA}",
        "output",
        "-json",
        name,
    ])

    value = json.loads(output)

    if not isinstance(value, list):
        raise RuntimeError(
            f"OpenTofu output {name!r} must be a list."
        )

    return value


def wait_for_ssm(instance_id: str) -> None:
    """Wait until Systems Manager reports the instance online."""
    print(
        f"\nWaiting up to {SSM_WAIT_SECONDS}s for "
        f"{instance_id} to register with SSM..."
    )

    deadline = time.monotonic() + SSM_WAIT_SECONDS

    while time.monotonic() < deadline:
        result = aws_json(
            "ssm",
            "describe-instance-information",
            "--filters",
            f"Key=InstanceIds,Values={instance_id}",
        )

        instances = result.get("InstanceInformationList", [])

        if (
            instances
            and instances[0].get("PingStatus") == "Online"
        ):
            print(f"{instance_id}: SSM is Online.")
            return

        time.sleep(5)

    raise TimeoutError(
        f"{instance_id} did not become available through SSM. "
        "Check the instance profile, SSM Agent, network access, "
        "and AWS permissions. SSH and Ansible were not attempted "
        "for this instance."
    )


def get_host_key_from_ssm(
    instance_id: str,
) -> tuple[str, str]:
    """Retrieve the instance's Ed25519 public host key through SSM."""
    result = aws_json(
        "ssm",
        "send-command",
        "--instance-ids",
        instance_id,
        "--document-name",
        "AWS-RunShellScript",
        "--parameters",
        json.dumps({
            "commands": [
                "test -r /etc/ssh/ssh_host_ed25519_key.pub "
                "&& cat /etc/ssh/ssh_host_ed25519_key.pub"
            ]
        }),
    )

    command_id = result["Command"]["CommandId"]
    print(
        f"{instance_id}: requested host key through SSM "
        f"(command {command_id})."
    )

    deadline = time.monotonic() + COMMAND_WAIT_SECONDS

    while time.monotonic() < deadline:
        try:
            invocation = aws_json(
                "ssm",
                "get-command-invocation",
                "--command-id",
                command_id,
                "--instance-id",
                instance_id,
            )
        except (RuntimeError, json.JSONDecodeError):
            time.sleep(2)
            continue

        status = invocation.get("Status")

        if status == "Success":
            lines = invocation.get(
                "StandardOutputContent", ""
            ).splitlines()

            for line in lines:
                fields = line.strip().split()

                if (
                    len(fields) >= 2
                    and fields[0] == "ssh-ed25519"
                ):
                    return fields[0], fields[1]

            raise RuntimeError(
                f"{instance_id}: SSM did not return a valid "
                "Ed25519 public host key."
            )

        if status in {
            "Failed",
            "TimedOut",
            "Cancelled",
            "Cancelling",
        }:
            raise RuntimeError(
                f"{instance_id}: SSM host-key retrieval ended "
                f"with status {status}: "
                f"{invocation.get('StandardErrorContent', '').strip()}"
            )

        time.sleep(2)

    raise TimeoutError(
        f"{instance_id}: timed out waiting for the SSM host-key command."
    )


def scan_ssh_host_key(ip: str) -> tuple[str, str]:
    """Observe the SSH endpoint's key without trusting it yet."""
    print(f"Observing SSH host key at {ip}...")

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
        check=False,
    )

    for line in result.stdout.splitlines():
        fields = line.strip().split()

        if len(fields) >= 3 and fields[1] == "ssh-ed25519":
            return fields[1], fields[2]

    raise RuntimeError(
        f"Could not obtain an Ed25519 SSH host key from {ip}. "
        "Check network access and security-group rules. "
        f"{result.stderr.strip()}"
    )


def existing_ed25519_keys(
    alias: str,
) -> set[tuple[str, str]]:
    """Read Ed25519 keys already pinned to this instance alias."""
    if not KNOWN_HOSTS.exists():
        return set()

    result = subprocess.run(
        [
            "ssh-keygen",
            "-F", alias,
            "-f", str(KNOWN_HOSTS),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    if result.returncode not in (0, 1):
        raise RuntimeError(
            f"Could not inspect known_hosts for {alias}."
        )

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
    """Pin only after matching the SSM and SSH endpoint keys."""
    if ssm_key != observed_key:
        raise RuntimeError(
            f"SSH HOST-KEY VERIFICATION FAILED for {alias}: "
            "the SSM key does not match the SSH endpoint key. "
            "The key was not added to known_hosts."
        )

    previous_keys = existing_ed25519_keys(alias)

    if previous_keys and previous_keys != {ssm_key}:
        raise RuntimeError(
            f"SSH HOST-KEY CHANGE DETECTED for {alias}. "
            "The previously pinned key differs from the key "
            "confirmed by SSM. Refusing to overwrite known_hosts."
        )

    if previous_keys == {ssm_key}:
        print(f"{alias}: existing pinned host key matches.")
        return

    KNOWN_HOSTS.parent.mkdir(parents=True, exist_ok=True)
    KNOWN_HOSTS.touch(mode=0o600, exist_ok=True)

    with KNOWN_HOSTS.open("a", encoding="utf-8") as known_hosts:
        known_hosts.write(
            f"{alias} {ssm_key[0]} {ssm_key[1]}\n"
        )

    KNOWN_HOSTS.chmod(0o600)
    print(f"{alias}: verified SSH host key pinned.")


def verify_worker(instance_id: str, ip: str) -> str:
    """Verify one worker and return its instance-specific SSH alias."""
    try:
        ip_address(ip)
    except ValueError as exc:
        raise RuntimeError(
            f"{instance_id}: OpenTofu returned an invalid IP address."
        ) from exc

    if not instance_id.startswith("i-"):
        raise RuntimeError(
            f"Unexpected EC2 instance ID: {instance_id}"
        )

    alias = f"benirender-{instance_id}"

    wait_for_ssm(instance_id)
    ssm_key = get_host_key_from_ssm(instance_id)
    observed_key = scan_ssh_host_key(ip)
    pin_verified_host_key(alias, ssm_key, observed_key)

    return alias


def write_inventory(
    workers: list[tuple[str, str, str]],
) -> None:
    """Write all verified workers to the Ansible inventory."""
    lines = [
        "all:",
        "  children:",
        "    render_workers:",
        "      hosts:",
    ]

    for index, (instance_id, ip, alias) in enumerate(
        workers, start=1
    ):
        host_name = f"worker{index:02d}"

        common_args = " ".join([
            f"-o HostKeyAlias={shlex.quote(alias)}",
            "-o StrictHostKeyChecking=yes",
            (
                "-o UserKnownHostsFile="
                f"{shlex.quote(str(KNOWN_HOSTS))}"
            ),
        ])

        lines.extend([
            f"        {host_name}:",
            f"          ansible_host: {json.dumps(ip)}",
            "          ansible_user: ec2-user",
            (
                "          ansible_ssh_private_key_file: "
                f"{json.dumps(str(SSH_KEY))}"
            ),
            (
                "          ansible_ssh_common_args: "
                f"{json.dumps(common_args)}"
            ),
            (
                "          benirender_instance_id: "
                f"{json.dumps(instance_id)}"
            ),
        ])

    INVENTORY.parent.mkdir(parents=True, exist_ok=True)
    INVENTORY.write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )

    print(f"\nWrote Ansible inventory: {INVENTORY}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Deploy the BeniRender EC2 worker pool"
    )

    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help=(
            f"Number of EC2 workers ({MIN_WORKERS}-{MAX_WORKERS}; "
            "default: 1)"
        ),
    )

    args = parser.parse_args()

    if not MIN_WORKERS <= args.workers <= MAX_WORKERS:
        parser.error(
            f"--workers must be between {MIN_WORKERS} and {MAX_WORKERS}"
        )

    return args


def main() -> None:
    args = parse_args()

    require_executables(
        "tofu",
        "aws",
        "ansible-playbook",
        "ssh-keyscan",
        "ssh-keygen",
    )

    if not SSH_KEY.is_file():
        raise RuntimeError(f"SSH private key not found: {SSH_KEY}")

    print("=== BeniRender deployment ===")
    print(f"Requested workers: {args.workers}")
    print(f"AWS profile: {AWS_PROFILE}")
    print(f"AWS region: {AWS_REGION}")

    run(["tofu", f"-chdir={INFRA}", "init"])

    run([
        "tofu",
        f"-chdir={INFRA}",
        "apply",
        "-auto-approve",
        f"-var=worker_count={args.workers}",
    ])

    # These names must match infra/outputs.tf.
    instance_ids = tofu_output_json("render_worker_instance_ids")
    public_ips = tofu_output_json("render_worker_public_ips")

    if len(instance_ids) != args.workers:
        raise RuntimeError(
            "The number of instance IDs returned by OpenTofu "
            f"({len(instance_ids)}) does not match the requested "
            f"worker count ({args.workers})."
        )

    if len(public_ips) != args.workers:
        raise RuntimeError(
            "The number of public IPs returned by OpenTofu "
            f"({len(public_ips)}) does not match the requested "
            f"worker count ({args.workers})."
        )

    if len(set(instance_ids)) != len(instance_ids):
        raise RuntimeError("OpenTofu returned duplicate instance IDs.")

    if len(set(public_ips)) != len(public_ips):
        raise RuntimeError("OpenTofu returned duplicate public IPs.")

    print(f"\nOpenTofu returned {len(instance_ids)} worker(s).")

    workers = list(zip(instance_ids, public_ips))
    verified_workers = []

    # Verify every worker before running Ansible on any worker.
    for index, (instance_id, ip) in enumerate(workers, start=1):
        print(
            f"\n=== Verifying worker {index}/{args.workers} "
            f"({instance_id}) ==="
        )

        alias = verify_worker(instance_id, ip)
        verified_workers.append((instance_id, ip, alias))

    write_inventory(verified_workers)

    print("\n=== Installing/configuring workers with Ansible ===")
    run([
        "ansible-playbook",
        "-i", str(INVENTORY),
        str(PLAYBOOK),
    ])

    print("\n=== Deployment completed successfully ===")
    print(f"Workers configured: {len(verified_workers)}")

    for index, (instance_id, ip, alias) in enumerate(
        verified_workers, start=1
    ):
        print(
            f"\nWorker {index:02d}\n"
            f"  Instance ID: {instance_id}\n"
            f"  Public IP:   {ip}\n"
            f"  SSH alias:   {alias}\n"
            f"  SSH command: ssh -i {shlex.quote(str(SSH_KEY))} "
            f"-o HostKeyAlias={shlex.quote(alias)} "
            "-o StrictHostKeyChecking=yes "
            f"-o UserKnownHostsFile={shlex.quote(str(KNOWN_HOSTS))} "
            f"ec2-user@{ip}"
        )


if __name__ == "__main__":
    try:
        main()
    except (
        RuntimeError,
        TimeoutError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
    ) as exc:
        print(f"\nDEPLOYMENT STOPPED: {exc}", file=sys.stderr)
        sys.exit(1)

