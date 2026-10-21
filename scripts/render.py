"""Distribute Blender frame rendering across BeniRender workers."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from queue import Empty, Queue

from tools import INVENTORY, ROOT


PREPARE_PLAYBOOK = ROOT / "ansible" / "prepare_render.yml"
FRAME_PLAYBOOK = ROOT / "ansible" / "render_frame.yml"
COLLECT_PLAYBOOK = ROOT / "ansible" / "collect_render.yml"

print_lock = threading.Lock()


def log(message: str) -> None:
    """Print a complete message safely from multiple threads."""
    with print_lock:
        print(message, flush=True)


def run_command(command: list[str]) -> int:
    """Run a command and return its exit code."""
    return subprocess.run(command, check=False).returncode


def run_playbook(
    playbook: Path,
    extra_vars: dict[str, object],
    *,
    worker: str | None = None,
) -> int:
    """Run an Ansible playbook, optionally limiting it to one worker."""
    command = [
        "ansible-playbook",
        "-i",
        str(INVENTORY),
        str(playbook),
    ]

    if worker is not None:
        command.extend(["--limit", worker])

    command.extend(["--extra-vars", json.dumps(extra_vars)])
    return run_command(command)


def discover_workers() -> list[str]:
    """Read worker hostnames from the Ansible inventory."""
    result = subprocess.run(
        [
            "ansible-inventory",
            "-i",
            str(INVENTORY),
            "--list",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(
            "Could not read the Ansible inventory:\n"
            f"{result.stderr.strip()}"
        )

    try:
        inventory = json.loads(result.stdout)
        workers = inventory.get("render_workers", {}).get("hosts", [])
    except (json.JSONDecodeError, AttributeError) as exc:
        raise RuntimeError("Ansible returned an invalid inventory.") from exc

    if not workers:
        raise RuntimeError(
            "No hosts found in the 'render_workers' inventory group. "
            "Deploy the workers and check ansible/inventory.yml."
        )

    return sorted(set(workers))


def render_worker_frames(
    worker: str,
    frame_queue: Queue[int],
    base_vars: dict[str, object],
) -> list[tuple[int, int]]:
    """Render queued frames on one worker, one at a time."""
    failures: list[tuple[int, int]] = []

    while True:
        try:
            frame = frame_queue.get_nowait()
        except Empty:
            break

        log(f"[{worker}] Starting frame {frame}")

        frame_vars = {
            **base_vars,
            "frame_number": frame,
        }

        try:
            return_code = run_playbook(
                FRAME_PLAYBOOK,
                frame_vars,
                worker=worker,
            )
        except OSError as exc:
            log(f"[{worker}] Could not start frame {frame}: {exc}")
            return_code = 1
        finally:
            frame_queue.task_done()

        if return_code != 0:
            log(f"[{worker}] FAILED frame {frame} (exit code {return_code})")
            failures.append((frame, return_code))
        else:
            log(f"[{worker}] Completed frame {frame}")

    return failures


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Distribute Blender animation frames across BeniRender workers."
    )
    parser.add_argument("scene", type=Path, help="Local .blend file")
    parser.add_argument(
        "--start-frame",
        type=int,
        required=True,
        help="First frame to render (inclusive)",
    )
    parser.add_argument(
        "--end-frame",
        type=int,
        required=True,
        help="Last frame to render (inclusive)",
    )
    parser.add_argument(
        "--frame-step",
        type=int,
        default=1,
        help="Render every Nth frame (default: 1)",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=ROOT / "renders",
        help="Local output directory (default: renders/)",
    )
    parser.add_argument(
        "--async-timeout",
        type=int,
        default=86400,
        help="Maximum duration for one frame, in seconds (default: 86400)",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=5,
        help="Seconds between remote render status checks (default: 5)",
    )
    args = parser.parse_args()

    scene = args.scene.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()

    if not scene.is_file():
        parser.error(f"Scene file does not exist: {scene}")
    if scene.suffix.lower() != ".blend":
        parser.error("Scene file must have a .blend extension.")
    if args.start_frame < 0 or args.end_frame < args.start_frame:
        parser.error("Frame range must satisfy 0 <= start-frame <= end-frame.")
    if args.frame_step < 1:
        parser.error("--frame-step must be at least 1.")
    if args.async_timeout < 1 or args.poll_interval < 1:
        parser.error("Timeout and polling interval must be positive.")

    for path in (INVENTORY, PREPARE_PLAYBOOK, FRAME_PLAYBOOK, COLLECT_PLAYBOOK):
        if not path.is_file():
            parser.error(f"Required file not found: {path}")

    for executable in ("ansible-playbook", "ansible-inventory"):
        if not shutil.which(executable):
            parser.error(f"{executable} is not installed or not on PATH.")

    workers = discover_workers()
    frames = list(
        range(args.start_frame, args.end_frame + 1, args.frame_step)
    )

    if not frames:
        parser.error("The selected frame range contains no frames.")

    job_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    local_job_dir = output_dir / job_id
    local_job_dir.mkdir(parents=True, exist_ok=False)

    base_vars: dict[str, object] = {
        "scene_path": str(scene),
        "scene_filename": scene.name,
        "render_job_id": job_id,
        "render_timeout": args.async_timeout,
        "render_poll_interval": args.poll_interval,
        "local_output_dir": str(local_job_dir),
    }

    print(f"Scene:         {scene}")
    print(f"Job ID:        {job_id}")
    print(f"Frame range:   {frames[0]}–{frames[-1]} (step {args.frame_step})")
    print(f"Frame count:   {len(frames)}")
    print(f"Workers:       {len(workers)}")
    print(f"Local output:  {local_job_dir}")
    print(f"Worker list:   {', '.join(workers)}")

    print("\nPreparing the scene on all workers...\n", flush=True)

    if run_playbook(PREPARE_PLAYBOOK, base_vars) != 0:
        raise RuntimeError(
            "Scene preparation failed. No frame rendering was started. "
            f"See the Ansible output above. Job ID: {job_id}"
        )

    frame_queue: Queue[int] = Queue()
    for frame in frames:
        frame_queue.put(frame)

    failures: list[tuple[str, int, int]] = []

    print("\nStarting distributed frame rendering...\n", flush=True)

    with ThreadPoolExecutor(max_workers=len(workers)) as executor:
        futures = {
            executor.submit(
                render_worker_frames,
                worker,
                frame_queue,
                base_vars,
            ): worker
            for worker in workers
        }

        for future in as_completed(futures):
            worker = futures[future]
            try:
                worker_failures = future.result()
            except Exception as exc:
                log(f"[{worker}] Worker scheduler failed: {exc}")
                failures.extend(
                    (worker, frame, 1)
                    for frame in frames
                )
                continue

            failures.extend(
                (worker, frame, return_code)
                for frame, return_code in worker_failures
            )

    print("\nCollecting rendered frames from all workers...\n", flush=True)

    collect_vars = {
        **base_vars,
        "expected_frames": frames,
    }

    collection_code = run_playbook(COLLECT_PLAYBOOK, collect_vars)

    expected_files = {
        f"frame_{frame:04d}.png"
        for frame in frames
    }
    actual_files = {
        path.name
        for path in local_job_dir.glob("frame_*.png")
        if path.is_file() and path.stat().st_size > 0
    }

    missing_files = sorted(expected_files - actual_files)
    unexpected_files = sorted(actual_files - expected_files)

    print("\nRender summary")
    print("--------------")
    print(f"Expected frames: {len(expected_files)}")
    print(f"Downloaded:      {len(actual_files & expected_files)}")
    print(f"Failed tasks:    {len(failures)}")
    print(f"Missing frames:  {len(missing_files)}")
    print(f"Output:          {local_job_dir}")

    if failures:
        print("\nFailed frame tasks:")
        for worker, frame, code in sorted(failures, key=lambda item: item[1]):
            print(f"  Frame {frame} on {worker} (exit code {code})")

    if missing_files:
        print("\nMissing frame files:")
        for filename in missing_files:
            print(f"  {filename}")

    if unexpected_files:
        print("\nUnexpected frame files:")
        for filename in unexpected_files:
            print(f"  {filename}")

    if collection_code != 0:
        raise RuntimeError(
            "Frame collection failed. Some rendered files may still be "
            f"available in {local_job_dir}"
        )

    if failures or missing_files:
        raise RuntimeError(
            "Render job did not complete successfully. "
            f"Check the worker output above. Job ID: {job_id}"
        )

    print("\nRender completed successfully.")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        print(f"\nRENDER FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
