"""Renderers for every YAML/JSON artifact launch_eks.py applies.

Kept separate so cmd_launch in launch_eks.py reads as control flow rather than a
wall of f-strings.

All artifacts are rendered as JSON (which is valid YAML 1.2 — kubectl, eksctl,
and helm accept JSON wherever they accept YAML). No PyYAML dependency.
"""
from __future__ import annotations

import json
from typing import Any

# ---------- pinned versions / constants ----------

EKS_VERSION_DEFAULT = "1.31"
KARPENTER_CHART_VERSION = "1.2.1"           # last 1.x compatible with eksctl-bundled CRDs path
KUBERAY_OPERATOR_CHART_VERSION = "1.2.2"
NVIDIA_PLUGIN_VERSION = "v0.17.1"
NVIDIA_PLUGIN_URL = (
    f"https://raw.githubusercontent.com/NVIDIA/k8s-device-plugin/"
    f"{NVIDIA_PLUGIN_VERSION}/deployments/static/nvidia-device-plugin.yml"
)
GPU_SMOKE_IMAGE = "nvidia/cuda:12.4.1-base-ubuntu22.04"

TAG_TYPE = "ray-eks"
SYSTEM_LABEL_KEY = "node-role.kuberay.io/system"
SYSTEM_LABEL_VALUE = "true"
SYSTEM_TAINT_EFFECT = "PreferNoSchedule"
POOL_LABEL_KEY = "ray-eks-pool"

GPU_FAMILIES = ("g", "p", "gr", "trn", "inf")

# Karpenter IAM CloudFormation template — owned by the Karpenter team, pinned per
# release. Provisions: KarpenterControllerRole, KarpenterNodeRole + instance profile,
# SQS interruption queue, EventBridge rules.
KARPENTER_IAM_CF_URL = (
    f"https://raw.githubusercontent.com/aws/karpenter-provider-aws/"
    f"v{KARPENTER_CHART_VERSION}/website/content/en/preview/getting-started/"
    f"getting-started-with-karpenter/cloudformation.yaml"
)


def is_gpu_instance_type(t: str) -> bool:
    family = t.split(".", 1)[0]
    # Keep only the leading alpha run, stopping at the first digit so post-digit
    # suffixes don't leak into the family. Examples: g6 -> g, g6e -> g,
    # p5 -> p, p5en -> p, gr6 -> gr, trn1 -> trn, inf2 -> inf.
    base = ""
    for c in family:
        if c.isdigit():
            break
        base += c
    return base in GPU_FAMILIES


# ---------- eksctl ClusterConfig ----------

def render_eksctl_config(spec: dict[str, Any]) -> str:
    """Render the eksctl ClusterConfig YAML.

    Includes ONLY the system managed node group. User pools become Karpenter
    NodePools applied later via kubectl, not managed node groups.
    """
    name = spec["name"]
    region = spec["region"]
    version = spec.get("version", EKS_VERSION_DEFAULT)
    owner = spec["owner"]
    sysp = spec["system"]

    # Optional VPC config. Default eksctl behavior is one NAT gateway per AZ
    # (HighlyAvailable). For dev clusters in NAT-quota-constrained accounts,
    # `vpc: { nat: Single }` provisions one shared NAT for the whole VPC.
    vpc_spec = spec.get("vpc") or {}
    nat_mode = vpc_spec.get("nat", "HighlyAvailable")
    if nat_mode not in ("HighlyAvailable", "Single", "Disable"):
        raise ValueError(
            f"vpc.nat must be HighlyAvailable | Single | Disable; got {nat_mode!r}"
        )

    config = {
        "apiVersion": "eksctl.io/v1alpha5",
        "kind": "ClusterConfig",
        "metadata": {
            "name": name,
            "region": region,
            "version": str(version),
            "tags": {"Type": TAG_TYPE, "Owner": owner},
        },
        "iam": {"withOIDC": True},
        **(
            {"availabilityZones": spec["availability_zones"]}
            if spec.get("availability_zones")
            else {}
        ),
        "vpc": {"nat": {"gateway": nat_mode}},
        "addons": [
            {"name": "vpc-cni"},
            {"name": "kube-proxy"},
            {"name": "coredns"},
        ],
        "managedNodeGroups": [{
            "name": "system",
            "instanceType": sysp["instance_type"],
            "minSize": sysp["min"],
            "maxSize": sysp["max"],
            "desiredCapacity": sysp["min"],
            "volumeSize": 100,
            "labels": {SYSTEM_LABEL_KEY: SYSTEM_LABEL_VALUE},
            "taints": [{
                "key": SYSTEM_LABEL_KEY,
                "value": SYSTEM_LABEL_VALUE,
                "effect": SYSTEM_TAINT_EFFECT,
            }],
            "tags": {"Type": TAG_TYPE, "Owner": owner},
            # Tag for Karpenter discovery — Karpenter looks for SGs/subnets tagged this:
            "propagateASGTags": True,
        }],
    }
    return json.dumps(config, indent=2)


