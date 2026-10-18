
variable "my_ip_cidr" {
  description = "Your public IPv4 address in CIDR notation, e.g. 203.0.113.25/32"
  type        = string

  validation {
    condition     = can(cidrnetmask(var.my_ip_cidr)) && endswith(var.my_ip_cidr, "/32")
    error_message = "my_ip_cidr must be a valid IPv4 /32 CIDR."
  }
}
