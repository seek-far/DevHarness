# OpenStack realization of the IaaS contract.
#
# Concept map (for the 1b learning focus / phase-2 AWS port):
#   keypair        ~ EC2 key pair
#   network/subnet ~ VPC + subnet
#   router         ~ internet gateway + route table
#   secgroup       ~ security group
#   floating IP    ~ Elastic IP
#   compute_instance ~ EC2 instance

# External network is pre-existing (DevStack 'public'); we look it up, not own it.
data "openstack_networking_network_v2" "external" {
  name = var.external_network
}

resource "openstack_compute_keypair_v2" "this" {
  name       = "${var.name_prefix}-key"
  public_key = var.ssh_public_key
}

# --- Private node network (VPC analog) ---------------------------------------
resource "openstack_networking_network_v2" "this" {
  name = "${var.name_prefix}-net"
}

resource "openstack_networking_subnet_v2" "this" {
  name            = "${var.name_prefix}-subnet"
  network_id      = openstack_networking_network_v2.this.id
  cidr            = var.network_cidr
  ip_version      = 4
  dns_nameservers = var.dns_nameservers
}

# Router connects the private subnet out to the external network (IGW analog).
resource "openstack_networking_router_v2" "this" {
  name                = "${var.name_prefix}-router"
  external_network_id = data.openstack_networking_network_v2.external.id
}

resource "openstack_networking_router_interface_v2" "this" {
  router_id = openstack_networking_router_v2.this.id
  subnet_id = openstack_networking_subnet_v2.this.id
}

# --- Security group ----------------------------------------------------------
# Ingress default-deny; we open exactly what 1b needs. k8s ports are opened now
# so 1b.3 doesn't have to re-touch this module. Egress is allow-all by default
# in OpenStack security groups (matches what apt/k8s pulls need).
resource "openstack_networking_secgroup_v2" "this" {
  name        = "${var.name_prefix}-sg"
  description = "SDLCMA 1b node security group"
}

locals {
  # protocol, from, to, description
  ingress_rules = {
    ssh      = { proto = "tcp", from = 22, to = 22, desc = "SSH" }
    kube_api = { proto = "tcp", from = 6443, to = 6443, desc = "k8s API server (1b.3)" }
    kubelet  = { proto = "tcp", from = 10250, to = 10250, desc = "kubelet (1b.3)" }
    nodeport = { proto = "tcp", from = 30000, to = 32767, desc = "k8s NodePort range (1b.4)" }
  }
}

resource "openstack_networking_secgroup_rule_v2" "ingress" {
  for_each          = local.ingress_rules
  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = each.value.proto
  port_range_min    = each.value.from
  port_range_max    = each.value.to
  remote_ip_prefix  = "0.0.0.0/0"
  description       = each.value.desc
  security_group_id = openstack_networking_secgroup_v2.this.id
}

resource "openstack_networking_secgroup_rule_v2" "icmp" {
  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = "icmp"
  remote_ip_prefix  = "0.0.0.0/0"
  description       = "ICMP (ping for smoke checks)"
  security_group_id = openstack_networking_secgroup_v2.this.id
}

# Nodes fully trust each other within the cluster subnet. Without this the CNI
# overlay (flannel VXLAN, UDP 8472) is dropped between nodes, so any cross-node
# pod traffic — incl. CoreDNS when it lands on a different node than its
# clients — silently times out. Scoping to network_cidr (not 0.0.0.0/0) keeps
# it to the node group; this is the same intra-node-group trust posture EKS/GKE
# node security groups use, and the right model to carry into phase-2 AWS.
resource "openstack_networking_secgroup_rule_v2" "intra_node" {
  direction         = "ingress"
  ethertype         = "IPv4"
  remote_ip_prefix  = var.network_cidr
  description       = "All traffic between cluster nodes (CNI overlay incl. flannel VXLAN 8472); protocol omitted = any"
  security_group_id = openstack_networking_secgroup_v2.this.id
}

# --- Instances ---------------------------------------------------------------
resource "openstack_compute_instance_v2" "control" {
  count           = var.control_count
  name            = "${var.name_prefix}-control-${count.index}"
  flavor_name     = var.control_flavor
  image_name      = var.image_name
  key_pair        = openstack_compute_keypair_v2.this.name
  security_groups = [openstack_networking_secgroup_v2.this.name]
  user_data       = var.cloud_init != "" ? var.cloud_init : null

  network {
    uuid = openstack_networking_network_v2.this.id
  }

  # Boot needs only the network; routing/floating-IP reachability needs the
  # router interface attached first.
  depends_on = [openstack_networking_router_interface_v2.this]
}

resource "openstack_compute_instance_v2" "worker" {
  count           = var.worker_count
  name            = "${var.name_prefix}-worker-${count.index}"
  flavor_name     = var.worker_flavor
  image_name      = var.image_name
  key_pair        = openstack_compute_keypair_v2.this.name
  security_groups = [openstack_networking_secgroup_v2.this.name]
  user_data       = var.cloud_init != "" ? var.cloud_init : null

  network {
    uuid = openstack_networking_network_v2.this.id
  }

  depends_on = [openstack_networking_router_interface_v2.this]
}

# Control nodes get a floating IP (Elastic IP analog) so the upper layers can
# reach the kube API. Workers stay private (reached via control / in-cluster).
#
# Provider v3 removed openstack_compute_floatingip_associate_v2; the v3 way is
# networking-side association by port. We resolve each instance's port via the
# port data source (filtered by device_id + our network).
resource "openstack_networking_floatingip_v2" "control" {
  count = var.control_count
  pool  = var.external_network
}

data "openstack_networking_port_v2" "control" {
  count      = var.control_count
  device_id  = openstack_compute_instance_v2.control[count.index].id
  network_id = openstack_networking_network_v2.this.id
}

resource "openstack_networking_floatingip_associate_v2" "control" {
  count       = var.control_count
  floating_ip = openstack_networking_floatingip_v2.control[count.index].address
  port_id     = data.openstack_networking_port_v2.control[count.index].id
}
