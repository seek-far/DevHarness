# Auth comes from clouds.yaml (copied to ~/.config/openstack/clouds.yaml,
# readable by the terraform-running user). No secrets in the repo. The DevStack
# password is a throwaway dev cred and stays only in that out-of-repo file.
provider "openstack" {
  cloud = var.os_cloud
}
