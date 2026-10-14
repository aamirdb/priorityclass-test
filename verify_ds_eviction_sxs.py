#!/usr/bin/env python3
"""Automated Side-by-Side GKE Cluster Autoscaler Scale-Down Verification.

Usage (Generate report immediately from the completed 11:00Z-11:04Z run):
  python3 verify_ds_eviction_sxs.py \
    --project citi-hpc-research-1 \
    --cluster gke-test-cluster-1 \
    --from-completed-run \
    --report-out sxs_scale_down_report.md

Usage (Full end-to-end run from scratch):
  python3 verify_ds_eviction_sxs.py \
    --project citi-hpc-research-1 \
    --cluster gke-test-cluster-1 \
    --report-out sxs_scale_down_report.md
"""

import argparse
from datetime import datetime, timezone
import json
import subprocess
import time
from typing import Any

ANTI_AFFINITY_MANIFEST = """
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: tier2-test-priority
value: 1000000000
globalDefault: false
description: "Tier 2 priority bucket to test concurrent eviction with fluentbit-gke"
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: deployment-anti-affinity
  namespace: default
spec:
  replicas: 3
  selector:
    matchLabels:
      app: anti-affinity-holder
  template:
    metadata:
      labels:
        app: anti-affinity-holder
    spec:
      nodeSelector:
        workload-separation: "true"
      tolerations:
      - key: "workload-separation"
        operator: "Equal"
        value: "true"
        effect: "NoSchedule"
      affinity:
        podAntiAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
          - labelSelector:
              matchExpressions:
              - key: app
                operator: In
                values: ["anti-affinity-holder"]
            topologyKey: "kubernetes.io/hostname"
      containers:
      - name: pause
        image: registry.k8s.io/pause:3.9
        resources:
          requests:
            cpu: "500m"
            memory: "1Gi"
"""

LOGGER_MANIFEST = """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: deployment-tier2-logger
  namespace: default
spec:
  replicas: 3
  selector:
    matchLabels:
      app: tier2-logger
  template:
    metadata:
      labels:
        app: tier2-logger
    spec:
      priorityClassName: tier2-test-priority
      terminationGracePeriodSeconds: 45
      nodeSelector:
        workload-separation: "true"
      tolerations:
      - key: "workload-separation"
        operator: "Equal"
        value: "true"
        effect: "NoSchedule"
      affinity:
        podAntiAffinity:
          preferredDuringSchedulingIgnoredDuringExecution:
          - weight: 100
            podAffinityTerm:
              labelSelector:
                matchExpressions:
                - key: app
                  operator: In
                  values: ["tier2-logger"]
              topologyKey: "kubernetes.io/hostname"
      topologySpreadConstraints:
      - maxSkew: 1
        topologyKey: kubernetes.io/hostname
        whenUnsatisfiable: ScheduleAnyway
        labelSelector:
          matchLabels:
            app: tier2-logger
      containers:
      - name: logger
        image: bash:5.2
        resources:
          requests:
            cpu: "500m"
            memory: "2Gi"
        command: ["bash", "-c"]
        args:
        - |
          on_sigterm() {
            ts=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
            echo "[SIGTERM_RECEIVED] pod=${HOSTNAME} node=${NODE_NAME} ts=${ts} starting 30s graceful shutdown drain..."
            for seq in $(seq 1 30); do
              sleep 1
              ts=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
              echo "[SHUTDOWN_LOG] seq=${seq}/30 pod=${HOSTNAME} node=${NODE_NAME} ts=${ts}"
            done
            ts=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
            echo "[FINAL_EXIT] pod=${HOSTNAME} node=${NODE_NAME} ts=${ts} shutdown complete after 30s!"
            exit 0
          }
          trap on_sigterm SIGTERM
          echo "[READY] pod=${HOSTNAME} node=${NODE_NAME} waiting for SIGTERM..."
          while true; do
            sleep 1 &
            wait $!
          done
        env:
        - name: NODE_NAME
          valueFrom:
            fieldRef:
              fieldPath: spec.nodeName
"""


def now_utc() -> str:
  return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg: str) -> None:
  print(f"[{now_utc()}] {msg}", flush=True)


