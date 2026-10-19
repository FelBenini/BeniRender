# AWS Systems Manager, used to establish trust, instead of asking the user to approve an unverified fingerprint

resource "aws_iam_role" "render_worker_ssm" {
  name = "benirender-worker-ssm-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Service = "ec2.amazonaws.com"
      }
      Action = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "render_worker_ssm" {
  role       = aws_iam_role.render_worker_ssm.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "render_worker" {
  name = "benirender-worker-instance-profile"
  role = aws_iam_role.render_worker_ssm.name
}
