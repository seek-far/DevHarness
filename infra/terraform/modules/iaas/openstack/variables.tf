# Contract inputs (see ../CONTRACT.md). Names here are provider-neutral on
# purpose: the future aws/ module declares the same set.

variable "name_prefix" {
  description = "Prefix for all created resources."
  type        = string
  default     = "sdlcma-1b"
}

variable "control_count" {
  description = "Number of k8s control-plane nodes."
  type        = number
  default     = 1
}

variable "worker_count" {
  description = "Number of k8s worker nodes."
  type        = number
  default     = 1
}

variable "control_flavor" {
  description = "Flavor for control nodes. 1b.2 proves provisioning with cirros; 1b.3 bumps this to a k8s-capable size (>=2 vCPU / 2 GB)."
  type        = string
  default     = "m1.tiny"
}

variable "worker_flavor" {
  description = "Flavor for worker nodes."
  type        = string
  default     = "m1.tiny"
}

variable "image_name" {
  description = "Base OS image. 1b.2 default is the DevStack-bundled cirros (proves the TF->IaaS path); 1b.3 overrides with an Ubuntu cloud image."
  type        = string
  default     = "cirros-0.6.2-x86_64-disk"
}

variable "ssh_public_key" {
  description = "Public key injected into every node via the provider keypair."
  type        = string
}

variable "ssh_user" {
  description = "Login user for the chosen image (image-specific, not a cloud concept). cirros -> 'cirros'; Ubuntu cloud image -> 'ubuntu'."
  type        = string
  default     = "cirros"
}

variable "network_cidr" {
  description = "CIDR for the private node network."
  type        = string
  default     = "10.10.0.0/24"
}

variable "dns_nameservers" {
  description = "Resolvers for the node subnet (internet egress for apt etc. in 1b.3)."
  type        = list(string)
  default     = ["8.8.8.8", "1.1.1.1"]
}

variable "external_network" {
  description = "Name of the provider's external network for floating IPs. DevStack default is 'public'."
  type        = string
  default     = "public"
}

variable "cloud_init" {
  description = "Cloud-init user-data for every node at first boot (OpenStack user_data / AWS user_data). Empty = none. 1b.3 uses this to pre-install kubeadm prerequisites."
  type        = string
  default     = ""
}
