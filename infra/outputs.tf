output "render_worker_public_ips" {
  description = "Public IP addresses of all render workers"
  value       = aws_instance.render_worker[*].public_ip
}

output "render_worker_instance_ids" {
  description = "EC2 instance IDs of all render workers"
  value       = aws_instance.render_worker[*].id
}

