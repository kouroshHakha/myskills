#!/usr/bin/env python3
"""launch_eks - YAML-driven, Karpenter-backed EKS cluster manager for Ray + KubeRay.

Scale-to-zero: only an always-on system node pool (1x m7i.large) bills in steady
state; user-defined node pools become Karpenter NodePool/EC2NodeClass CRDs and
scale from zero on demand.

eksctl drives the cluster + system NG. Karpenter handles per-pool autoscaling.
KubeRay operator is preinstalled on the system pool. AWS-tag-driven discovery.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

# Local module — keep all renderers there.
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import templates as T  # noqa: E402

CONFIG_DIR = Path.home() / ".config" / "launch-eks"
CONFIG_PATH = CONFIG_DIR / "config.json"


# ---------- low-level helpers ----------

def die(msg: str, code: int = 1) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def aws_env(profile: str) -> dict:
    keep = {k: v for k, v in os.environ.items()
            if not k.startswith(("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
                                 "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN",
                                 "AWS_PROFILE"))}
    keep["AWS_PROFILE"] = profile
    return keep


def run(cmd: list[str], *, env: dict | None = None, capture: bool = True,
        check: bool = True, stdin: str | None = None,
        cwd: str | None = None) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, env=env, capture_output=capture, text=True,
                       input=stdin, cwd=cwd)
    if check and r.returncode != 0:
        out = (r.stderr or r.stdout or "").strip()
        die(f"{cmd[0]} {' '.join(cmd[1:3])}... failed: {out}")
    return r


def aws(args: list[str], cfg: dict, *, capture: bool = True, check: bool = True,
        stdin: str | None = None) -> str:
    cmd = ["aws", "--profile", cfg["aws_profile"], "--region", cfg["aws_region"],
           "--no-cli-pager"] + args
    r = run(cmd, env=aws_env(cfg["aws_profile"]),
            capture=capture, check=check, stdin=stdin)
    return r.stdout


def kubectl(args: list[str], *, capture: bool = True, check: bool = True,
            stdin: str | None = None) -> str:
    r = run(["kubectl"] + args, capture=capture, check=check, stdin=stdin)
    return r.stdout


def helm(args: list[str], *, capture: bool = True, check: bool = True) -> str:
    r = run(["helm"] + args, capture=capture, check=check)
    return r.stdout


def eksctl(args: list[str], cfg: dict, *, capture: bool = True,
           check: bool = True, stdin: str | None = None) -> str:
    env = aws_env(cfg["aws_profile"])
    env["AWS_REGION"] = cfg["aws_region"]
    r = run(["eksctl"] + args, env=env, capture=capture, check=check, stdin=stdin)
    return r.stdout


def check_deps() -> None:
    missing = [t for t in ("aws", "eksctl", "kubectl", "helm", "yq")
               if shutil.which(t) is None]
    if missing:
        die("missing required tool(s): " + ", ".join(missing) +
            "\n  brew install eksctl kubectl helm yq")


# ---------- config ----------

def load_or_init_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text())
    cfg = {
        "aws_profile": os.environ.get("AWS_PROFILE", "default"),
        "aws_region": os.environ.get("AWS_REGION", "us-west-2"),
        "owner": os.environ.get("USER", "user"),
    }
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n")
    print(f"==> created {CONFIG_PATH} with derived defaults — review and edit if needed")
    return cfg


# ---------- cluster.yaml schema ----------

def load_cluster_yaml(path: str, cfg: dict) -> dict:
    p = Path(path)
    if not p.exists():
        die(f"cluster spec not found: {path}")
    if p.suffix == ".json":
        raw = json.loads(p.read_text())
    else:
        # Use yq to convert YAML→JSON. yq is in deps.
        r = subprocess.run(["yq", "-o=json", str(p)], capture_output=True, text=True)
        if r.returncode != 0:
            die(f"yq failed parsing {path}: {(r.stderr or r.stdout).strip()}")
        raw = json.loads(r.stdout)
    if not isinstance(raw, dict):
        die(f"{path}: top-level must be a mapping")
    return _validate_spec(raw, cfg)


def _validate_spec(raw: dict, cfg: dict) -> dict:
    spec: dict[str, Any] = {}

    # name
    name = raw.get("name")
    if not name or not isinstance(name, str):
        die("cluster spec: `name` (string) is required")
    spec["name"] = name

    # region/version/owner with config fallback
    spec["region"] = raw.get("region", cfg["aws_region"])
    spec["version"] = str(raw.get("version", T.EKS_VERSION_DEFAULT))
    spec["owner"] = raw.get("owner", cfg["owner"])

    # system pool (defaults applied)
    sysp = dict(raw.get("system") or {})
    sysp.setdefault("instance_type", "m7i.large")
    sysp.setdefault("min", 1)
    sysp.setdefault("max", max(2, int(sysp["min"]) + 1))
    if int(sysp["min"]) < 1:
        die("system.min must be >= 1 (need somewhere to host operators)")
    if int(sysp["max"]) < int(sysp["min"]):
        die("system.max must be >= system.min")
    spec["system"] = sysp

    # vpc options (currently only `nat`; eksctl handles validation)
    vpc_raw = raw.get("vpc") or {}
    if not isinstance(vpc_raw, dict):
        die("vpc must be a mapping")
    if vpc_raw:
        spec["vpc"] = vpc_raw

    # optional AZ pinning — useful when some AZs are at NAT/EIP quota
    az_raw = raw.get("availability_zones") or []
    if az_raw:
        if not isinstance(az_raw, list) or not all(isinstance(z, str) for z in az_raw):
            die("availability_zones must be a list of strings")
        if len(az_raw) < 2:
            die("availability_zones: EKS requires at least 2 AZs")
        spec["availability_zones"] = az_raw

    # disruption defaults
    disr = dict(raw.get("disruption") or {})
    disr.setdefault("consolidation_policy", "WhenEmpty")
    disr.setdefault("consolidate_after", "30s")
    disr.setdefault("expire_after", "720h")
    if disr["consolidation_policy"] not in ("WhenEmpty", "WhenEmptyOrUnderutilized"):
        die("disruption.consolidation_policy must be WhenEmpty | WhenEmptyOrUnderutilized")
    spec["disruption"] = disr

    # nodepools
    pools_raw = raw.get("nodepools") or []
    if not isinstance(pools_raw, list):
        die("nodepools must be a list")
    seen_names = set()
    pools: list[dict[str, Any]] = []
    for i, p in enumerate(pools_raw):
        if not isinstance(p, dict):
            die(f"nodepools[{i}]: must be a mapping")
        pname = p.get("name")
        if not pname or not isinstance(pname, str):
            die(f"nodepools[{i}]: `name` (string) is required")
        if pname == "system":
            die(f"nodepools[{i}]: name 'system' is reserved")
        if pname in seen_names:
            die(f"nodepools[{i}]: duplicate name {pname!r}")
        seen_names.add(pname)

        instance_types = p.get("instance_types") or []
        if not isinstance(instance_types, list) or not instance_types:
            die(f"nodepools[{pname}]: `instance_types` must be a non-empty list")
        if not all(isinstance(x, str) for x in instance_types):
            die(f"nodepools[{pname}]: instance_types must be strings")

        gpu = bool(p.get("gpu", False))
        if gpu:
            non_gpu = [x for x in instance_types if not T.is_gpu_instance_type(x)]
            if non_gpu:
                die(f"nodepools[{pname}]: gpu=true but instance_types include "
                    f"non-GPU types: {non_gpu}")

        max_nodes = int(p.get("max", 0))
        if max_nodes <= 0:
            die(f"nodepools[{pname}]: `max` (int > 0) is required")

        capacity_types = p.get("capacity_types") or ["on-demand"]
        if not isinstance(capacity_types, list) or not capacity_types:
            die(f"nodepools[{pname}]: capacity_types must be a non-empty list")
        for ct in capacity_types:
            if ct not in ("on-demand", "spot"):
                die(f"nodepools[{pname}]: capacity_types must be on-demand|spot")

        # min support: per plan, default 0 (true scale-to-zero). Allow override
        # but warn — that breaks the steady-state cost story.
        min_nodes = int(p.get("min", 0))
        if min_nodes < 0:
            die(f"nodepools[{pname}]: min must be >= 0")
        if min_nodes > 0:
            print(f"warning: nodepools[{pname}].min={min_nodes} — "
                  f"breaks scale-to-zero; this many nodes will bill 24/7")

        ami_family = p.get("ami_family") or "Bottlerocket"
        if ami_family not in ("Bottlerocket", "AL2023"):
            die(f"nodepools[{pname}]: ami_family must be Bottlerocket|AL2023")

        labels = p.get("labels") or {}
        if not isinstance(labels, dict):
            die(f"nodepools[{pname}]: labels must be a mapping")
        taints = p.get("taints") or []
        if not isinstance(taints, list):
            die(f"nodepools[{pname}]: taints must be a list")
        for j, t in enumerate(taints):
            if not isinstance(t, dict) or "key" not in t or "effect" not in t:
                die(f"nodepools[{pname}].taints[{j}]: must have key, value, effect")

        pools.append({
            "name": pname,
            "instance_types": instance_types,
            "max": max_nodes,
            "min": min_nodes,
            "capacity_types": capacity_types,
            "disk_gb": int(p.get("disk_gb", 100)),
            "gpu": gpu,
            "ami_family": ami_family,
            "labels": labels,
            "taints": taints,
            "consolidation_policy": p.get("consolidation_policy"),
            "consolidate_after": p.get("consolidate_after"),
            "expire_after": p.get("expire_after"),
        })
    spec["nodepools"] = pools
    return spec


# ---------- discovery (clusters tagged Type=ray-eks, Owner=<user>) ----------

def discover(cfg: dict) -> list[dict]:
    out = aws([
        "resourcegroupstaggingapi", "get-resources",
        "--resource-type-filters", "eks:cluster",
        "--tag-filters",
        f"Key=Type,Values={T.TAG_TYPE}",
        f"Key=Owner,Values={cfg['owner']}",
        "--query", "ResourceTagMappingList[].ResourceARN",
        "--output", "json",
    ], cfg)
    arns = json.loads(out or "[]")
    clusters = []
    for arn in arns:
        name = arn.rsplit("/", 1)[1]
        d = aws(["eks", "describe-cluster", "--name", name,
                 "--query", "cluster", "--output", "json"], cfg)
        info = json.loads(d)
        clusters.append({
            "name": info["name"],
            "status": info["status"],
            "version": info.get("version", ""),
            "arn": arn,
            "endpoint": info.get("endpoint", ""),
        })
    return clusters


def find_cluster(name: str, cfg: dict) -> dict | None:
    for c in discover(cfg):
        if c["name"] == name:
            return c
    return None


# ---------- waits ----------

def wait_for_deployment(namespace: str, name: str, timeout_s: int = 300) -> None:
    print(f"    waiting for deployment {namespace}/{name} Ready (timeout {timeout_s}s)")
    r = run(["kubectl", "-n", namespace, "rollout", "status",
             f"deployment/{name}", f"--timeout={timeout_s}s"],
            capture=False, check=False)
    if r.returncode != 0:
        die(f"deployment {namespace}/{name} not Ready in {timeout_s}s")


def wait_for_nodepool_ready(name: str, timeout_s: int = 120) -> None:
    print(f"    waiting for NodePool {name} Ready")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        r = subprocess.run(
            ["kubectl", "get", "nodepool", name,
             "-o", "jsonpath={.status.conditions[?(@.type=='Ready')].status}"],
            capture_output=True, text=True,
        )
        if r.returncode == 0 and r.stdout.strip() == "True":
            return
        time.sleep(3)
    die(f"NodePool {name} not Ready in {timeout_s}s")


def wait_for_pod_phase(name: str, namespace: str, target: str,
                       timeout_s: int) -> str:
    deadline = time.time() + timeout_s
    last = ""
    while time.time() < deadline:
        r = subprocess.run([
            "kubectl", "-n", namespace, "get", "pod", name,
            "-o", "jsonpath={.status.phase}",
        ], capture_output=True, text=True)
        last = r.stdout.strip()
        if last == target or last == "Failed":
            return last
        time.sleep(5)
    return last


# ---------- Karpenter IAM via CloudFormation ----------

def install_karpenter_iam(spec: dict, cfg: dict) -> dict:
    """Deploy Karpenter's official IAM/SQS CF template. Returns CF outputs."""
    cluster_name = spec["name"]
    stack = f"Karpenter-{cluster_name}"
    print(f"==> deploying Karpenter IAM CF stack {stack}")
    print(f"    fetching template: {T.KARPENTER_IAM_CF_URL}")
    try:
        with urllib.request.urlopen(T.KARPENTER_IAM_CF_URL, timeout=30) as resp:
            template = resp.read().decode()
    except Exception as e:
        die(f"failed to fetch Karpenter CF template: {e}")

    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(template)
        tpath = f.name

    aws([
        "cloudformation", "deploy",
        "--stack-name", stack,
        "--template-file", tpath,
        "--capabilities", "CAPABILITY_NAMED_IAM",
        "--parameter-overrides", f"ClusterName={cluster_name}",
    ], cfg, capture=False)

    out = aws([
        "cloudformation", "describe-stacks",
        "--stack-name", stack,
        "--query", "Stacks[0].Outputs",
        "--output", "json",
    ], cfg)
    outputs = {o["OutputKey"]: o["OutputValue"] for o in (json.loads(out) or [])}

    # The upstream CF template (Karpenter v1.x) creates only the node role and
    # the controller managed policy — not the controller IAM role itself.
    # Create the IRSA-bound controller role via eksctl and inject its ARN so
    # the helm install can wire it onto the karpenter ServiceAccount.
    account_id = aws(["sts", "get-caller-identity", "--query", "Account",
                      "--output", "text"], cfg).strip()
    policy_arn = f"arn:aws:iam::{account_id}:policy/KarpenterControllerPolicy-{cluster_name}"
    sa_role_name = f"{cluster_name}-karpenter"
    print(f"==> creating IRSA role {sa_role_name} for karpenter controller")
    eksctl([
        "create", "iamserviceaccount",
        "--cluster", cluster_name,
        "--region", cfg["aws_region"],
        "--namespace", "karpenter",
        "--name", "karpenter",
        "--role-name", sa_role_name,
        "--attach-policy-arn", policy_arn,
        "--role-only",
        "--approve",
        "--override-existing-serviceaccounts",
    ], cfg, capture=False)
    outputs["KarpenterControllerRoleArn"] = (
        f"arn:aws:iam::{account_id}:role/{sa_role_name}"
    )
    outputs["KarpenterNodeRoleArn"] = (
        f"arn:aws:iam::{account_id}:role/KarpenterNodeRole-{cluster_name}"
    )
    # SQS queue created by the upstream CF template is named exactly
    # ${ClusterName}; the skill's older fallback ("Karpenter-${ClusterName}")
    # doesn't match v1.x templates.
    outputs.setdefault("InterruptionQueueName", cluster_name)
    return outputs


