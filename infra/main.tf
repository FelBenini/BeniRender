terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region = "us-east-1"
}

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

resource "aws_security_group" "render_worker" {
  name        = "blender-render-worker"
  description = "SSH access to Blender render worker"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description = "SSH from my workstation"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.my_ip_cidr]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name    = "blender-render-worker"
    Project = "blender-renderfarm"
  }
}

# Latest Amazon Linux 2023 x86-64 AMI
data "aws_ami" "al2023" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-2023.*-x86_64"]
  }

  filter {
    name   = "architecture"
    values = ["x86_64"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }

  filter {
    name   = "root-device-type"
    values = ["ebs"]
  }
}

# Register our local SSH public key with AWS
resource "aws_key_pair" "render_worker" {
  key_name   = "benirender-worker"
  public_key = file(pathexpand("~/.ssh/benirender-worker.pub"))

  tags = {
    Project = "blender-renderfarm"
  }
}

# First CPU render worker
resource "aws_instance" "render_worker" {
  ami                         = data.aws_ami.al2023.id
  instance_type               = "t3.medium"
  subnet_id                   = data.aws_subnets.default.ids[0]
  vpc_security_group_ids      = [aws_security_group.render_worker.id]
  key_name                    = aws_key_pair.render_worker.key_name
  associate_public_ip_address = true

  root_block_device {
    volume_size           = 20
    volume_type           = "gp3"
    encrypted             = true
    delete_on_termination = true
  }

  tags = {
    Name    = "benirender-worker-01"
    Project = "blender-renderfarm"
    Role    = "render-worker"
  }
}
