---
name: launch-eks
description: Provision a Karpenter-backed EKS cluster on AWS for Ray + KubeRay workloads. Takes a YAML spec defining a list of node pools (mixed CPU + GPU, each with a max), all of which scale to zero in steady state. KubeRay operator and (for AL2023 GPU pools) the NVIDIA device plugin are preinstalled. Use when the user wants to spin up, list, switch kubeconfig to, validate, or tear down an EKS cluster for KubeRay. AWS-tag-driven, no terraform, no local state.
---

# launch-eks

YAML-driven, Karpenter-backed EKS cluster manager for Ray + KubeRay. Discovery is by AWS tag (`Type=ray-eks` + `Owner=<user>`); no local state files.

**Steady-state cost**: only the EKS control plane ($0.10/hr) + a single-node system pool (~$30/mo for `m7i.large`) bill when idle. User-defined CPU/GPU node pools become Karpenter `NodePool` CRDs and stay at zero nodes until a Ray pod requests resources.

## Commands

```sh
SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
$SKILL_DIR/scripts/launch_eks.py launch -f cluster.yaml [--smoke-test POOL] [--dry-run]
$SKILL_DIR/scripts/launch_eks.py list
$SKILL_DIR/scripts/launch_eks.py kubeconfig NAME
$SKILL_DIR/scripts/launch_eks.py validate NAME [--smoke-test POOL]
$SKILL_DIR/scripts/launch_eks.py shutdown NAME [-y]
```

| Command | What it does |
|---|---|
| `launch` | Renders artifacts from `cluster.yaml`, creates the cluster via `eksctl`, installs Karpenter + KubeRay, applies user NodePools, validates between phases. See the timing table below. `--smoke-test POOL` adds a GPU end-to-end check. |
| `list` | Queries AWS by tag and prints clusters. |
| `kubeconfig` | Runs `aws eks update-kubeconfig` so `kubectl` points at the named cluster. |
| `validate` | Runs the same checks `launch` runs. With `--smoke-test POOL`, triggers Karpenter to provision the pool and runs `nvidia-smi`. |
| `shutdown` | **Drains Karpenter NodePools + EC2NodeClasses first** (so its EC2 instances terminate cleanly), then `eksctl delete cluster`, then deletes the Karpenter IAM CF stack. |

## Pre-launch: pick a region with actual GPU capacity

Before `launch`, especially during demand spikes (newer instance families, end of fiscal quarter, etc.), check that AWS has on-demand capacity in your target region/AZ. AWS exposes no read-only "is on-demand available right now" API — `describe-instance-type-offerings` only confirms a type is *supported*, not *currently available*, and `run-instances --dry-run` only checks IAM. The closest signal is **`get-spot-placement-scores`** (designed for spot but reflects overall pool depth):

```sh
aws ec2 get-spot-placement-scores \
  --instance-types g6e.12xlarge g6e.24xlarge g6e.48xlarge \
  --target-capacity 1 \
  --target-capacity-unit-type units \
  --region-names us-east-1 us-east-2 us-west-2 eu-west-1 eu-central-1 ap-northeast-1 \
  --single-availability-zone \
  --region us-east-1 \
  --output json | jq '.SpotPlacementScores | sort_by(-.Score)'
```

Returns a 1–10 score per `(region, AvailabilityZoneId)`. Pick the highest scorer. Rule of thumb: 7+ usually works, ≤3 means expect `InsufficientInstanceCapacity` errors. The scores are forecasts, not guarantees.

Without this check, the failure mode is Karpenter repeatedly creating + terminating NodeClaims as `RunInstances` returns `InsufficientInstanceCapacity` — visible only by tailing the Karpenter logs, easy to mistake for a config bug. Run the query first; pin the cluster with `availability_zones:` in `cluster.yaml` once you have the data.

## NAT and EIP quotas (often hit before instance quotas)

Cluster creation provisions a fresh VPC with 3 NAT gateways (one per AZ) by default — each consuming an EIP. If the AWS account is near its per-AZ NAT quota (default 5/AZ) or the regional EIP quota (default 5/region for older accounts, often 15+ for active ones), eksctl rolls back the cluster stack with `ServiceLimitExceeded` or "maximum number of addresses has been reached".