def map_karpenter_node_role(spec: dict, cfg: dict, node_role_arn: str) -> None:
    """Add Karpenter node IAM role to aws-auth so its instances can join."""
    cluster_name = spec["name"]
    print(f"==> mapping Karpenter node role to aws-auth")
    eksctl([
        "create", "iamidentitymapping",
        "--cluster", cluster_name,
        "--region", cfg["aws_region"],
        "--arn", node_role_arn,
        "--username", "system:node:{{EC2PrivateDNSName}}",
        "--group", "system:bootstrappers",
        "--group", "system:nodes",
    ], cfg, capture=False)


def tag_cluster_for_karpenter_discovery(spec: dict, cfg: dict) -> None:
    """Tag subnets + SGs that Karpenter discovers via karpenter.sh/discovery=<cluster>."""
    cluster_name = spec["name"]
    print(f"==> tagging subnets + cluster SG for Karpenter discovery")

    # Subnets: get from cluster
    out = aws([
        "eks", "describe-cluster", "--name", cluster_name,
        "--query", "cluster.resourcesVpcConfig.subnetIds",
        "--output", "json",
    ], cfg)
    subnet_ids = json.loads(out)
    if subnet_ids:
        aws([
            "ec2", "create-tags",
            "--resources", *subnet_ids,
            "--tags", f"Key=karpenter.sh/discovery,Value={cluster_name}",
        ], cfg)

    # Cluster security group (the SG attached to the EKS managed network interfaces)
    out = aws([
        "eks", "describe-cluster", "--name", cluster_name,
        "--query", "cluster.resourcesVpcConfig.clusterSecurityGroupId",
        "--output", "text",
    ], cfg).strip()
    if out and out != "None":
        aws([
            "ec2", "create-tags",
            "--resources", out,
            "--tags", f"Key=karpenter.sh/discovery,Value={cluster_name}",
        ], cfg)


