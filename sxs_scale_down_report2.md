## Empirical Side-by-Side Verification of `cluster-autoscaler.kubernetes.io/enable-ds-eviction: "false"`

We ran an automated side-by-side Cluster Autoscaler scale-down test on cluster `gke-test-cluster-1` (project `citi-hpc-research-1`) comparing two nodes drained simultaneously from the same node pool:
- **Node A (`gke-gke-test-cluster-1-test-ca-pool-34c175af-f9vc`) — Default (without annotation on `fluentbit-gke-q9xdg`)**:
  - Tier 2 Workload: `deployment-tier2-logger-6495699864-ksnzp` (`priority: 1,000,000,000`, 30s graceful shutdown)
- **Node B (`gke-gke-test-cluster-1-test-ca-pool-8a83b8d6-pld8`) — Annotated `fluentbit-gke-7zxmk` with `cluster-autoscaler.kubernetes.io/enable-ds-eviction: "false"`**:
  - Tier 2 Workload: `deployment-tier2-logger-6495699864-n29f2` (`priority: 1,000,000,000`, 30s graceful shutdown)

### Side-by-Side Scale-Down Timeline

| Phase / Metric | Node A (`gke-gke-test-cluster-1-test-ca-pool-34c175af-f9vc`) — Default (No Annotation) | Node B (`gke-gke-test-cluster-1-test-ca-pool-8a83b8d6-pld8`) — With `enable-ds-eviction: "false"` |
| :--- | :--- | :--- |
| **`fluentbit-gke` Pod** | `fluentbit-gke-q9xdg` | `fluentbit-gke-7zxmk` |
| **Tier 2 Workload Pod** | `deployment-tier2-logger-6495699864-ksnzp` | `deployment-tier2-logger-6495699864-n29f2` |
| **Marked Unneeded (`DeletionCandidateOfClusterAutoscaler`)** | `2026-10-13T11:46:52Z` | `2026-10-13T11:47:00Z` |
| **Scale-Down Cordon (`ToBeDeletedByClusterAutoscaler`)** | `2026-10-13T11:47:53Z` | `2026-10-13T11:48:00Z` |
| **Tier 2 Eviction Wave (`priority >= 1,000,000,000`)** | • Workload `deployment-tier2-logger-6495699864-ksnzp` evicted (`deletionTimestamp: 2026-10-13T11:48:37Z`).<br>• **`fluentbit-gke-q9xdg` EVICTED** (`deletionTimestamp: 2026-10-13T11:49:52Z`). | • Workload `deployment-tier2-logger-6495699864-n29f2` evicted (`deletionTimestamp: 2026-10-13T11:48:43Z`).<br>• **`fluentbit-gke-7zxmk` NEVER EVICTED** (`deletionTimestamp: <none>`). |
| **`fluent-bit` Engine Behavior** | Evicted (`deletionTimestamp: 2026-10-13T11:49:52Z`); replacement pod `fluentbit-gke-zzzgs` spawned. | **Zero interruption** — never received `SIGTERM`, never paused `in_tail` inputs, and never restarted. |
| **Workload `SIGTERM_RECEIVED` -> `FINAL_EXIT`** | `2026-10-13T11:47:52.777054609Z` -> `<none>` | `2026-10-13T11:47:58.479895050Z` -> `2026-10-13T11:48:28.616158483Z` |
| **Shutdown Logs Delivered to Cloud Logging** | **1/32** lines (max gap between flushes: **0.0s**)<br>Flush `receiveTimestamp`s: `2026-10-13T11:47:52.911213617Z` | **32/32** lines (max gap between flushes: **5.0s**)<br>Flush `receiveTimestamp`s: `2026-10-13T11:48:01.434602679Z`, `2026-10-13T11:48:06.412762754Z`, `2026-10-13T11:48:11.354140415Z`, `2026-10-13T11:48:16.271246197Z`, `2026-10-13T11:48:21.269014680Z`, `2026-10-13T11:48:26.263267102Z`, `2026-10-13T11:48:31.267988482Z` |
| **GCE MIG `deleteInstances` Timestamp** | `2026-10-13T11:49:58.245130Z` | `2026-10-13T11:48:33.666907Z` |
