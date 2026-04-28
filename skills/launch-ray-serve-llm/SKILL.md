---
name: launch-ray-serve-llm
description: Deploy a Ray Serve LLM model on KubeRay as a RayService — given an existing EKS/GKE/AKS cluster with the KubeRay operator already installed (e.g. one created by the launch-eks skill). Maps a vLLM CLI invocation onto a `LLMConfig` + `RayService` manifest, sets the env vars that actually make first-pull downloads fast, and includes the gotchas that bite first-time deployments.
---

# launch-ray-serve-llm

Stand up an OpenAI-compatible LLM endpoint on a Kubernetes cluster you already have. Assumes:
- KubeRay operator is running (e.g. cluster was provisioned by `launch-eks`, or `helm install kuberay-operator kuberay/kuberay-operator -n kuberay-system --create-namespace`).
- `kubectl` is pointed at it.
- A GPU node pool exists (Karpenter-managed or otherwise) that can satisfy the worker pod's `nvidia.com/gpu` requests + the right tolerations/nodeSelector.

## When to use

- Translating a `vllm serve <model> --flag1 --flag2 ...` invocation into a Ray-Serve-LLM manifest.
- Adding tool calling + reasoning parsers (`tool_call_parser`, `reasoning_parser`) on top of vLLM via Ray Serve LLM's OpenAI ingress.
- Choosing the right env vars for fast cold-start model downloads, throughput, and HA proxies.

## Quick start

1. Pick or render a `RayService` YAML (see `examples/`).
2. (Optional) Create the HF token secret if you want higher Hub rate limits or the model is gated:
   ```sh
   kubectl create secret generic hf-token --from-literal=token=$HF_TOKEN
   ```
3. `kubectl apply -f rayservice.yaml`
4. Watch: `kubectl get rayservice -w` until `SERVICE STATUS=Running` and `NUM SERVE ENDPOINTS` equals the number of Ray pods (head + workers).
5. Smoke test:
   ```sh
   kubectl port-forward svc/<name>-serve-svc 18000:8000 &
   curl -s http://localhost:18000/v1/models | jq
   ```

Cold start is dominated by image pull (~5 min for `rayproject/ray-llm`) + model download (variable — see HF Hub section below) + vLLM warmup/CUDA graph capture (~3 min).

## Mapping vLLM CLI → LLMConfig

vLLM's OpenAI-server CLI args map onto Ray Serve LLM's `LLMConfig` like this. Reference: `python/ray/llm/_internal/serve/core/configs/llm_config.py`, `python/ray/llm/_internal/serve/engines/vllm/vllm_models.py`.

| vLLM CLI arg                            | LLMConfig field                                                       |
|-----------------------------------------|-----------------------------------------------------------------------|
| `<MODEL>` (positional)                  | `model_loading_config.model_id` (or `model_source` if different from id) |
| `--trust-remote-code`                   | `engine_kwargs.trust_remote_code: true`                               |
| `--tensor-parallel-size N`              | `engine_kwargs.tensor_parallel_size: N`                               |
| `--max-model-len N`                     | `engine_kwargs.max_model_len: N`                                      |
| `--enable-auto-tool-choice`             | `engine_kwargs.enable_auto_tool_choice: true`                         |
| `--tool-call-parser X`                  | `engine_kwargs.tool_call_parser: X`                                   |
| `--reasoning-parser X`                  | `engine_kwargs.reasoning_parser: X`                                   |
| `--speculative-config '{...}'`          | `engine_kwargs.speculative_config: { ... }` (dict, not JSON string)   |
| `--quantization fp8`                    | usually inferred from the model weights; otherwise `engine_kwargs.quantization: fp8` |

`engine_kwargs` is forwarded verbatim to vLLM's `AsyncEngineArgs` + `FrontendArgs`, so any other vLLM flag works under its snake_case name. The Serve app entrypoint is `ray.serve.llm:build_openai_app`, fed `{ "llm_configs": [<one or more configs>] }`.

## Critical env vars

These are not optional knobs — they materially change deployment behavior. Set on **both** the head and worker container `env`:

### Fast model download (the most important one)

```yaml
- name: HF_HUB_DISABLE_XET
  value: "1"
- name: HF_HUB_ENABLE_HF_TRANSFER
  value: "1"
```

**Why both:** HuggingFace migrated to a Xet-based storage backend. The newer `hf_xet` client (auto-used when installed) chunked-deduplicates against `cas-server.xethub.hf.co` and is dramatically slower than the legacy LFS path for cold first-pulls — empirically ~6 MB/s vs. ~500 MB/s. Setting `HF_HUB_DISABLE_XET=1` forces the legacy path; `HF_HUB_ENABLE_HF_TRANSFER=1` then activates the Rust-based parallel chunked downloader (which only kicks in on the legacy path). Documentation calls `HF_HUB_ENABLE_HF_TRANSFER` "deprecated" — what they mean is "ignored when Xet is the backend"; with Xet disabled, it's required.

`HF_XET_HIGH_PERFORMANCE=1` is the recommended Xet-native equivalent, but in our testing it left ~10× speedup on the table compared to the legacy pipeline. Skip Xet for first-pulls.