def install_karpenter_helm(spec: dict, cfg: dict, cf_outputs: dict) -> None:
    cluster_name = spec["name"]
    endpoint_out = aws([
        "eks", "describe-cluster", "--name", cluster_name,
        "--query", "cluster.endpoint", "--output", "text",
    ], cfg).strip()

    controller_role = cf_outputs.get("KarpenterControllerRoleArn") \
        or cf_outputs.get("KarpenterControllerRole")
    queue_name = cf_outputs.get("InterruptionQueueName") \
        or f"Karpenter-{cluster_name}"
    if not controller_role:
        die(f"could not find KarpenterControllerRole in CF outputs: {cf_outputs}")

    values = T.karpenter_helm_values(
        cluster_name=cluster_name,
        cluster_endpoint=endpoint_out,
        interruption_queue=queue_name,
        controller_role_arn=controller_role,
    )

    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(T.render_doc(values))
        vpath = f.name

    print(f"==> helm install karpenter {T.KARPENTER_CHART_VERSION}")
    helm([
        "upgrade", "--install", "karpenter",
        "oci://public.ecr.aws/karpenter/karpenter",
        "--version", T.KARPENTER_CHART_VERSION,
        "--namespace", "karpenter",
        "--create-namespace",
        "--values", vpath,
        "--wait", "--timeout", "5m",
    ], capture=False)


