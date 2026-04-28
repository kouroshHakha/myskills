#!/usr/bin/env python3
"""launch_devbox - lean Ray dev EC2 manager.

AWS-tag-driven. No terraform. No local state for box bookkeeping.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from textwrap import dedent

CONFIG_DIR = Path.home() / ".config" / "launch-dev-box"
CONFIG_PATH = CONFIG_DIR / "config.json"
SSH_CONFIG = CONFIG_DIR / "ssh_config"
TAG_TYPE = "ray-devbox"
GPU_PREFIXES = ("g", "p")  # g3 g4 g4dn g5 g6 p3 p4 p5 ...

SCRIPT_DIR = Path(__file__).resolve().parent
BOOTSTRAP_SCRIPT = SCRIPT_DIR / "bootstrap.sh"


def die(msg: str, code: int = 1) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def is_gpu(instance_type: str) -> bool:
    return instance_type.split(".", 1)[0].startswith(GPU_PREFIXES)


def aws_env(profile: str) -> dict:
    """Strip stray AWS_* env vars; force the chosen profile."""
    keep = {k: v for k, v in os.environ.items()
            if not k.startswith(("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
                                 "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN",
                                 "AWS_PROFILE"))}
    keep["AWS_PROFILE"] = profile
    return keep


def aws(args: list[str], cfg: dict, *, capture: bool = True) -> str:
    cmd = ["aws", "--profile", cfg["aws_profile"], "--region", cfg["aws_region"],
           "--no-cli-pager"] + args
    r = subprocess.run(cmd, env=aws_env(cfg["aws_profile"]),
                       capture_output=capture, text=True)
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "").strip()
        die(f"aws {' '.join(args[:2])} failed: {msg}")
    return r.stdout


# ---------- config ----------

def load_or_init_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text())

    # First run: derive sensible defaults; user can edit afterwards.
    gh_user = ""
    try:
        r = subprocess.run(["gh", "api", "user", "--jq", ".login"],
                           capture_output=True, text=True, check=True)
        gh_user = r.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

    pubkey_path = ""
    for name in ("id_ed25519.pub", "id_rsa.pub"):
        p = Path.home() / ".ssh" / name
        if p.exists():
            pubkey_path = str(p)
            break

    cfg = {
        "aws_profile": os.environ.get("AWS_PROFILE", "default"),
        "aws_region": os.environ.get("AWS_REGION", "us-west-2"),
        "github_user": gh_user,
        "pubkey_path": pubkey_path,
        "owner": os.environ.get("USER", "user"),
    }
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n")
    print(f"==> created {CONFIG_PATH} with derived defaults — review and edit if needed")
    if not cfg["github_user"]:
        die("github_user not set in config; install `gh` and `gh auth login`, or edit the config")
    if not cfg["pubkey_path"]:
        die("pubkey_path not set; create ~/.ssh/id_ed25519 (ssh-keygen) or edit the config")
    return cfg


# ---------- AMI lookup ----------

def find_ami(instance_type: str, cfg: dict) -> str:
    if is_gpu(instance_type):
        # AWS Deep Learning OSS Nvidia Driver AMI (Ubuntu 22.04) — drivers,
        # CUDA, PyTorch preinstalled. Owner = amazon (137112412989).
        out = aws([
            "ec2", "describe-images",
            "--owners", "amazon",
            "--filters",
            "Name=name,Values=Deep Learning OSS Nvidia Driver AMI GPU PyTorch * (Ubuntu 22.04) *",
            "Name=architecture,Values=x86_64",
            "--query", "Images | sort_by(@, &CreationDate) | [-1].ImageId",
            "--output", "text",
        ], cfg).strip()
        if not out or out == "None":
            die(f"no Deep Learning AMI found in {cfg['aws_region']}")
        return out

    # CPU: Canonical Ubuntu 22.04 LTS via SSM parameter (auto-updated by AWS).
    out = aws([
        "ssm", "get-parameter",
        "--name", "/aws/service/canonical/ubuntu/server/22.04/stable/current/amd64/hvm/ebs-gp3/ami-id",
        "--query", "Parameter.Value", "--output", "text",
    ], cfg).strip()
    if not out:
        die("no Ubuntu 22.04 SSM parameter found")
    return out


# ---------- discovery ----------

def discover(cfg: dict) -> list[dict]:
    out = aws([
        "ec2", "describe-instances",
        "--filters",
        f"Name=tag:Type,Values={TAG_TYPE}",
        f"Name=tag:Owner,Values={cfg['owner']}",
        "Name=instance-state-name,Values=running,pending,stopping,stopped",
        "--query", "Reservations[].Instances[]",
        "--output", "json",
    ], cfg)
    instances = json.loads(out or "[]")
    boxes = []
    for inst in instances:
        tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
        boxes.append({
            "name": tags.get("Name", inst["InstanceId"]),
            "instance_id": inst["InstanceId"],
            "instance_type": inst["InstanceType"],
            "state": inst["State"]["Name"],
            "public_ip": inst.get("PublicIpAddress", ""),
            "ami": inst["ImageId"],
            "sg_ids": [sg["GroupId"] for sg in inst.get("SecurityGroups", [])],
            "volume_ids": [bdm["Ebs"]["VolumeId"] for bdm in inst.get("BlockDeviceMappings", []) if "Ebs" in bdm],
        })
    return boxes


def find_box(name: str, cfg: dict) -> dict | None:
    for b in discover(cfg):
        if b["name"] == name:
            return b
    return None


# ---------- ssh config aggregator ----------

def write_ssh_config(boxes: list[dict]) -> None:
    """Regenerate ~/.config/launch-dev-box/ssh_config from current AWS state."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Auto-generated by launch-dev-box from AWS. Do not edit.",
        f"# Add this Include line once to ~/.ssh/config:",
        f"#   Include {SSH_CONFIG}",
        "",
        "Host *",
        "    PreferredAuthentications publickey",
        "    ServerAliveInterval 60",
        "    ServerAliveCountMax 30",
        "",
    ]
    for b in boxes:
        if b["state"] != "running" or not b["public_ip"]:
            continue
        # User varies by AMI: Ubuntu uses 'ubuntu', DLAMI also 'ubuntu'.
        lines += [
            f"Host {b['name']}",
            f"    User ubuntu",
            f"    Hostname {b['public_ip']}",
            f"    StrictHostKeyChecking no",
            f"    UserKnownHostsFile /dev/null",
            f"    ForwardAgent yes",
            "",
        ]
    SSH_CONFIG.write_text("\n".join(lines))