Pre-flight checks worth running:

```sh
# NAT gateways per AZ
aws ec2 describe-nat-gateways --region $REGION \
  --filter Name=state,Values=available,pending \
  --query 'NatGateways[].SubnetId' --output text \
| tr '\t' '\n' | xargs -I{} aws ec2 describe-subnets --region $REGION \
    --subnet-ids {} --query 'Subnets[0].AvailabilityZone' --output text \
| sort | uniq -c

# Region EIP usage
aws ec2 describe-addresses --region $REGION --query 'length(Addresses[])' --output text
aws service-quotas get-service-quota --region $REGION \
  --service-code ec2 --quota-code L-0263D0A3 --query 'Quota.Value' --output text

# Find unattached (orphan) EIPs that may be reclaimable
aws ec2 describe-addresses --region $REGION \
  --query 'Addresses[?!AssociationId].[PublicIp,AllocationId,Tags[0].Value]' --output table
```

Mitigations:
- Set `vpc.nat: Single` to drop NAT footprint from 3 → 1.
- Use `availability_zones:` to skip AZs that are at their per-AZ NAT cap.
- Release orphan EIPs (with the owner's blessing if shared infra).

## One-time setup

1. **Tools** on `PATH`: `aws`, `eksctl`, `kubectl`, `helm`, `yq`. Install: `brew install eksctl kubectl helm yq` (awscli separately).
2. **Config**: first run auto-creates `~/.config/launch-eks/config.json` with defaults derived from env. Fields: `aws_profile`, `aws_region`, `owner`. Edit if needed.
3. **AWS auth**: `aws sts get-caller-identity --profile <your-profile>` must work. The IAM principal needs permissions to create EKS clusters, IAM roles, VPCs, subnets, security groups, CloudFormation stacks, and SQS queues (eksctl + the Karpenter CF template provision all of these).

## Cluster spec (`cluster.yaml`) schema

```yaml
name: kuberay-prod                # required
region: us-west-2                 # optional; falls back to ~/.config/launch-eks/config.json
version: "1.31"                   # optional; defaults to current EKS GA minus one minor
owner: kourosh                    # optional; falls back to config

vpc:                              # optional
  nat: HighlyAvailable            # HighlyAvailable (default, 1 NAT/AZ) | Single (1 NAT total) | Disable

availability_zones:               # optional; pin cluster to specific AZs (≥2 required)
  - us-east-2a                    # useful when other AZs are NAT/EIP/instance-quota saturated
  - us-east-2b
  - us-east-2c

system:                           # optional; defaults shown
  instance_type: m7i.large        # always-on system node — hosts kuberay + karpenter + kube-system
  min: 1                          # min must be >= 1
  max: 2

disruption:                       # optional global defaults applied per-pool unless overridden
  consolidation_policy: WhenEmpty           # WhenEmpty | WhenEmptyOrUnderutilized
  consolidate_after: 30s
  expire_after: 720h

nodepools:                        # list — at least one entry expected
  - name: cpu-small
    instance_types: [m7i.xlarge, m7i.2xlarge, m6i.xlarge]
    max: 20                       # absolute ceiling on pod-driven scale-up
    capacity_types: [on-demand]   # default if omitted; spot allowed
    disk_gb: 100                  # gp3, encrypted, 250 MB/s throughput
    ami_family: Bottlerocket      # default; AL2023 also supported
    labels: { ray-role: cpu-worker }
    taints: []                    # array of {key, value, effect}
    # gpu: false                  # implicit when omitted

  - name: gpu-l4
    instance_types: [g6.xlarge, g6.2xlarge, g6.4xlarge, g6.12xlarge]
    max: 4
    disk_gb: 300
    gpu: true                     # ⇒ Bottlerocket NVIDIA AMI, implicit nvidia.com/gpu taint
    labels: { ray-role: gpu-worker, accelerator: nvidia-l4 }
```

Validation enforces: name required and not `system`; `system.min >= 1`; `instance_types` non-empty; if `gpu: true`, every `instance_types` entry must be a GPU family (`g`/`p`/`gr`/`trn`/`inf` — recognized by the leading alpha run before the first digit, so `g6`, `g6e`, `p5`, `p5e`, `p5en`, `gr6`, `trn1`, `trn1n`, `inf2` all match); `max > 0`; capacity types ⊆ `{on-demand, spot}`; if `availability_zones` is set, requires ≥2 entries. Per-pool `min: N > 0` is allowed but emits a warning that it breaks scale-to-zero billing.

See `examples/{simple-cpu,cpu-and-gpu,multi-pool}.yaml` for worked configurations.

## Cluster shape

Terminology: **system pool** = the EKS-managed node group that hosts operators (always on). **User pools** = entries under `nodepools:` in the YAML, each rendered as a Karpenter `NodePool` + `EC2NodeClass` (scale-to-zero).

```
EKS cluster (control plane: $0.10/hr always)
├── system pool  ← EKS-managed NG, always-on, 1× m7i.large (~$30/mo)
│     hosts: kuberay-operator, karpenter, coredns, metrics-server, kube-system
│     label: node-role.kuberay.io/system=true
│     taint:  node-role.kuberay.io/system=true:PreferNoSchedule
└── User pools  ← Karpenter NodePools (one per entry in nodepools:, all min=0)
      ├── EC2NodeClass: AMI/disk/subnets/SG selectors
      └── NodePool:     instance-type requirements + taints + labels + cpu limits
```

User pods land on Karpenter-provisioned EC2 instances; the system pool only runs control-plane operators.

## Defaults

The YAML schema above documents the per-pool defaults (capacity, disk, AMI family, disruption). A few that aren't obvious from the schema:

- **EKS version policy**: tracks one minor below current EKS GA. Override with `version:` in the YAML.
- **AMI choice rationale**: Bottlerocket for both CPU and GPU pools (smaller, faster boot, GPU variant ships the NVIDIA driver + device plugin in-AMI). AL2023 supported for compatibility but loses the in-AMI driver.
- **GPU device plugin install**: only deployed if any pool uses `ami_family: AL2023` AND `gpu: true`. Bottlerocket pools skip it (driver is in the AMI).
- **Idle-node lifetime**: ~2 min after last pod (consequence of `consolidateAfter: 30s`).
- **Pinned chart versions**: Karpenter, KubeRay operator, and the NVIDIA device plugin are pinned in `scripts/launch_eks.py`. Treat those pins as the source of truth — they will drift faster than this doc.

## Validation

`launch` runs validation between phases. The `validate` subcommand re-runs the same checks against an existing cluster.

| Check | Phase | Why |
|---|---|---|
| All nodes `Ready` | baseline + full | Cluster is actually usable. |
| `kube-system` pods all `Running`/`Succeeded` | baseline + full | Catches addon failures. |
| `aws-node` and `kube-proxy` DaemonSets fully scheduled | baseline + full | Pod networking + service routing wired up. |
| `karpenter` Deployment Ready in `karpenter` ns | full | Autoscaler is alive. |
| Each `NodePool` `.status.conditions[type=Ready]=True` | full | Karpenter accepted the pool spec; ready to provision on demand. |
| `kuberay-operator` Deployment Ready | full | Operator can reconcile RayCluster/RayService CRs. |
| **GPU smoke test** (opt-in via `--smoke-test POOL`) | post-launch | Trigger-and-verify: applies a Job requesting `nvidia.com/gpu: 1`, watches Karpenter provision a node, asserts `nvidia-smi` succeeds in the container, deletes the Job, watches the node terminate. End-to-end proves: AMI passthrough + device plugin + Karpenter provisioning + scale-back-to-zero. Prints per-phase timings. |

The smoke test is **opt-in** because it costs ~one EC2-instance-minute and adds 4–8 minutes to launch. Run it explicitly when you change AMIs, GPU instance types, or want a contract test.

## Honest cluster-creation timing

| Phase | Time |
|---|---|
| `eksctl create cluster` (control plane + system NG) | ~15–18 min |
| Subnet + SG tagging for Karpenter | ~5s |
| Karpenter IAM CF stack deploy | ~2–3 min |
| `eksctl create iamidentitymapping` for Karpenter node role | ~10s |
| `helm install karpenter` (waits for Deployment Ready) | ~1–2 min |
| Apply NodePools + EC2NodeClasses, wait Ready | ~30s |
| `helm install kuberay-operator` (waits for Deployment Ready) | ~1–2 min |
| Final validate | ~30s |
| **Total without smoke test** | **~22–28 min** |
| `+ --smoke-test gpu-l4` | +4–8 min |

**Per-scale-up cold latency** (idle cluster → first user pod runs):
- CPU pool, small image: ~3 min.
- GPU pool, `nvidia/cuda` base image: ~4 min.
- GPU pool, `ray-llm` 13 GB image first pull on a fresh node: ~6–9 min (image pull dominates).
- Subsequent pods on the same warm node: seconds.

Cold start is the cost of scale-to-zero. Mitigations:
- Set `min: 1` on a pool (warning: that pool now bills 24/7).
- Pre-warm by `kubectl scale --replicas=1` of a pause-image deployment before a demo.
- Use smaller images.

## Examples

CPU-only:
```sh
$SKILL_DIR/scripts/launch_eks.py launch -f $SKILL_DIR/examples/simple-cpu.yaml
```

CPU + GPU with smoke test:
```sh
$SKILL_DIR/scripts/launch_eks.py launch \
  -f $SKILL_DIR/examples/cpu-and-gpu.yaml \
  --smoke-test gpu-l4
```

Multi-pool production-ish:
```sh
$SKILL_DIR/scripts/launch_eks.py launch -f $SKILL_DIR/examples/multi-pool.yaml
```

Dry-run (print all rendered configs, do not call AWS):
```sh
$SKILL_DIR/scripts/launch_eks.py launch -f cluster.yaml --dry-run
```

Switch kubectl context:
```sh
$SKILL_DIR/scripts/launch_eks.py kubeconfig kuberay-prod
kubectl get nodes
kubectl get nodepools
```

Tear down (drains Karpenter first):
```sh
$SKILL_DIR/scripts/launch_eks.py shutdown kuberay-prod -y
```

## Notes / gotchas

- **GPU taint requires tolerations on Ray pods.** When you write a RayCluster/RayService spec for GPU workers in a `gpu: true` pool, add to the worker pod template:
  ```yaml
  tolerations:
    - { key: nvidia.com/gpu, operator: Equal, value: "true", effect: NoSchedule }
  nodeSelector:
    ray-eks-pool: gpu-l4    # or whatever your pool name is
  ```
  Otherwise GPU pods stay Pending and Karpenter never wakes up.

- **Pre-shutdown CRD cleanup is mandatory.** Karpenter-provisioned EC2 instances do not get cleaned up by `eksctl delete cluster` alone — they are not part of any managed node group. `shutdown` deletes all NodePools/EC2NodeClasses first to drain them. If you do `eksctl delete cluster` directly, run `aws ec2 describe-instances --filters Name=tag:karpenter.sh/nodepool,Values=*` afterwards and manually terminate any survivors.

- **System pool is single-node by default (no HA).** Acceptable for dev. If the system node dies, Karpenter and the KubeRay operator restart on the replacement (`max: 2` lets the managed NG self-heal). For production, set `system.min: 2`.

- **AL2 sunset.** AWS stopped publishing EKS-optimized AL2 AMIs on 2025-11-26. Default AMI family is Bottlerocket; AL2023 is the AL alternative. The skill does not support AL2.

- **Don't enable `WhenEmptyOrUnderutilized` casually.** That mode lets Karpenter drain a node hosting a Ray head to repack — costs you cluster reset and any in-flight actor state. Stick with `WhenEmpty` unless you've annotated Ray heads with `karpenter.sh/do-not-disrupt: "true"`.

- **EKS Auto Mode is incompatible.** This skill installs self-managed Karpenter; it would conflict with Auto Mode's bundled Karpenter. Don't enable Auto Mode on these clusters.

- **First image pull on a fresh GPU node is slow** (~5+ min for `ray-llm`-sized images). gp3 throughput defaults to 250 MB/s in this skill to keep kubelet's `image-pull-progress-deadline` happy; if you bump to massive images, raise it further in the EC2NodeClass `blockDeviceMappings`.

- **Region**: defaults to `us-west-2` from config. Override per-launch by setting `region:` in the YAML.
