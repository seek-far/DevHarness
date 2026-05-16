variable "os_cloud" {
  description = "clouds.yaml entry to authenticate with. DevStack: 'devstack-admin' (admin project) or 'devstack' (demo)."
  type        = string
  default     = "devstack-admin"
}

# Passthrough of the IaaS contract. Defaults are 1b.2-appropriate (cirros proof
# of the TF->IaaS path); 1b.3 overrides flavor/image/ssh_user via tfvars.
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
  default = "m1.tiny"
}

variable "worker_flavor" {
  type    = string
  default = "m1.tiny"
}

variable "image_name" {
  type    = string
  default = "cirros-0.6.2-x86_64-disk"
}

variable "ssh_public_key" {
  description = "Public key string injected into nodes. No default: must be supplied (tfvars / -var) so the key is never guessed."
  type        = string
}

variable "ssh_user" {
  type    = string
  default = "cirros"
}

variable "network_cidr" {
  type    = string
  default = "10.10.0.0/24"
}

variable "external_network" {
  type    = string
  default = "public"
}

variable "cloud_init" {
  description = "Raw cloud-init user-data string for every node. Usually left empty; prefer cloud_init_file."
  type        = string
  default     = ""
}

variable "cloud_init_file" {
  description = "Path (relative to this root) to a cloud-init file; if set, its contents win over var.cloud_init. 1b.3 points this at ../../k8s-bootstrap/cloud-init.yaml."
  type        = string
  default     = ""
}