# ---------- KubeRay operator ----------

def install_kuberay_operator() -> None:
    print(f"==> helm install kuberay-operator {T.KUBERAY_OPERATOR_CHART_VERSION}")
    helm(["repo", "add", "kuberay", "https://ray-project.github.io/kuberay-helm/"],
         capture=False, check=False)
    helm(["repo", "update", "kuberay"], capture=False, check=False)

    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(T.render_doc(T.kuberay_helm_values()))
        vpath = f.name

    helm([
        "upgrade", "--install", "kuberay-operator", "kuberay/kuberay-operator",
        "--version", T.KUBERAY_OPERATOR_CHART_VERSION,
        "--namespace", "kuberay-system",
        "--create-namespace",
        "--values", vpath,
        "--wait", "--timeout", "5m",
    ], capture=False)


# ---------- NodePools / EC2NodeClasses ----------

def apply_user_pools(spec: dict) -> None:
    docs = T.render_pool_documents(spec)
    print(f"==> applying {len(spec['nodepools'])} NodePools + EC2NodeClasses")
    kubectl(["apply", "-f", "-"], stdin=docs)
    for pool in spec["nodepools"]:
        wait_for_nodepool_ready(pool["name"])


# ---------- AL2023 GPU pools: device plugin DaemonSet ----------

