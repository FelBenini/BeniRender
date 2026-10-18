output "render_worker_public_ip" {
  description = "Public IPv4 address of the render worker"
  value       = aws_instance.render_worker.public_ip
}

output "render_worker_instance_id" {
  description = "EC2 instance ID of the render worker"
  value       = aws_instance.render_worker.id
}