Concrete numbers from a 2026-04 deploy of `Qwen/Qwen3.6-35B-A3B-FP8` (~37 GB) on `g6e.12xlarge`:

| Config                                                         | Effective rate | ETA for 37 GB |
|----------------------------------------------------------------|----------------|---------------|
| Default (Xet via hf_xet)                                       | ~0.65 MB/s     | **~16 hours** |
| `HF_XET_HIGH_PERFORMANCE=1`                                    | ~6 MB/s        | ~100 min      |
| `HF_HUB_DISABLE_XET=1` + `HF_HUB_ENABLE_HF_TRANSFER=1`         | **~500 MB/s**  | **~70 sec**   |

Authenticated requests (HF token) raise the per-account rate limit but do not change the protocol bottleneck. The fix is the env-var pair, not the token; the token helps as a secondary win.

### Other useful Ray Serve env vars

```yaml
- name: RAY_SERVE_THROUGHPUT_OPTIMIZED
  value: "1"
- name: RAY_SERVE_ENABLE_HA_PROXY     # requires a recent Ray; verify before enabling
  value: "1"
```

`RAY_SERVE_THROUGHPUT_OPTIMIZED=1` disables some per-request tracing overhead. `RAY_SERVE_ENABLE_HA_PROXY=1` swaps Ray Serve's Python HTTP proxy for HAProxy with an `HAProxyManager` actor on each Ray node — improved throughput and lower latency at high QPS. The HA proxy code path was added relatively recently; if you're on an older Ray pin, the env var is silently ignored (no error, no HAProxy actors). Verify before enabling:

```sh
kubectl exec <head-pod> -- python -c \
  "from ray.serve._private.constants import RAY_SERVE_ENABLE_HA_PROXY; print('flag visible:', RAY_SERVE_ENABLE_HA_PROXY)"
kubectl get pods -l ray.io/serve=true -o name | xargs -I{} kubectl exec {} -- pgrep -af haproxy
```

If the import errors or no `haproxy` processes appear after the RayService is `Running`, your image's Ray is too old.

## Anatomy of a working RayService

See `examples/qwen3.6-35b-a3b-fp8-l40s.yaml` for the canonical reference. Key shape:

```yaml
apiVersion: ray.io/v1
kind: RayService
metadata: { name: <name> }
spec:
  serviceUnhealthySecondThreshold: 1800
  deploymentUnhealthySecondThreshold: 1800
  serveConfigV2: |
    applications:
    - name: llm_app
      route_prefix: "/"
      import_path: ray.serve.llm:build_openai_app
      args:
        llm_configs:
          - model_loading_config:
              model_id: <hf-model-id>            # also acts as served_model_name in OpenAI API
            accelerator_type: L40S               # Ray-side custom resource (auto-detected from NVML; this filters placement to L40S nodes)
            deployment_config:
              autoscaling_config: { min_replicas: 1, max_replicas: N }
            engine_kwargs:
              tensor_parallel_size: 2
              # ... other engine_kwargs ...
            runtime_env:
              env_vars:
                HF_HUB_DISABLE_XET: "1"
                HF_HUB_ENABLE_HF_TRANSFER: "1"
  rayClusterConfig:
    rayVersion: "3.0.0.dev0"   # match the image's `ray --version`
    headGroupSpec:
      template:
        spec:
          nodeSelector: { ray-eks-pool: cpu-head }   # match your CPU pool label
          containers: [{ image: rayproject/ray-llm:nightly-py311-cu128, ... }]
    workerGroupSpecs:
      - groupName: gpu
        template:
          spec:
            nodeSelector: { ray-eks-pool: gpu-l40s } # match your GPU pool label
            tolerations:
              - { key: nvidia.com/gpu, operator: Equal, value: "true", effect: NoSchedule }
            containers: [{
              image: rayproject/ray-llm:nightly-py311-cu128,
              resources: { requests/limits: { nvidia.com/gpu: 2, ... } },
              ports: [{ name: serve, containerPort: 8000 }],   # for clarity; K8s services route by targetPort regardless
              env: [
                # secret reference for HF token (optional: true makes it work without the secret)
                { name: HUGGING_FACE_HUB_TOKEN, valueFrom: { secretKeyRef: { name: hf-token, key: token, optional: true } } },
                { name: HF_HUB_DISABLE_XET,         value: "1" },
                { name: HF_HUB_ENABLE_HF_TRANSFER,  value: "1" },
                { name: RAY_SERVE_THROUGHPUT_OPTIMIZED, value: "1" },
                { name: RAY_SERVE_ENABLE_HA_PROXY,  value: "1" },
              ]
            }]
```

GPU resource math: with `nvidia.com/gpu: N` per pod and `tensor_parallel_size: M`, set `N == M` so each replica gets exactly one TP group. Karpenter (or your autoscaler) provisions a node big enough to hold `N` GPUs in one VM.

## Routing and HA proxy