# ---------- security group ----------

def ensure_security_group(name: str, cfg: dict) -> str:
    """Create a per-box SG that allows SSH from anywhere; return its id."""
    sg_name = f"ray-devbox-{name}"
    out = aws([
        "ec2", "describe-security-groups",
        "--filters", f"Name=group-name,Values={sg_name}",
        "--query", "SecurityGroups[0].GroupId", "--output", "text",
    ], cfg).strip()
    if out and out != "None":
        return out

    out = aws([
        "ec2", "create-security-group",
        "--group-name", sg_name,
        "--description", f"ray-devbox SG for {name}",
        "--query", "GroupId", "--output", "text",
    ], cfg).strip()
    sg_id = out
    aws([
        "ec2", "authorize-security-group-ingress",
        "--group-id", sg_id,
        "--protocol", "tcp", "--port", "22",
        "--cidr", "0.0.0.0/0",
    ], cfg)
    return sg_id


# ---------- launch ----------

def cloud_init_user_data(pubkey: str) -> str:
    """Cloud-init #cloud-config that adds the user's pubkey to ubuntu's authorized_keys."""
    return dedent(f"""\
        #cloud-config
        ssh_authorized_keys:
          - {pubkey.strip()}
        """)


def cmd_launch(args: argparse.Namespace) -> None:
    cfg = load_or_init_config()
    if args.region:
        cfg["aws_region"] = args.region

    name = args.name
    existing = find_box(name, cfg)
    if existing:
        print(f"==> {name} already exists ({existing['state']}, {existing['public_ip']}); "
              "running bootstrap on it (idempotent)")
        if existing["state"] != "running":
            die(f"box is {existing['state']!r}; start it manually first")
        boxes = discover(cfg)
        write_ssh_config(boxes)
        wait_for_ssh(name)
        run_bootstrap(name, cfg["github_user"])
        return

    pubkey = Path(cfg["pubkey_path"]).expanduser().read_text().strip()
    user_data = cloud_init_user_data(pubkey)

    print(f"==> resolving AMI for {args.type} ({'GPU' if is_gpu(args.type) else 'CPU'})")
    ami = find_ami(args.type, cfg)
    print(f"    {ami}")

    print(f"==> creating security group")
    sg_id = ensure_security_group(name, cfg)

    print(f"==> launching {name} ({args.type})")
    tags_spec = (
        f"ResourceType=instance,Tags=["
        f"{{Key=Name,Value={name}}},"
        f"{{Key=Type,Value={TAG_TYPE}}},"
        f"{{Key=Owner,Value={cfg['owner']}}}"
        f"]"
    )
    bdm = (
        f"DeviceName=/dev/sda1,Ebs={{VolumeSize={args.disk_gb},VolumeType=gp3,DeleteOnTermination=true,Encrypted=true}}"
    )
    out = aws([
        "ec2", "run-instances",
        "--image-id", ami,
        "--instance-type", args.type,
        "--security-group-ids", sg_id,
        "--tag-specifications", tags_spec,
        "--block-device-mappings", bdm,
        "--user-data", user_data,
        "--metadata-options", "HttpEndpoint=enabled,HttpTokens=required",
        "--count", "1",
        "--query", "Instances[0].InstanceId", "--output", "text",
    ], cfg).strip()
    instance_id = out
    print(f"    instance_id={instance_id}")

    print(f"==> waiting for instance running")
    aws(["ec2", "wait", "instance-running", "--instance-ids", instance_id], cfg, capture=False)

    boxes = discover(cfg)
    write_ssh_config(boxes)

    print(f"==> waiting for ssh")
    wait_for_ssh(name)

    run_bootstrap(name, cfg["github_user"])

    box = find_box(name, cfg)
    print()
    print(f"==> launched {name}")
    if box:
        print(f"    ip:   {box['public_ip']}")
        print(f"    type: {box['instance_type']}")
    print(f"    ssh:  ssh {name}    (after Include line in ~/.ssh/config)")


