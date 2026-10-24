
variable "my_ip_cidr" {
  description = "Your public IPv4 address in CIDR notation, e.g. 203.0.113.25/32"
  type        = string

  validation {
    condition     = can(cidrnetmask(var.my_ip_cidr)) && endswith(var.my_ip_cidr, "/32")
    error_message = "my_ip_cidr must be a valid IPv4 /32 CIDR."
  }
}

variable "worker_count" {
  description = "Number of EC2 render workers to provision"
  type        = number
  default     = 1

  validation {
    condition = (
      var.worker_count >= 1 &&
      var.worker_count <= 20 &&
      var.worker_count == floor(var.worker_count)
    )
    error_message = "worker_count must be a whole number between 1 and 20."
  }
}

variable "instance_type" {
  description = "Instance type of EC2 render workers to provision"
  type        = string
  default     = "t3.large"
}
