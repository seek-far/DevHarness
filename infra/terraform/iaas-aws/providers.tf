# `terraform validate` does not authenticate or call AWS, so no credentials
# are needed to lock the contract. Region is config, not a secret. Phase 2
# supplies real creds out-of-band (env / profile), same no-secrets-in-repo
# stance as the openstack root's clouds.yaml.
provider "aws" {
  region = var.aws_region
}