def wait_for_ssh(name: str, attempts: int = 60, delay_s: int = 5) -> None:
    for _ in range(attempts):
        r = subprocess.run(
            ["ssh", "-F", str(SSH_CONFIG), "-o", "ConnectTimeout=5",
             "-o", "BatchMode=yes", name, "true"],
            capture_output=True,
        )
        if r.returncode == 0:
            return
        time.sleep(delay_s)
    die(f"ssh to {name} never came up in {attempts*delay_s}s")


def run_bootstrap(name: str, gh_user: str) -> None:
    bootstrap = BOOTSTRAP_SCRIPT.read_text()
    print(f"==> running bootstrap on {name}")
    cmd = ["ssh", "-F", str(SSH_CONFIG), name, f"bash -s {gh_user} {name}"]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    proc.communicate(bootstrap.encode())
    if proc.returncode != 0:
        die(f"bootstrap failed (exit {proc.returncode})")


# ---------- list / ssh / shutdown ----------

def cmd_list(args: argparse.Namespace) -> None:
    del args
    cfg = load_or_init_config()
    boxes = discover(cfg)
    write_ssh_config(boxes)
    if not boxes:
        print("(no ray dev boxes)")
        return
    print(f"{'NAME':<22} {'STATE':<10} {'INSTANCE':<14} {'IP':<16} {'AMI':<22}")
    for b in sorted(boxes, key=lambda x: x["name"]):
        kind = "GPU" if is_gpu(b["instance_type"]) else "CPU"
        print(f"{b['name']:<22} {b['state']:<10} {b['instance_type']:<14} "
              f"{b['public_ip']:<16} {kind+' ('+b['ami'][:14]+')':<22}")


def cmd_ssh(args: argparse.Namespace) -> None:
    cfg = load_or_init_config()
    box = find_box(args.name, cfg)
    if box is None:
        die(f"no box named {args.name!r}")
    if box["state"] != "running":
        die(f"box {args.name} is {box['state']!r}, not running")
    boxes = discover(cfg)
    write_ssh_config(boxes)
    cmd = ["ssh", "-F", str(SSH_CONFIG), args.name] + (args.extra or [])
    os.execvp("ssh", cmd)


def cmd_shutdown(args: argparse.Namespace) -> None:
    cfg = load_or_init_config()
    box = find_box(args.name, cfg)
    if box is None:
        die(f"no box named {args.name!r}")
    print(f"==> {args.name}: state={box['state']}, instance={box['instance_id']}")
    if not args.yes:
        if input(f"terminate {args.name}? [y/N] ").strip().lower() != "y":
            print("aborted")
            return

    print(f"==> terminating instance")
    aws(["ec2", "terminate-instances", "--instance-ids", box["instance_id"]], cfg)
    print(f"==> waiting for instance termination")
    aws(["ec2", "wait", "instance-terminated", "--instance-ids", box["instance_id"]], cfg, capture=False)

    # Root EBS deletes on termination by default. Just clean up the SG.
    for sg_id in box["sg_ids"]:
        out = aws([
            "ec2", "describe-security-groups",
            "--group-ids", sg_id,
            "--query", "SecurityGroups[0].GroupName", "--output", "text",
        ], cfg).strip()
        if out.startswith("ray-devbox-"):
            r = subprocess.run([
                "aws", "ec2", "delete-security-group",
                "--profile", cfg["aws_profile"], "--region", cfg["aws_region"],
                "--no-cli-pager", "--group-id", sg_id,
            ], env=aws_env(cfg["aws_profile"]), capture_output=True, text=True)
            status = "deleted" if r.returncode == 0 else f"skipped ({r.stderr.strip()[:80]})"
            print(f"    sg {sg_id} ({out}): {status}")

    boxes = discover(cfg)
    write_ssh_config(boxes)
    print(f"==> {args.name} terminated")


# ---------- entry point ----------

def main() -> None:
    parser = argparse.ArgumentParser(prog="launch_devbox", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("launch", help="launch + bootstrap a new dev box")
    s.add_argument("--name", required=True)
    s.add_argument("--type", required=True, help="e.g. m7i.4xlarge, g6.12xlarge")
    s.add_argument("--region", help="override region (default from config)")
    s.add_argument("--disk-gb", type=int, default=200)
    s.set_defaults(func=cmd_launch)

    s = sub.add_parser("list", help="list ray dev boxes")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("ssh", help="ssh into a box by name")
    s.add_argument("name")
    s.add_argument("extra", nargs=argparse.REMAINDER)
    s.set_defaults(func=cmd_ssh)

    s = sub.add_parser("shutdown", help="terminate a box")
    s.add_argument("name")
    s.add_argument("-y", "--yes", action="store_true")
    s.set_defaults(func=cmd_shutdown)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