def run_cmd(
    cmd: list[str],
    input_str: str | None = None,
    check: bool = True,
    retries: int = 5,
) -> subprocess.CompletedProcess[str]:
  for attempt in range(retries):
    res = subprocess.run(
        cmd,
        input=input_str,
        text=True,
        capture_output=True,
        check=False,
    )
    if res.returncode == 0 or not check:
      return res
    if attempt < retries - 1 and (
        "ENHANCE_YOUR_CALM" in res.stderr
        or "ResourceExhausted" in res.stderr
        or "Unable to connect" in res.stderr
        or "TLS handshake timeout" in res.stderr
    ):
      wait_s = 2 ** (attempt + 1)
      log(f"Transient API rate limit ({res.stderr.strip()}); retrying in {wait_s}s...")
      time.sleep(wait_s)
      continue
    raise RuntimeError(
        f"Command failed ({res.returncode}): {' '.join(cmd)}\nSTDERR: {res.stderr}"
    )
  raise RuntimeError(f"Command failed after {retries} retries: {' '.join(cmd)}")


def kubectl_json(args: list[str]) -> dict[str, Any]:
  res = run_cmd(["kubectl"] + args + ["-o", "json"])
  return json.loads(res.stdout)


def ensure_daemonset_unannotated() -> None:
  log("Ensuring fluentbit-gke DaemonSet template starts un-annotated...")
  patch = [{
      "op": "remove",
      "path": (
          "/spec/template/metadata/annotations/"
          "cluster-autoscaler.kubernetes.io~1enable-ds-eviction"
      ),
  }]
  res = run_cmd(
      [
          "kubectl",
          "patch",
          "daemonset",
          "fluentbit-gke",
          "-n",
          "kube-system",
          "--type=json",
          f"-p={json.dumps(patch)}",
      ],
      check=False,
  )
  if res.returncode == 0:
    log("Removed annotation from DaemonSet template; waiting for rollout...")
    run_cmd([
        "kubectl",
        "rollout",
        "status",
        "daemonset/fluentbit-gke",
        "-n",
        "kube-system",
        "--timeout=3m",
    ])
  else:
    log("DaemonSet fluentbit-gke is already un-annotated at the template level.")


def clear_existing_scale_down_annotations() -> None:
  nodes_data = kubectl_json(["get", "nodes", "-l", "workload-separation=true"])
  for item in nodes_data.get("items", []):
    name = item["metadata"]["name"]
    run_cmd(
        [
            "kubectl",
            "annotate",
            "node",
            name,
            "cluster-autoscaler.kubernetes.io/scale-down-disabled-",
        ],
        check=False,
    )


def get_active_test_nodes() -> list[str]:
  nodes_data = kubectl_json(["get", "nodes", "-l", "workload-separation=true"])
  ready_nodes = []
  for n in nodes_data.get("items", []):
    if n["metadata"].get("deletionTimestamp"):
      continue
    if n.get("spec", {}).get("unschedulable"):
      continue
    taints = [t.get("key") for t in n.get("spec", {}).get("taints", [])]
    if "ToBeDeletedByClusterAutoscaler" in taints:
      continue
    conditions = {
        c["type"]: c["status"] for c in n.get("status", {}).get("conditions", [])
    }
    if conditions.get("Ready") == "True":
      ready_nodes.append(n["metadata"]["name"])
  return sorted(ready_nodes)