KubeRay creates `<rayservice-name>-serve-svc` (ClusterIP) selecting all Ray pods with label `ray.io/serve=true`. `proxy_location: EveryNode` (default since Ray 2.x — don't bother setting it explicitly) puts an HTTP proxy on every Ray node, so the Service has both head + workers as endpoints. kube-proxy load-balances client connections across them (random per-connection in the default iptables mode, true round-robin in IPVS mode); the proxy on the receiving pod then forwards to a Serve replica. With `RAY_SERVE_ENABLE_HA_PROXY=1`, that proxy is HAProxy.

Implication: only one pod template needs to publish `containerPort: 8000` for traffic to reach (K8s routes by `targetPort`), but declare it on both head and worker for clarity / NetworkPolicies / observability.

## Troubleshooting

- **`WaitForServeDeploymentReady` for >10 min**: usually the model download is slow. Check `du -sh /home/ray/.cache/huggingface/` inside the worker pod. If <100 MB/min, you're on the slow Xet path — confirm the env vars actually got into the worker (`kubectl exec <worker> -c ray-worker -- env | grep HF_`) and re-roll if not.
- **Replica timeouts during init**: vLLM's CUDA graph capture across compile sizes 1–512 takes several minutes for 30B-class MoE models. The "30s init" warning in the controller log is informational; real failure shows up as repeated replica restarts.
- **MTP speculative decoding silent failure**: only some checkpoints ship MTP heads (look for `mtp.safetensors` in the model's HF `siblings`). vLLM logs `Resolved architecture: <X>MTP` and `Loading drafter model... Detected MTP model. Sharing target model embedding/lm_head weights` when it works. If those lines don't appear, drop `speculative_config` and try `{method: ngram, num_speculative_tokens: 2, prompt_lookup_max: 4}` which works on any model.
- **Tool-call parser empty `tool_calls: []`**: usually `max_tokens` was hit during the model's `<think>` reasoning phase before it got to the tool call. Bump `max_tokens` to 1500+ for Qwen3-style reasoning models.
- **`serve_status=Restarting` after a config change**: KubeRay does blue/green RayCluster rotations on `rayClusterConfig` changes. The new RayCluster is created alongside, then traffic flips when it's healthy — this can take ~20 min for LLM workloads.
- **`reasoning` field is empty / mixed with `content`**: confirm `reasoning_parser: <name>` matches a parser registered in your vLLM build (`python -c "from vllm.reasoning import ReasoningParserManager; print(ReasoningParserManager.registry.keys())"` inside the container).

## Image pinning

Ray Serve LLM moves fast. The image must include:
- A `ray.serve.llm` module (Ray ≥ 2.40, ideally a recent nightly).
- A vLLM new enough for any tool/reasoning parser you reference (e.g. `qwen3_coder` parser only landed in vLLM v0.10+).
- `hf_transfer` (nearly always preinstalled) and `huggingface_hub`.

Recommended starting point: `rayproject/ray-llm:nightly-py311-cu128`. Verify inside the container before applying:
```sh
kubectl exec <head-pod> -- python -c "
import vllm, transformers
print('vllm', vllm.__version__, 'transformers', transformers.__version__)
from vllm.entrypoints.openai.tool_parsers import ToolParserManager
print('parsers:', list(ToolParserManager.tool_parsers.keys()))
from vllm.reasoning import ReasoningParserManager
print('reasoning:', list(ReasoningParserManager.registry.keys()))
"
```

If the parser you want isn't listed, bump to a newer nightly.

## Examples

- `examples/qwen3.6-35b-a3b-fp8-l40s.yaml` — Qwen3.6 MoE, FP8, TP=2 across L40S, MTP speculative, qwen3_coder tool parser, qwen3 reasoning parser, HA proxy. Mirrors the 2026-04 reference deploy.

## Notes / gotchas

- **Don't set `HF_HUB_ENABLE_HF_TRANSFER=1` without also setting `HF_HUB_DISABLE_XET=1`** if `hf_xet` is installed in the image. The transfer flag is silently ignored on the Xet path.
- **`serveConfigV2.runtime_env.env_vars` only applies to the Serve actor processes**, not to the Ray worker process that hosts them. For env vars vLLM reads at engine init (which is most of them, including the HF download knobs), set them on the **container `env`** as well — that's the only way they reach the EngineCore subprocess. The provided example does both, defensively.
- **`accelerator_type`** in `LLMConfig` is a Ray-side custom resource string (e.g. `L40S`, `H100`, `L4`), not a Kubernetes node label. Ray's raylet auto-detects it from NVML at startup; you do not need to put `accelerator: <X>` labels on K8s nodes for placement to work. (You can still put them as K8s nodeSelector hints for pod-level routing, but Ray scheduling is independent of those.)
- **Image pull on a fresh GPU node is slow** (~5–8 min for `ray-llm`'s ~13 GB image). Pre-warm with a tiny Deployment that pulls the image but requests no GPU if you need fast cold starts.
- **`max_replicas` in `autoscaling_config`** is a Serve-level cap. Coordinate it with the worker group's `maxReplicas` and the GPU pool's `max` to avoid overcommit. Math: `serve_max_replicas * gpus_per_replica <= gpu_pool_max * gpus_per_node`.
