
"""Upload, render, and download a Blender scene using Ansible."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "ansible" / "inventory.yml"
PLAYBOOK = ROOT / "ansible" / "render.yml"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Render a Blender scene on the BeniRender worker."
    )
    parser.add_argument("scene", type=Path, help="Local .blend file")
    parser.add_argument(
        "-o", "--output-dir",
        type=Path,
        default=ROOT / "renders",
        help="Local output directory (default: renders/)",
    )
    parser.add_argument(
        "--async-timeout",
        type=int,
        default=86400,
        help="Maximum render duration in seconds (default: 86400)",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=5,
        help="Ansible async polling interval in seconds (default: 5)",
    )
    args = parser.parse_args()

    scene = args.scene.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()

    if not scene.is_file():
        parser.error(f"Scene file does not exist: {scene}")
    if scene.suffix.lower() != ".blend":
        parser.error("Scene file must have a .blend extension.")
    if not INVENTORY.is_file():
        parser.error(f"Ansible inventory not found: {INVENTORY}")
    if not PLAYBOOK.is_file():
        parser.error(f"Ansible playbook not found: {PLAYBOOK}")
    if args.async_timeout < 1 or args.poll_interval < 1:
        parser.error("Timeout and polling interval must be positive.")
    if not shutil.which("ansible-playbook"):
        parser.error("ansible-playbook is not installed or not on PATH.")

    job_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    local_job_dir = output_dir / job_id
    local_job_dir.mkdir(parents=True, exist_ok=True)

    extra_vars = {
        "scene_path": str(scene),
        "scene_filename": scene.name,
        "render_job_id": job_id,
        "render_timeout": args.async_timeout,
        "render_poll_interval": args.poll_interval,
        "local_output_dir": str(local_job_dir),
    }

    command = [
        "ansible-playbook",
        "-i", str(INVENTORY),
        str(PLAYBOOK),
        "--extra-vars", json.dumps(extra_vars),
    ]

    print(f"Scene:        {scene}")
    print(f"Local output: {local_job_dir}")
    print(f"Job ID:       {job_id}")
    print("\nStarting render; Ansible output will appear below.\n", flush=True)

    result = subprocess.run(command, check=False)

    if result.returncode != 0:
        raise RuntimeError(
            f"Ansible render playbook failed with exit code {result.returncode}. "
            f"Check the output above. Local output directory: {local_job_dir}"
        )

    files = sorted(p for p in local_job_dir.iterdir() if p.is_file())
    print(f"\nRender completed. Downloaded {len(files)} file(s).")
    print(f"Output directory: {local_job_dir}")
    for path in files:
        print(f"  {path.name}")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        print(f"\nRENDER FAILED: {exc}", file=sys.stderr)
        sys.exit(1)