# ---------- Karpenter EC2NodeClass + NodePool ----------

def _ami_family_for_pool(pool: dict[str, Any]) -> str:
    explicit = pool.get("ami_family")
    if explicit:
        return explicit
    return "Bottlerocket"


def render_ec2nodeclass(pool: dict[str, Any], cluster_name: str, owner: str) -> dict[str, Any]:
    ami_family = _ami_family_for_pool(pool)

    # Karpenter v1 alias syntax: <family>@latest auto-resolves the right variant
    # (e.g. Bottlerocket NVIDIA AMI for GPU instance types).
    alias = {
        "Bottlerocket": "bottlerocket@latest",
        "AL2023": "al2023@latest",
    }[ami_family]

    # Bottlerocket data volume is /dev/xvdb; AL2023 root is /dev/xvda.
    if ami_family == "Bottlerocket":
        data_device = "/dev/xvdb"
    else:
        data_device = "/dev/xvda"

    block_device_mappings = [{
        "deviceName": data_device,
        "ebs": {
            "volumeSize": f"{pool['disk_gb']}Gi",
            "volumeType": "gp3",
            "throughput": 250,
            "iops": 3000,
            "deleteOnTermination": True,
            "encrypted": True,
        },
    }]

    nc = {
        "apiVersion": "karpenter.k8s.aws/v1",
        "kind": "EC2NodeClass",
        "metadata": {"name": pool["name"]},
        "spec": {
            "amiFamily": ami_family,
            "amiSelectorTerms": [{"alias": alias}],
            "role": f"KarpenterNodeRole-{cluster_name}",
            "subnetSelectorTerms": [{"tags": {"karpenter.sh/discovery": cluster_name}}],
            "securityGroupSelectorTerms": [{"tags": {"karpenter.sh/discovery": cluster_name}}],
            "blockDeviceMappings": block_device_mappings,
            "tags": {
                "Type": TAG_TYPE,
                "Owner": owner,
                "karpenter-pool": pool["name"],
            },
        },
    }
    return nc


def render_nodepool(
    pool: dict[str, Any],
    cluster_name: str,
    disruption_defaults: dict[str, Any],
) -> dict[str, Any]:
    # Build pool labels: user labels + bookkeeping labels we always add.
    labels = dict(pool.get("labels") or {})
    labels[POOL_LABEL_KEY] = pool["name"]
    if pool.get("gpu"):
        labels.setdefault("nvidia.com/gpu", "true")

    # Build taints: user taints + GPU taint if gpu.
    taints = list(pool.get("taints") or [])
    if pool.get("gpu"):
        taints.append({
            "key": "nvidia.com/gpu",
            "value": "true",
            "effect": "NoSchedule",
        })

    requirements = [
        {
            "key": "node.kubernetes.io/instance-type",
            "operator": "In",
            "values": list(pool["instance_types"]),
        },
        {
            "key": "karpenter.sh/capacity-type",
            "operator": "In",
            "values": list(pool["capacity_types"]),
        },
        {
            "key": "kubernetes.io/arch",
            "operator": "In",
            "values": ["amd64"],
        },
    ]

    # Translate pool.max into per-resource limits. Karpenter has no node-count
    # limit field — the canonical pattern is to cap by aggregate CPU.
    # 192 vCPU is a permissive ceiling per node family; multiply by user max.
    max_nodes = int(pool["max"])
    cpu_cap = max_nodes * 192

    # Per-pool override falls back to global disruption defaults when the user
    # didn't set it on the pool (None counts as unset).
    consolidation_policy = (pool.get("consolidation_policy")
                            or disruption_defaults["consolidation_policy"])
    consolidate_after = (pool.get("consolidate_after")
                         or disruption_defaults["consolidate_after"])
    expire_after = (pool.get("expire_after")
                    or disruption_defaults["expire_after"]
                    or "Never")

    disruption = {
        "consolidationPolicy": consolidation_policy,
        "consolidateAfter": consolidate_after,
        "budgets": [{"nodes": "100%"}],
    }

    np = {
        "apiVersion": "karpenter.sh/v1",
        "kind": "NodePool",
        "metadata": {"name": pool["name"]},
        "spec": {
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "nodeClassRef": {
                        "group": "karpenter.k8s.aws",
                        "kind": "EC2NodeClass",
                        "name": pool["name"],
                    },
                    "requirements": requirements,
                    "taints": taints,
                    # Karpenter v1: expireAfter lives in spec.template.spec.
                    "expireAfter": expire_after,
                },
            },
            "limits": {"cpu": str(cpu_cap)},
            "disruption": disruption,
        },
    }
    return np


