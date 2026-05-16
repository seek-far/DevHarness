variable "aws_region" {
  description = "AWS region (phase-2; validate does not use it)."
  type        = string
  default     = "us-east-1"
}

# Same contract passthrough as the openstack root — identical names.
variable "name_prefix" {
  type    = string
  default = "sdlcma-1b"
}

variable "control_count" {
  type    = number
  default = 1
}

variable "worker_count" {
  type    = number
  default = 1
}

variable "control_flavor" {
  type    = string
  default = "t3.small"
}

variable "worker_flavor" {
  type    = string
  default = "t3.small"
}

variable "image_name" {
  type    = string
  default = "ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"
}

variable "ssh_public_key" {
  description = "Public key string. No default (same discipline as the openstack root): never guessed."
  type        = string
}

variable "ssh_user" {
  type    = string
  default = "ubuntu"
}

variable "network_cidr" {
  type    = string
  default = "10.10.0.0/24"
}

variable "external_network" {
  type    = string
  default = ""
}

variable "cloud_init" {
  type    = string
  default = ""
}

variable "cloud_init_file" {
  description = "Path (relative to this root) to a cloud-init file; contents win over var.cloud_init. Phase-2 points this at ../../k8s-bootstrap/cloud-init.yaml — the SAME file 1b.3 used."
  type        = string
  default     = ""
}