def install_device_plugin_if_needed(spec: dict) -> None:
    al2023_gpu = [p for p in spec["nodepools"]
                  if p["gpu"] and p["ami_family"] == "AL2023"]
    if not al2023_gpu:
        return
    print(f"==> applying NVIDIA k8s device plugin (for AL2023 GPU pools)")
    kubectl(["apply", "-f", T.NVIDIA_PLUGIN_URL])


# ---------- validation ----------

def _check_nodes_ready() -> list[dict]:
    out = kubectl(["get", "nodes", "-o", "json"])
    nodes = json.loads(out).get("items", [])
    if not nodes:
        die("cluster has no nodes")
    not_ready = []
    for n in nodes:
        ready = any(c["type"] == "Ready" and c["status"] == "True"
                    for c in n["status"].get("conditions", []))
        if not ready:
            not_ready.append(n["metadata"]["name"])
    if not_ready:
        die(f"nodes not Ready: {not_ready}")
    print(f"    [ok] nodes Ready ({len(nodes)} total)")
    return nodes


def _check_kube_system_pods() -> None:
    out = kubectl(["-n", "kube-system", "get", "pods", "-o", "json"])
    pods = json.loads(out).get("items", [])
    bad = [(p["metadata"]["name"], p["status"]["phase"]) for p in pods
           if p["status"]["phase"] not in ("Running", "Succeeded")]
    if bad:
        die(f"kube-system pods not healthy: {bad}")
    print(f"    [ok] kube-system pods healthy ({len(pods)} pods)")


def _check_core_daemonsets() -> None:
    for ds in ("aws-node", "kube-proxy"):
        out = kubectl([
            "-n", "kube-system", "get", "ds", ds,
            "-o", "jsonpath={.status.numberReady}/{.status.desiredNumberScheduled}",
        ])
        ready, _, desired = out.partition("/")
        if not ready or not desired or ready != desired:
            die(f"daemonset {ds}: only {out} pods Ready")
        print(f"    [ok] daemonset {ds}: {out}")


