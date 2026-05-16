# AWS realization of the IaaS contract. 1:1 with ../openstack/main.tf:
#   VPC+subnet      ~ network/subnet      IGW+route table ~ router
#   security group  ~ security group      key pair        ~ keypair
#   EIP             ~ floating IP          EC2 instance    ~ compute instance
# The 1b.4 lesson is carried over: an intra-node "self" rule so the CNI
# overlay (flannel VXLAN) is never dropped between nodes.

# image_name is an AMI name glob (contract stays a string).
data "aws_ami" "node" {
  most_recent = true
  owners      = ["099720109477", "amazon"] # Canonical, Amazon

  filter {
    name   = "name"
    values = [var.image_name]
  }
  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

resource "aws_key_pair" "this" {
  key_name   = "${var.name_prefix}-key"
  public_key = var.ssh_public_key
}

# --- VPC + subnet (network/subnet analog) ------------------------------------
resource "aws_vpc" "this" {
  cidr_block           = var.network_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = "${var.name_prefix}-vpc" }
}

resource "aws_vpc_dhcp_options" "this" {
  domain_name_servers = var.dns_nameservers
  tags                = { Name = "${var.name_prefix}-dhcp" }
}

resource "aws_vpc_dhcp_options_association" "this" {
  vpc_id          = aws_vpc.this.id
  dhcp_options_id = aws_vpc_dhcp_options.this.id
}

resource "aws_subnet" "this" {
  vpc_id                  = aws_vpc.this.id
  cidr_block              = var.network_cidr
  map_public_ip_on_launch = false
  tags                    = { Name = "${var.name_prefix}-subnet" }
}

# Internet gateway + route (router analog).
resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = "${var.name_prefix}-igw" }
}

resource "aws_route_table" "this" {
  vpc_id = aws_vpc.this.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }
  tags = { Name = "${var.name_prefix}-rt" }
}

resource "aws_route_table_association" "this" {
  subnet_id      = aws_subnet.this.id
  route_table_id = aws_route_table.this.id
}

# --- Security group ----------------------------------------------------------
resource "aws_security_group" "this" {
  name        = "${var.name_prefix}-sg"
  description = "SDLCMA 1b node security group"
  vpc_id      = aws_vpc.this.id
}

locals {
  ingress_rules = {
    ssh      = { from = 22, to = 22, desc = "SSH" }
    kube_api = { from = 6443, to = 6443, desc = "k8s API server" }
    kubelet  = { from = 10250, to = 10250, desc = "kubelet" }
    nodeport = { from = 30000, to = 32767, desc = "k8s NodePort range" }
  }
}

resource "aws_security_group_rule" "ingress" {
  for_each          = local.ingress_rules
  type              = "ingress"
  protocol          = "tcp"
  from_port         = each.value.from
  to_port           = each.value.to
  cidr_blocks       = ["0.0.0.0/0"]
  description       = each.value.desc
  security_group_id = aws_security_group.this.id
}

resource "aws_security_group_rule" "icmp" {
  type              = "ingress"
  protocol          = "icmp"
  from_port         = -1
  to_port           = -1
  cidr_blocks       = ["0.0.0.0/0"]
  description       = "ICMP"
  security_group_id = aws_security_group.this.id
}

# 1b.4 lesson: nodes must fully trust each other or the CNI overlay (flannel
# VXLAN UDP 8472) is dropped cross-node. AWS idiom = a self-referencing rule.
resource "aws_security_group_rule" "intra_node" {
  type                     = "ingress"
  protocol                 = "-1"
  from_port                = 0
  to_port                  = 0
  source_security_group_id = aws_security_group.this.id
  description              = "All traffic between cluster nodes (CNI overlay incl. flannel VXLAN 8472)"
  security_group_id        = aws_security_group.this.id
}

resource "aws_security_group_rule" "egress_all" {
  type              = "egress"
  protocol          = "-1"
  from_port         = 0
  to_port           = 0
  cidr_blocks       = ["0.0.0.0/0"]
  description       = "All egress (apt / image pulls / LLM / GitLab)"
  security_group_id = aws_security_group.this.id
}

# --- Instances ---------------------------------------------------------------
resource "aws_instance" "control" {
  count                  = var.control_count
  ami                    = data.aws_ami.node.id
  instance_type          = var.control_flavor
  key_name               = aws_key_pair.this.key_name
  subnet_id              = aws_subnet.this.id
  vpc_security_group_ids = [aws_security_group.this.id]
  user_data              = var.cloud_init != "" ? var.cloud_init : null
  tags                   = { Name = "${var.name_prefix}-control-${count.index}" }
}

resource "aws_instance" "worker" {
  count                  = var.worker_count
  ami                    = data.aws_ami.node.id
  instance_type          = var.worker_flavor
  key_name               = aws_key_pair.this.key_name
  subnet_id              = aws_subnet.this.id
  vpc_security_group_ids = [aws_security_group.this.id]
  user_data              = var.cloud_init != "" ? var.cloud_init : null
  tags                   = { Name = "${var.name_prefix}-worker-${count.index}" }
}

# Control nodes get an Elastic IP (floating-IP analog).
resource "aws_eip" "control" {
  count    = var.control_count
  instance = aws_instance.control[count.index].id
  domain   = "vpc"
  tags     = { Name = "${var.name_prefix}-control-eip-${count.index}" }
}