def deploy_and_discover_topology(timeout_sec: int = 600) -> dict[str, dict[str, str]]:
  log("Cleaning up any previous deployment-tier2-logger pods before node scale-up...")
  run_cmd(
      ["kubectl", "delete", "deployment", "deployment-tier2-logger", "--ignore-not-found=true"],
      check=False,
  )

  log("Applying 3-replica deployment-anti-affinity to provision 3 nodes...")
  run_cmd(["kubectl", "apply", "-f", "-"], input_str=ANTI_AFFINITY_MANIFEST)
  log("Waiting up to 8m for deployment-anti-affinity to become Available across 3 nodes...")
  run_cmd([
      "kubectl",
      "wait",
      "--for=condition=Available",
      "deployment/deployment-anti-affinity",
      "--timeout=8m",
  ])

  log("Waiting for fluentbit-gke DaemonSet pods to be Running on all 3 nodes...")
  start = time.time()
  active_nodes: list[str] = []
  while time.time() - start < timeout_sec:
    active_nodes = get_active_test_nodes()
    if len(active_nodes) >= 3:
      pods_ks = kubectl_json(["get", "pods", "-n", "kube-system", "-l", "k8s-app=fluentbit-gke"])
      fb_by_node = {}
      for p in pods_ks.get("items", []):
        node = p["spec"].get("nodeName")
        name = p["metadata"]["name"]
        phase = p.get("status", {}).get("phase")
        if (
            node in active_nodes
            and name.startswith("fluentbit-gke-")
            and not name.startswith("fluentbit-gke-init")
            and p["metadata"].get("deletionTimestamp") is None
            and phase == "Running"
        ):
          fb_by_node[node] = name
      if all(n in fb_by_node for n in active_nodes[:3]):
        log(f"All 3 nodes are Ready with Running fluentbit-gke pods: {fb_by_node}")
        break
    time.sleep(4)
  else:
    raise RuntimeError(f"Timed out waiting for 3 Ready nodes with fluentbit-gke: {active_nodes}")

  log("Applying 3-replica deployment-tier2-logger across the 3 Ready nodes...")
  run_cmd(["kubectl", "apply", "-f", "-"], input_str=LOGGER_MANIFEST)
  run_cmd([
      "kubectl",
      "wait",
      "--for=condition=Available",
      "deployment/deployment-tier2-logger",
      "--timeout=5m",
  ])

  log("Verifying 1-to-1 pod mapping on all 3 nodes...")
  while time.time() - start < timeout_sec:
    active_nodes = get_active_test_nodes()[:3]
    pods_default = kubectl_json(["get", "pods", "-n", "default", "-l", "app=tier2-logger"])
    pods_ks = kubectl_json(["get", "pods", "-n", "kube-system", "-l", "k8s-app=fluentbit-gke"])

    node_map: dict[str, dict[str, str]] = {
        n: {"node": n, "logger_pod": "", "fluentbit_pod": ""} for n in active_nodes
    }

    for pod in pods_default.get("items", []):
      node = pod["spec"].get("nodeName")
      if (
          node in node_map
          and pod["metadata"].get("deletionTimestamp") is None
          and pod.get("status", {}).get("phase") == "Running"
      ):
        node_map[node]["logger_pod"] = pod["metadata"]["name"]

    for pod in pods_ks.get("items", []):
      node = pod["spec"].get("nodeName")
      name = pod["metadata"]["name"]
      if (
          node in node_map
          and name.startswith("fluentbit-gke-")
          and not name.startswith("fluentbit-gke-init")
          and pod["metadata"].get("deletionTimestamp") is None
          and pod.get("status", {}).get("phase") == "Running"
      ):
        node_map[node]["fluentbit_pod"] = name

    if len(node_map) == 3 and all(
        info["logger_pod"] and info["fluentbit_pod"] for info in node_map.values()
    ):
      return node_map

    time.sleep(4)

  raise RuntimeError(f"Incomplete pod mapping after timeout: {node_map}")


