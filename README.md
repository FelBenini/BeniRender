# BeniRender

**BeniRender** is a Blender render farm project that uses AWS EC2, OpenTofu, Ansible, and Python to distribute animation frames across render workers.

## Features

- Infrastructure provisioning with OpenTofu
- Worker configuration with Ansible
- Distributed frame rendering
- Automatic frame collection
- Configurable frame ranges and worker count

## Requirements

- Linux workstation
- Python 3
- OpenTofu
- Ansible Core
- AWS CLI v2
- AWS account with EC2 permissions

GPU rendering additionally requires a supported NVIDIA GPU instance, compatible NVIDIA drivers, and a Blender build configured for CUDA or OptiX.

## Project Structure

```text
BeniRender/
├── ansible/       # Worker configuration and render playbooks
├── infra/         # AWS infrastructure (OpenTofu)
├── scripts/       # Python rendering orchestrator
```

## Infrastructure Deployment

### 1. Configure AWS credentials

```bash
aws configure sso --profile renderfarm
aws sso login --profile renderfarm
export AWS_PROFILE=renderfarm
```

Verify your credentials:

```bash
aws sts get-caller-identity
```

### 2. Configure infrastructure

Create `infra/terraform.tfvars` with your configuration, including your public IP and desired worker count:

```hcl
my_ip_cidr   = "YOUR_PUBLIC_IP/32"
worker_count = 1
```

Replace `YOUR_PUBLIC_IP` with your public IP address. Keep local configuration and state files out of version control.

### 3. Deploy workers

From the project root, run:

```bash
python scripts/deploy.py
```

Or, you can also specify the amount of workers you want to deploy:

```bash
python scripts/deploy.py --workers 5
```

The deployment script manages infrastructure provisioning. Review its output and confirm the deployment completes successfully.

**Note:** Your AWS account must have sufficient EC2 vCPU quota for the selected instance type. GPU instance families may require a quota increase before deployment.

### 4. Verify deployment

Check the deployment output for worker instance IDs and public IP addresses. Confirm that the workers are running and that Ansible can reach and configure them before starting a render.

### 4. Configure and verify workers

Run the project's Ansible configuration playbook using the inventory and variables defined by the repository. Verify that Blender is installed and that the worker can access the scene and required assets.

For GPU rendering, verify that the NVIDIA driver and the selected Blender GPU backend work correctly before submitting a full animation.

## Rendering

From the project root, run:

```bash
python scripts/render.py path/to/your/scene.blend \
  --start-frame 1 \
  --end-frame 6
```

Useful options:

- `--start-frame`: First frame to render
- `--end-frame`: Last frame to render
- `--frame-step`: Interval between frames
- `--output-dir`: Local output directory
- `--async-timeout`: Maximum render task duration
- `--poll-interval`: Interval for checking render progress

For example, to render frames 1–240:

```bash
python scripts/render.py path/to/your/scene.blend \
  --start-frame 1 \
  --end-frame 240
```

Check the script's help for current defaults:

```bash
python scripts/render.py --help
```

## Cleanup

When finished, review the infrastructure plan before destroying deployed resources:

```bash
cd infra
tofu plan -destroy
tofu destroy
```

Check the AWS Console afterward for any remaining billable resources.

## Limitations

- Blender scenes with external textures, caches, linked libraries, or other dependencies may require additional asset packaging or synchronization.
- GPU rendering depends on compatible instance hardware, drivers, and Blender configuration.
- Rendering speed and cost depend on scene complexity, resolution, samples, and worker count.