def render_pool_documents(spec: dict[str, Any]) -> str:
    """Render all NodePools + EC2NodeClasses as a single multi-document stream.

    Each document is JSON; documents are separated by `---` so kubectl apply
    handles them as a multi-document YAML stream.
    """
    docs: list[dict[str, Any]] = []
    for pool in spec["nodepools"]:
        docs.append(render_ec2nodeclass(pool, spec["name"], spec["owner"]))
        docs.append(render_nodepool(pool, spec["name"], spec["disruption"]))
    return "\n---\n".join(json.dumps(d, indent=2) for d in docs)


# ---------- KubeRay operator helm values ----------

def kuberay_helm_values() -> dict[str, Any]:
    """Land kuberay-operator on the system pool with the matching toleration."""
    return {
        "nodeSelector": {SYSTEM_LABEL_KEY: SYSTEM_LABEL_VALUE},
        "tolerations": [{
            "key": SYSTEM_LABEL_KEY,
            "operator": "Equal",
            "value": SYSTEM_LABEL_VALUE,
            "effect": SYSTEM_TAINT_EFFECT,
        }],
    }


# ---------- Karpenter helm values ----------

def karpenter_helm_values(
    cluster_name: str,
    cluster_endpoint: str,
    interruption_queue: str,
    controller_role_arn: str,
) -> dict[str, Any]:
    return {
        "settings": {
            "clusterName": cluster_name,
            "clusterEndpoint": cluster_endpoint,
            "interruptionQueue": interruption_queue,
        },
        "serviceAccount": {
            "annotations": {
                "eks.amazonaws.com/role-arn": controller_role_arn,
            },
        },
        "controller": {
            "resources": {
                "requests": {"cpu": "200m", "memory": "256Mi"},
                "limits": {"cpu": "1", "memory": "1Gi"},
            },
        },
        "nodeSelector": {SYSTEM_LABEL_KEY: SYSTEM_LABEL_VALUE},
        "tolerations": [{
            "key": SYSTEM_LABEL_KEY,
            "operator": "Equal",
            "value": SYSTEM_LABEL_VALUE,
            "effect": SYSTEM_TAINT_EFFECT,
        }],
        "replicas": 1,
    }


# ---------- GPU smoke test Job ----------

def render_gpu_smoke_job(pool_name: str, job_name: str,
                         taints: list[dict[str, Any]]) -> dict[str, Any]:
    """Minimal Job that requests nvidia.com/gpu: 1 and runs nvidia-smi.

    Triggers Karpenter to provision a node from the named pool, then exits.
    Cluster-side tolerations match every taint the pool sets, plus the standard
    nvidia.com/gpu taint from the gpu: true rendering.
    """
    tolerations = [
        {"key": t["key"], "operator": "Equal",
         "value": t["value"], "effect": t["effect"]}
        for t in taints
    ]
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": job_name,
            "labels": {"app": "launch-eks-gpu-smoke"},
        },
        "spec": {
            "ttlSecondsAfterFinished": 300,
            "backoffLimit": 0,
            "template": {
                "metadata": {"labels": {"app": "launch-eks-gpu-smoke"}},
                "spec": {
                    "restartPolicy": "Never",
                    "nodeSelector": {POOL_LABEL_KEY: pool_name},
                    "tolerations": tolerations,
                    "containers": [{
                        "name": "smoke",
                        "image": GPU_SMOKE_IMAGE,
                        "command": ["nvidia-smi"],
                        "resources": {"limits": {"nvidia.com/gpu": 1}},
                    }],
                },
            },
        },
    }


# ---------- helpers ----------

def render_doc(obj: Any) -> str:
    """Render a single document as pretty JSON (valid YAML 1.2)."""
    return json.dumps(obj, indent=2)