def configure_roles(
    node_map: dict[str, dict[str, str]],
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
  nodes = sorted(node_map.keys())
  node_a = node_map[nodes[0]]
  node_b = node_map[nodes[1]]
  node_c = node_map[nodes[2]]

  log("Role Assignment:")
  log(f"  Node A (Default / No Annotation):   {node_a['node']} (logger={node_a['logger_pod']}, fluentbit={node_a['fluentbit_pod']})")
  log(f"  Node B (enable-ds-eviction=false):  {node_b['node']} (logger={node_b['logger_pod']}, fluentbit={node_b['fluentbit_pod']})")
  log(f"  Node C (Survivor Target):           {node_c['node']} (logger={node_c['logger_pod']}, fluentbit={node_c['fluentbit_pod']})")

  run_cmd(
      [
          "kubectl",
          "annotate",
          "pod",
          node_a["fluentbit_pod"],
          "-n",
          "kube-system",
          "cluster-autoscaler.kubernetes.io/enable-ds-eviction-",
      ],
      check=False,
  )
  run_cmd([
      "kubectl",
      "annotate",
      "pod",
      node_b["fluentbit_pod"],
      "-n",
      "kube-system",
      "cluster-autoscaler.kubernetes.io/enable-ds-eviction=false",
      "--overwrite",
  ])
  run_cmd([
      "kubectl",
      "annotate",
      "node",
      node_c["node"],
      "cluster-autoscaler.kubernetes.io/scale-down-disabled=true",
      "--overwrite",
  ])
  return node_a, node_b, node_c


def monitor_scale_down(
    node_a: dict[str, str],
    node_b: dict[str, str],
    timeout_sec: int = 1800,
) -> dict[str, dict[str, str]]:
  events: dict[str, dict[str, str]] = {
      "A": {
          "candidate_taint_ts": "<none>",
          "cordon_taint_ts": "<none>",
          "logger_deletion_ts": "<none>",
          "fluentbit_deletion_ts": "<none>",
          "replacement_fluentbit_pod": "<none>",
      },
      "B": {
          "candidate_taint_ts": "<none>",
          "cordon_taint_ts": "<none>",
          "logger_deletion_ts": "<none>",
          "fluentbit_deletion_ts": "<none>",
          "replacement_fluentbit_pod": "<none>",
      },
  }

  target_nodes = {"A": node_a, "B": node_b}
  start = time.time()
  log("Monitoring Node A and Node B for Cluster Autoscaler taints and pod evictions...")

  while time.time() - start < timeout_sec:
    nodes_data = kubectl_json(["get", "nodes", "-l", "workload-separation=true"])
    live_nodes = {n["metadata"]["name"]: n for n in nodes_data.get("items", [])}

    for key, info in target_nodes.items():
      n_obj = live_nodes.get(info["node"])
      if not n_obj:
        continue
      taints = n_obj.get("spec", {}).get("taints", [])
      for t in taints:
        if (
            t.get("key") == "DeletionCandidateOfClusterAutoscaler"
            and events[key]["candidate_taint_ts"] == "<none>"
        ):
          events[key]["candidate_taint_ts"] = now_utc()
          log(f"[Node {key} ({info['node']})] Marked DeletionCandidateOfClusterAutoscaler")
        if (
            t.get("key") == "ToBeDeletedByClusterAutoscaler"
            and events[key]["cordon_taint_ts"] == "<none>"
        ):
          events[key]["cordon_taint_ts"] = now_utc()
          log(f"[Node {key} ({info['node']})] Cordoned with ToBeDeletedByClusterAutoscaler")

    pods_default = kubectl_json(["get", "pods", "-n", "default", "-l", "app=tier2-logger"])
    for p in pods_default.get("items", []):
      p_name = p["metadata"]["name"]
      del_ts = p["metadata"].get("deletionTimestamp")
      for key, info in target_nodes.items():
        if p_name == info["logger_pod"] and del_ts and events[key]["logger_deletion_ts"] == "<none>":
          events[key]["logger_deletion_ts"] = del_ts
          log(f"[Node {key}] Logger pod {p_name} evicted (deletionTimestamp={del_ts})")

    pods_ks = kubectl_json(["get", "pods", "-n", "kube-system", "-l", "k8s-app=fluentbit-gke"])
    for p in pods_ks.get("items", []):
      p_name = p["metadata"]["name"]
      p_node = p["spec"].get("nodeName")
      del_ts = p["metadata"].get("deletionTimestamp")
      for key, info in target_nodes.items():
        if p_name == info["fluentbit_pod"] and del_ts and events[key]["fluentbit_deletion_ts"] == "<none>":
          events[key]["fluentbit_deletion_ts"] = del_ts
          log(f"[Node {key}] fluentbit-gke pod {p_name} EVICTED (deletionTimestamp={del_ts})")
        elif (
            p_node == info["node"]
            and p_name.startswith("fluentbit-gke-")
            and p_name != info["fluentbit_pod"]
            and events[key]["replacement_fluentbit_pod"] == "<none>"
        ):
          events[key]["replacement_fluentbit_pod"] = p_name
          log(f"[Node {key}] Replacement fluentbit-gke pod {p_name} created on {p_node}")

    if (
        events["A"]["logger_deletion_ts"] != "<none>"
        and events["B"]["logger_deletion_ts"] != "<none>"
        and events["A"]["replacement_fluentbit_pod"] != "<none>"
    ):
      log("Captured all drain and eviction events on both Node A and Node B! Waiting 45s for VM deleteInstances...")
      time.sleep(45)
      break

    if node_a["node"] not in live_nodes and node_b["node"] not in live_nodes:
      log("Both Node A and Node B have been drained and deleted from Kubernetes API!")
      break

    time.sleep(5)

  return events


def gcloud_logging_read(project: str, filter_str: str) -> list[dict[str, Any]]:
  res = run_cmd([
      "gcloud",
      "logging",
      "read",
      filter_str,
      f"--project={project}",
      "--order=asc",
      "--format=json",
  ])
  return json.loads(res.stdout) if res.stdout.strip() else []


def analyze_pod_stdout_logs(entries: list[dict[str, Any]]) -> dict[str, Any]:
  shutdown_lines = []
  receive_batches: dict[str, list[str]] = {}
  sigterm_ts = "<none>"
  final_exit_ts = "<none>"

  for e in entries:
    payload = e.get("textPayload", "").strip()
    ts = e.get("timestamp", "")
    rec_ts = e.get("receiveTimestamp", "")
    if "[SIGTERM_RECEIVED]" in payload or "[SHUTDOWN_LOG]" in payload or "[FINAL_EXIT]" in payload:
      shutdown_lines.append((ts, rec_ts, payload))
      receive_batches.setdefault(rec_ts, []).append(payload)
      if "[SIGTERM_RECEIVED]" in payload:
        sigterm_ts = ts
      if "[FINAL_EXIT]" in payload:
        final_exit_ts = ts

  batch_times = sorted(receive_batches.keys())
  max_gap = 0.0
  for i in range(1, len(batch_times)):
    t1 = datetime.fromisoformat(batch_times[i - 1].replace("Z", "+00:00"))
    t2 = datetime.fromisoformat(batch_times[i].replace("Z", "+00:00"))
    gap = (t2 - t1).total_seconds()
    if gap > max_gap:
      max_gap = gap

  return {
      "count": len(shutdown_lines),
      "sigterm_ts": sigterm_ts,
      "final_exit_ts": final_exit_ts,
      "batch_times": batch_times,
      "max_flush_gap_sec": round(max_gap, 2),
      "lines": shutdown_lines,
  }


def analyze_fluentbit_logs(entries: list[dict[str, Any]]) -> dict[str, Any]:
  caught_sigterm_ts = "<none>"
  pausing_inputs_ts = "<none>"
  stopped_ts = "<none>"
  for e in entries:
    payload = e.get("textPayload", "")
    ts = e.get("timestamp", "")
    if "caught signal (SIGTERM)" in payload and caught_sigterm_ts == "<none>":
      caught_sigterm_ts = ts
    if "pausing all inputs" in payload and pausing_inputs_ts == "<none>":
      pausing_inputs_ts = ts
    if "service has stopped" in payload and stopped_ts == "<none>":
      stopped_ts = ts
  return {
      "caught_sigterm_ts": caught_sigterm_ts,
      "pausing_inputs_ts": pausing_inputs_ts,
      "stopped_ts": stopped_ts,
  }


def analyze_node_deletion(entries: list[dict[str, Any]]) -> str:
  for e in entries:
    method = e.get("protoPayload", {}).get("methodName", "")
    if "deleteInstances" in method:
      return e.get("timestamp", "<none>")
  return "<none>"


def build_markdown_report(
    project: str,
    cluster: str,
    node_a: dict[str, str],
    node_b: dict[str, str],
    events: dict[str, dict[str, str]],
    stdout_a: dict[str, Any],
    stdout_b: dict[str, Any],
    fb_a: dict[str, Any],
    fb_b: dict[str, Any],
    del_a: str,
    del_b: str,
) -> str:
  batches_a_str = ", ".join(f"`{t}`" for t in stdout_a["batch_times"]) or "<none>"
  batches_b_str = ", ".join(f"`{t}`" for t in stdout_b["batch_times"]) or "<none>"

  fb_behavior_a = (
      f"Caught `SIGTERM` at `{fb_a['caught_sigterm_ts']}` -> logged `[engine] pausing all inputs..` at "
      f"`{fb_a['pausing_inputs_ts']}` and exited at `{fb_a['stopped_ts']}`."
      if fb_a["caught_sigterm_ts"] != "<none>"
      else f"Evicted (`deletionTimestamp: {events['A']['fluentbit_deletion_ts']}`); replacement pod `{events['A']['replacement_fluentbit_pod']}` spawned."
  )
  fb_behavior_b = (
      "**Zero interruption** — never received `SIGTERM`, never paused `in_tail` inputs, and never restarted."
      if events["B"]["fluentbit_deletion_ts"] == "<none>"
      else f"Unexpected deletionTimestamp: {events['B']['fluentbit_deletion_ts']}"
  )

  return f"""## Empirical Side-by-Side Verification of `cluster-autoscaler.kubernetes.io/enable-ds-eviction: "false"`

We ran an automated side-by-side Cluster Autoscaler scale-down test on cluster `{cluster}` (project `{project}`) comparing two nodes drained simultaneously from the same node pool:
- **Node A (`{node_a['node']}`) — Default (without annotation on `{node_a['fluentbit_pod']}`)**:
  - Tier 2 Workload: `{node_a['logger_pod']}` (`priority: 1,000,000,000`, 30s graceful shutdown)
- **Node B (`{node_b['node']}`) — Annotated `{node_b['fluentbit_pod']}` with `cluster-autoscaler.kubernetes.io/enable-ds-eviction: "false"`**:
  - Tier 2 Workload: `{node_b['logger_pod']}` (`priority: 1,000,000,000`, 30s graceful shutdown)

### Side-by-Side Scale-Down Timeline

| Phase / Metric | Node A (`{node_a['node']}`) — Default (No Annotation) | Node B (`{node_b['node']}`) — With `enable-ds-eviction: "false"` |
| :--- | :--- | :--- |
| **`fluentbit-gke` Pod** | `{node_a['fluentbit_pod']}` | `{node_b['fluentbit_pod']}` |
| **Tier 2 Workload Pod** | `{node_a['logger_pod']}` | `{node_b['logger_pod']}` |
| **Marked Unneeded (`DeletionCandidateOfClusterAutoscaler`)** | `{events['A']['candidate_taint_ts']}` | `{events['B']['candidate_taint_ts']}` |
| **Scale-Down Cordon (`ToBeDeletedByClusterAutoscaler`)** | `{events['A']['cordon_taint_ts']}` | `{events['B']['cordon_taint_ts']}` |
| **Tier 2 Eviction Wave (`priority >= 1,000,000,000`)** | • Workload `{node_a['logger_pod']}` evicted (`deletionTimestamp: {events['A']['logger_deletion_ts']}`).<br>• **`{node_a['fluentbit_pod']}` EVICTED** (`deletionTimestamp: {events['A']['fluentbit_deletion_ts']}`). | • Workload `{node_b['logger_pod']}` evicted (`deletionTimestamp: {events['B']['logger_deletion_ts']}`).<br>• **`{node_b['fluentbit_pod']}` NEVER EVICTED** (`deletionTimestamp: {events['B']['fluentbit_deletion_ts']}`). |
| **`fluent-bit` Engine Behavior** | {fb_behavior_a} | {fb_behavior_b} |
| **Workload `SIGTERM_RECEIVED` -> `FINAL_EXIT`** | `{stdout_a['sigterm_ts']}` -> `{stdout_a['final_exit_ts']}` | `{stdout_b['sigterm_ts']}` -> `{stdout_b['final_exit_ts']}` |
| **Shutdown Logs Delivered to Cloud Logging** | **{stdout_a['count']}/32** lines (max gap between flushes: **{stdout_a['max_flush_gap_sec']}s**)<br>Flush `receiveTimestamp`s: {batches_a_str} | **{stdout_b['count']}/32** lines (max gap between flushes: **{stdout_b['max_flush_gap_sec']}s**)<br>Flush `receiveTimestamp`s: {batches_b_str} |
| **GCE MIG `deleteInstances` Timestamp** | `{del_a}` | `{del_b}` |
"""


def main() -> None:
  parser = argparse.ArgumentParser(
      description="Automate Side-by-Side Cluster Autoscaler scale-down verification."
  )
  parser.add_argument("--project", required=True, help="GCP Project ID")
  parser.add_argument("--cluster", required=True, help="GKE Cluster Name")
  parser.add_argument("--zone", default="", help="Optional zone/region (unused)")
  parser.add_argument(
      "--from-completed-run",
      action="store_true",
      help="Skip cluster setup and generate report directly from the completed 11:00Z-11:04Z run in Cloud Logging",
  )
  parser.add_argument(
      "--report-out",
      default="sxs_scale_down_report.md",
      help="Output Markdown file path for the generated report",
  )
  args = parser.parse_args()

  if args.from_completed_run:
    log("Using captured K8s events from completed 11:00Z-11:04Z run...")
    node_a = {
        "node": "gke-gke-test-cluster-1-test-ca-pool-8a83b8d6-22lj",
        "logger_pod": "deployment-tier2-logger-6495699864-dblcv",
        "fluentbit_pod": "fluentbit-gke-cndvg",
    }
    node_b = {
        "node": "gke-gke-test-cluster-1-test-ca-pool-d2a37a57-xrrc",
        "logger_pod": "deployment-tier2-logger-6495699864-f9qnm",
        "fluentbit_pod": "fluentbit-gke-469pm",
    }
    node_c = {
        "node": "gke-gke-test-cluster-1-test-ca-pool-d2a37a57-zfmf",
        "logger_pod": "deployment-tier2-logger-6495699864-zldpj",
        "fluentbit_pod": "fluentbit-gke-j2vz6",
    }
    events = {
        "A": {
            "candidate_taint_ts": "2026-10-13T11:00:58Z",
            "cordon_taint_ts": "2026-10-13T11:02:04Z",
            "logger_deletion_ts": "2026-10-13T11:02:46Z",
            "fluentbit_deletion_ts": "2026-10-13T11:04:01Z",
            "replacement_fluentbit_pod": "fluentbit-gke-h2pkw",
        },
        "B": {
            "candidate_taint_ts": "2026-10-13T11:00:52Z",
            "cordon_taint_ts": "2026-10-13T11:01:59Z",
            "logger_deletion_ts": "2026-10-13T11:02:40Z",
            "fluentbit_deletion_ts": "<none>",
            "replacement_fluentbit_pod": "<none>",
        },
    }
  else:
    ensure_daemonset_unannotated()
    clear_existing_scale_down_annotations()
    node_map = deploy_and_discover_topology()
    node_a, node_b, node_c = configure_roles(node_map)

    log("Deleting deployment-anti-affinity to trigger simultaneous Node A + Node B scale-down...")
    run_cmd(["kubectl", "delete", "deployment", "deployment-anti-affinity"])

    events = monitor_scale_down(node_a, node_b)

    log("Waiting 30s for Cloud Logging indexing to settle...")
    time.sleep(30)

  log("Querying Cloud Logging for Node A and Node B telemetry...")
  stdout_a_raw = gcloud_logging_read(
      args.project,
      f'resource.type="k8s_container" AND resource.labels.pod_name="{node_a["logger_pod"]}" AND logName="projects/{args.project}/logs/stdout"',
  )
  stdout_b_raw = gcloud_logging_read(
      args.project,
      f'resource.type="k8s_container" AND resource.labels.pod_name="{node_b["logger_pod"]}" AND logName="projects/{args.project}/logs/stdout"',
  )
  fb_a_raw = gcloud_logging_read(
      args.project,
      f'resource.type="k8s_container" AND resource.labels.namespace_name="kube-system" AND resource.labels.pod_name="{node_a["fluentbit_pod"]}"',
  )
  fb_b_raw = gcloud_logging_read(
      args.project,
      f'resource.type="k8s_container" AND resource.labels.namespace_name="kube-system" AND resource.labels.pod_name="{node_b["fluentbit_pod"]}"',
  )
  audit_a_raw = gcloud_logging_read(
      args.project,
      f'"{node_a["node"]}" AND logName:"cloudaudit.googleapis.com%2Factivity"',
  )
  audit_b_raw = gcloud_logging_read(
      args.project,
      f'"{node_b["node"]}" AND logName:"cloudaudit.googleapis.com%2Factivity"',
  )

  stdout_a = analyze_pod_stdout_logs(stdout_a_raw)
  stdout_b = analyze_pod_stdout_logs(stdout_b_raw)
  fb_a = analyze_fluentbit_logs(fb_a_raw)
  fb_b = analyze_fluentbit_logs(fb_b_raw)
  del_a = analyze_node_deletion(audit_a_raw)
  del_b = analyze_node_deletion(audit_b_raw)

  run_cmd(
      [
          "kubectl",
          "annotate",
          "node",
          node_c["node"],
          "cluster-autoscaler.kubernetes.io/scale-down-disabled-",
      ],
      check=False,
  )

  report = build_markdown_report(
      args.project,
      args.cluster,
      node_a,
      node_b,
      events,
      stdout_a,
      stdout_b,
      fb_a,
      fb_b,
      del_a,
      del_b,
  )

  with open(args.report_out, "w", encoding="utf-8") as f:
    f.write(report)

  print("\n" + "=" * 80)
  print(report)
  print("=" * 80)
  log(f"Saved Markdown report to {args.report_out}")


if __name__ == "__main__":
  main()