# IaaS module contract (phase 1b)

This is the **provider-neutral seam** for the SDLCMA deployment. The upper
layers (k8s install in 1b.3, Helm/app in 1b.4, and the existing 1a
`helm_release` root) depend only on the names below — never on a specific
cloud provider. Phase 2 swaps the *implementation* (`openstack/` → `aws/`)
without the consumers changing.

Any implementation under `modules/iaas/<provider>/` MUST expose exactly these
input variables and output names. Add provider-specific knobs only as
**optional** variables with defaults, so the common call site stays portable.

## Inputs (required contract)

| Variable | Type | Meaning |
|---|---|---|
| `name_prefix` | string | Prefix for all created resources (isolation / cleanup). |
| `control_count` | number | Number of k8s control-plane nodes. |
| `worker_count` | number | Number of k8s worker nodes. |
| `control_flavor` | string | Instance size for control nodes (provider's flavor/instance-type id). |
| `worker_flavor` | string | Instance size for worker nodes. |
| `image_name` | string | Base OS image (cirros for 1b.2 proof; Ubuntu cloud image from 1b.3). |
| `ssh_public_key` | string | Public key injected into every node. |
| `ssh_user` | string | Login user for the chosen image (image-specific; not a cloud concept). |
| `network_cidr` | string | CIDR for the private node network. |
| `dns_nameservers` | list(string) | Resolvers for the node subnet (internet egress for apt etc.). |
| `external_network` | string | Name of the provider's external/public network for floating/public IPs. |

## Optional inputs

These have defaults; an implementation MUST accept them but the common call
site can omit them. Provider-neutral by design.

| Variable | Type | Default | Meaning |
|---|---|---|---|
| `cloud_init` | string | `""` | Cloud-init user-data injected into every node at first boot. Maps to OpenStack `user_data` / AWS `user_data`. Empty = no user-data. Used from 1b.3 to pre-install the kubeadm prerequisites. |

## Outputs (required contract)

| Output | Meaning |
|---|---|
| `control_ips` | Publicly reachable IPs of control nodes (floating/elastic). |
| `worker_ips` | Reachable IPs of worker nodes (private is fine; reachable from control). |
| `ssh_user` | Echo of the login user, so consumers don't re-derive it. |
| `network_id` | Provider id of the private node network. |
| `kubeapi_endpoint` | `https://<control_ips[0]>:6443` — where 1b.3 will stand up the API. |

Phase-2 AWS note: `external_network` maps to "allocate Elastic IPs from the
account's public pool"; `network_cidr` maps to the VPC/subnet CIDR. Names stay;
the `aws/` implementation reinterprets them in AWS terms.
