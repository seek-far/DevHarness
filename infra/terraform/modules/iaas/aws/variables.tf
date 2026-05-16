# IDENTICAL contract names to ../openstack/variables.tf — that is the locked
# seam. Only defaults/descriptions are reinterpreted in AWS terms. The
# contract-parity test asserts the var/output name SETS match byte-for-byte.

variable "name_prefix" {
  description = "Prefix + Name tag for all created resources."
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
  description = "Control node size. Provider-specific value, contract-neutral name: AWS instance type (e.g. t3.small = 2 vCPU / 2 GB, kubeadm-capable)."
  type        = string
  default     = "t3.small"
}

variable "worker_flavor" {
  description = "Worker node size (AWS instance type)."
  type        = string
  default     = "t3.small"
}

variable "image_name" {
  description = "Base OS image. In AWS this is an AMI *name* glob, resolved via a data source (keeps the contract a string, like the OpenStack glance image name)."
  type        = string
  default     = "ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"
}

variable "ssh_public_key" {
  description = "Public key injected into every node (aws_key_pair)."
  type        = string
}

variable "ssh_user" {
  description = "Login user for the chosen image (image-specific). Ubuntu AMI -> 'ubuntu'."
  type        = string
  default     = "ubuntu"
}

variable "network_cidr" {
  description = "CIDR for the VPC + node subnet."
  type        = string
  default     = "10.10.0.0/24"
}

variable "dns_nameservers" {
  description = "Resolvers for the subnet (AWS: VPC DHCP option set domain-name-servers)."
  type        = list(string)
  default     = ["8.8.8.8", "1.1.1.1"]
}

variable "external_network" {
  description = "OpenStack used a named external network for floating IPs. In AWS, public addressing comes from the account's public pool (EIPs) + an Internet Gateway, so this carries no value here — declared only to keep the contract identical across implementations."
  type        = string
  default     = ""
}

variable "cloud_init" {
  description = "Cloud-init user-data for every node at first boot (AWS user_data). Empty = none. Optional contract input."
  type        = string
  default     = ""
}