def _check_karpenter() -> bool:
    r = subprocess.run(
        ["kubectl", "-n", "karpenter", "get", "deployment", "karpenter",
         "-o", "json"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(f"    [skip] karpenter Deployment not found")
        return False
    info = json.loads(r.stdout)
    desired = info["status"].get("replicas", 0) or 0
    ready = info["status"].get("readyReplicas", 0) or 0
    if desired == 0 or ready < desired:
        die(f"karpenter deployment unhealthy: {ready}/{desired} replicas Ready")
    print(f"    [ok] karpenter deployment: {ready}/{desired}")
    return True


def _check_nodepools() -> int:
    out = kubectl(["get", "nodepool", "-o", "json"], check=False)
    if not out:
        print(f"    [skip] no NodePool CRD installed")
        return 0
    items = json.loads(out).get("items", [])
    if not items:
        print(f"    [skip] no NodePools defined")
        return 0
    for np in items:
        name = np["metadata"]["name"]
        conds = {c["type"]: c["status"]
                 for c in np.get("status", {}).get("conditions", [])}
        ready = conds.get("Ready", "Unknown")
        if ready != "True":
            die(f"NodePool {name} not Ready: status={ready} conds={conds}")
        print(f"    [ok] NodePool {name}: Ready")
    return len(items)


def _check_kuberay() -> bool:
    r = subprocess.run(
        ["kubectl", "-n", "kuberay-system", "get", "deployment",
         "kuberay-operator", "-o", "json"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(f"    [skip] kuberay-operator not found")
        return False
    info = json.loads(r.stdout)
    desired = info["status"].get("replicas", 0) or 0
    ready = info["status"].get("readyReplicas", 0) or 0
    if desired == 0 or ready < desired:
        die(f"kuberay-operator unhealthy: {ready}/{desired} Ready")
    print(f"    [ok] kuberay-operator: {ready}/{desired}")
    return True


def validate(*, phase: str = "full") -> None:
    """Run validation checks. Phase 'baseline' = pre-Karpenter (system NG only)."""
    print()
    print(f"==> validating cluster ({phase})")
    _check_nodes_ready()
    _check_kube_system_pods()
    _check_core_daemonsets()
    if phase == "full":
        _check_karpenter()
        _check_nodepools()
        _check_kuberay()
    print(f"==> validation passed")


# ---------- GPU smoke test (trigger-and-verify) ----------

def gpu_smoke_test(spec: dict, pool_name: str,
                   provision_timeout_s: int = 480) -> None:
    pool = next((p for p in spec["nodepools"] if p["name"] == pool_name), None)
    if pool is None:
        die(f"smoke-test: pool {pool_name!r} not found in spec")
    if not pool["gpu"]:
        die(f"smoke-test: pool {pool_name!r} is not a GPU pool (gpu: true)")

    job_name = f"gpu-smoke-{int(time.time())}"
    pool_taints = list(pool["taints"])
    pool_taints.append({"key": "nvidia.com/gpu", "value": "true",
                        "effect": "NoSchedule"})
    job = T.render_gpu_smoke_job(pool_name, job_name, pool_taints)

    print()
    print(f"==> GPU smoke test: trigger Karpenter to provision {pool_name}, run nvidia-smi")
    t0 = time.time()
    kubectl(["apply", "-f", "-"], stdin=T.render_doc(job))
    try:
        # Wait for the Job's pod to exist
        pod_name = ""
        deadline = time.time() + 60
        while time.time() < deadline:
            r = subprocess.run([
                "kubectl", "get", "pods", "-l", f"job-name={job_name}",
                "-o", "jsonpath={.items[0].metadata.name}",
            ], capture_output=True, text=True)
            if r.stdout.strip():
                pod_name = r.stdout.strip()
                break
            time.sleep(3)
        if not pod_name:
            die("smoke-test: Job pod did not appear in 60s")
        t_pod_created = time.time()
        print(f"    pod scheduled ({pod_name}) in {t_pod_created - t0:.0f}s")

        # Wait for pod Running (covers EC2 boot + image pull)
        phase = wait_for_pod_phase(pod_name, "default", "Running",
                                   provision_timeout_s)
        t_running = time.time()
        if phase == "Failed":
            logs = kubectl(["logs", pod_name], check=False)
            die(f"smoke-test: pod failed before Running\n{logs}")
        if phase != "Running":
            r = subprocess.run([
                "kubectl", "describe", "pod", pod_name,
            ], capture_output=True, text=True)
            die(f"smoke-test: pod still {phase!r} after {provision_timeout_s}s\n"
                f"{r.stdout}")
        print(f"    pod Running in {t_running - t_pod_created:.0f}s "
              f"(EC2 boot + image pull)")

        # Wait for completion
        phase = wait_for_pod_phase(pod_name, "default", "Succeeded", 60)
        t_done = time.time()
        logs = kubectl(["logs", pod_name], check=False)
        if phase != "Succeeded":
            die(f"smoke-test: pod did not Succeed (phase={phase})\n{logs}")
        if "NVIDIA-SMI" not in logs:
            die(f"smoke-test: nvidia-smi output missing\n{logs}")
        print(f"    nvidia-smi succeeded in {t_done - t_running:.0f}s")
        for line in logs.splitlines()[:5]:
            print(f"         {line}")
    finally:
        # Delete the Job (and its pod) so Karpenter consolidation can scale down.
        subprocess.run(
            ["kubectl", "delete", "job", job_name, "--ignore-not-found",
             "--wait=false"],
            capture_output=True,
        )

    # Wait for the Karpenter-provisioned node to disappear (validates scale-down).
    print(f"    waiting for node to scale back to zero (Karpenter consolidation)")
    deadline = time.time() + 300
    while time.time() < deadline:
        r = subprocess.run([
            "kubectl", "get", "nodes", "-l", f"{T.POOL_LABEL_KEY}={pool_name}",
            "-o", "jsonpath={.items[*].metadata.name}",
        ], capture_output=True, text=True)
        if not r.stdout.strip():
            t_scaled = time.time()
            print(f"    [ok] node terminated in {t_scaled - t_done:.0f}s — scale-to-zero verified")
            print(f"    [ok] GPU smoke test total: {t_scaled - t0:.0f}s")
            return
        time.sleep(10)
    print(f"    [warn] node did not terminate in 300s — check Karpenter consolidation logs")


# ---------- commands ----------

def cmd_launch(args: argparse.Namespace) -> None:
    cfg = load_or_init_config()
    check_deps()
    spec = load_cluster_yaml(args.f, cfg)

    # cluster.yaml's region wins — every subsequent aws()/eksctl()/kubectl()
    # call needs to target that region, not whichever region the user happens
    # to have configured globally.
    cfg["aws_region"] = spec["region"]

    # eksctl ClusterConfig
    eksctl_yaml = T.render_eksctl_config(spec)

    if args.dry_run:
        print("==> DRY RUN: rendered eksctl ClusterConfig:")
        print(eksctl_yaml)
        print("==> DRY RUN: rendered NodePools + EC2NodeClasses:")
        print(T.render_pool_documents(spec))
        print("==> DRY RUN: KubeRay helm values:")
        print(T.render_doc(T.kuberay_helm_values()))
        return

    if find_cluster(spec["name"], cfg):
        die(f"cluster {spec['name']!r} already exists; pick another name or run shutdown first")

    print(f"==> creating EKS cluster {spec['name']} (~17-20 min for control plane + system NG + Karpenter IAM)")
    eksctl(["create", "cluster", "-f", "-"], cfg, capture=False, stdin=eksctl_yaml)

    print(f"==> updating local kubeconfig")
    aws(["eks", "update-kubeconfig", "--name", spec["name"]], cfg, capture=False)

    # Phase-1 validation: cluster + system NG only.
    validate(phase="baseline")

    # Karpenter requires subnets + cluster SG to carry the discovery tag.
    tag_cluster_for_karpenter_discovery(spec, cfg)

    cf_outputs = install_karpenter_iam(spec, cfg)

    node_role = cf_outputs.get("KarpenterNodeRole") \
        or cf_outputs.get("KarpenterNodeRoleArn")
    if node_role:
        if not node_role.startswith("arn:"):
            # KarpenterNodeRole is sometimes returned as just the role name; build ARN.
            account_id = aws(["sts", "get-caller-identity",
                              "--query", "Account", "--output", "text"], cfg).strip()
            node_role = f"arn:aws:iam::{account_id}:role/{node_role}"
        map_karpenter_node_role(spec, cfg, node_role)

    install_karpenter_helm(spec, cfg, cf_outputs)
    apply_user_pools(spec)
    install_kuberay_operator()
    install_device_plugin_if_needed(spec)

    validate(phase="full")

    if args.smoke_test:
        gpu_smoke_test(spec, args.smoke_test)

    print()
    print(f"==> launched cluster {spec['name']}")
    print(f"    region:    {spec['region']}")
    print(f"    nodepools: {', '.join(p['name'] for p in spec['nodepools'])}")
    print(f"    nodes:     kubectl get nodes")
    print(f"    pools:     kubectl get nodepool")
    print(f"    next:      apply a RayCluster/RayService — KubeRay operator is ready")


def cmd_list(args: argparse.Namespace) -> None:
    del args
    check_deps()
    cfg = load_or_init_config()
    clusters = discover(cfg)
    if not clusters:
        print("(no ray-eks clusters)")
        return
    print(f"{'NAME':<24} {'STATUS':<12} {'VERSION':<10}")
    for c in sorted(clusters, key=lambda x: x["name"]):
        print(f"{c['name']:<24} {c['status']:<12} {c['version']:<10}")


def cmd_kubeconfig(args: argparse.Namespace) -> None:
    check_deps()
    cfg = load_or_init_config()
    if find_cluster(args.name, cfg) is None:
        die(f"no cluster named {args.name!r}")
    aws(["eks", "update-kubeconfig", "--name", args.name], cfg, capture=False)
    print(f"==> kubeconfig updated; current-context now points at {args.name}")


def cmd_validate(args: argparse.Namespace) -> None:
    check_deps()
    cfg = load_or_init_config()
    if find_cluster(args.name, cfg) is None:
        die(f"no cluster named {args.name!r}")
    aws(["eks", "update-kubeconfig", "--name", args.name], cfg, capture=False)
    validate(phase="full")
    if args.smoke_test:
        # Reconstruct minimal pool info from cluster CRDs.
        out = kubectl(["get", "nodepool", args.smoke_test, "-o", "json"],
                      check=False)
        if not out:
            die(f"NodePool {args.smoke_test!r} not found in cluster")
        np = json.loads(out)
        labels = (np.get("spec", {}).get("template", {})
                  .get("metadata", {}).get("labels") or {})
        if labels.get("nvidia.com/gpu") != "true":
            die(f"NodePool {args.smoke_test!r} is not a GPU pool")
        taints = (np.get("spec", {}).get("template", {})
                  .get("spec", {}).get("taints") or [])
        spec_stub = {"nodepools": [{
            "name": args.smoke_test,
            "gpu": True,
            "taints": taints,
        }]}
        gpu_smoke_test(spec_stub, args.smoke_test)


def cmd_shutdown(args: argparse.Namespace) -> None:
    check_deps()
    cfg = load_or_init_config()
    cluster = find_cluster(args.name, cfg)
    if cluster is None:
        die(f"no cluster named {args.name!r}")
    print(f"==> {args.name}: status={cluster['status']}")
    if not args.yes:
        prompt = (f"DELETE EKS cluster {args.name}? "
                  f"this terminates all nodes, load balancers, and the VPC. [y/N] ")
        if input(prompt).strip().lower() != "y":
            print("aborted")
            return

    # Switch kubeconfig to the cluster so the CRD cleanup hits the right context.
    aws(["eks", "update-kubeconfig", "--name", args.name], cfg,
        capture=False, check=False)

    # Pre-delete: remove Karpenter NodePools + EC2NodeClasses so Karpenter
    # tears down its provisioned EC2 instances BEFORE eksctl drops the cluster.
    # Without this step, those instances leak (and keep billing).
    print(f"==> draining Karpenter NodeClaims (delete NodePools + EC2NodeClasses)")
    subprocess.run([
        "kubectl", "delete", "nodepool", "--all",
        "--wait=true", "--timeout=10m",
    ], check=False)
    subprocess.run([
        "kubectl", "delete", "ec2nodeclass", "--all",
        "--wait=true", "--timeout=10m",
    ], check=False)

    print(f"==> eksctl delete cluster (~10-15 min)")
    eksctl(["delete", "cluster", "--name", args.name,
            "--region", cfg["aws_region"]],
           cfg, capture=False)

    # Tear down the Karpenter IAM CF stack last (was created by us, after cluster).
    stack = f"Karpenter-{args.name}"
    print(f"==> deleting Karpenter IAM CF stack {stack}")
    aws(["cloudformation", "delete-stack", "--stack-name", stack],
        cfg, capture=False, check=False)

    print(f"==> {args.name} deleted")


# ---------- entry point ----------

def main() -> None:
    parser = argparse.ArgumentParser(prog="launch_eks", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("launch", help="provision a new EKS cluster from a YAML spec")
    s.add_argument("-f", required=True, metavar="FILE",
                   help="cluster.yaml — see examples/ in the skill dir")
    s.add_argument("--smoke-test", metavar="POOL",
                   help="after install, run nvidia-smi via Karpenter against POOL")
    s.add_argument("--dry-run", action="store_true",
                   help="print rendered configs and exit; do not call AWS")
    s.set_defaults(func=cmd_launch)

    s = sub.add_parser("list", help="list ray-eks clusters")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("kubeconfig", help="point kubectl at a cluster")
    s.add_argument("name")
    s.set_defaults(func=cmd_kubeconfig)

    s = sub.add_parser("validate",
                       help="run validation checks on an existing cluster")
    s.add_argument("name")
    s.add_argument("--smoke-test", metavar="POOL",
                   help="trigger Karpenter to provision POOL and run nvidia-smi")
    s.set_defaults(func=cmd_validate)

    s = sub.add_parser("shutdown",
                       help="delete an EKS cluster (drains Karpenter first)")
    s.add_argument("name")
    s.add_argument("-y", "--yes", action="store_true")
    s.set_defaults(func=cmd_shutdown)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
