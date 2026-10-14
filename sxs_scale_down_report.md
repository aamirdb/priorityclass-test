## Empirical Side-by-Side Verification of `cluster-autoscaler.kubernetes.io/enable-ds-eviction: "false"`

We ran an automated side-by-side Cluster Autoscaler scale-down test on cluster `gke-test-cluster-1` (project `citi-hpc-research-1`) comparing two nodes drained simultaneously from the same node pool:
- **Node A (`gke-gke-test-cluster-1-test-ca-pool-34c175af-w7jm`) — Default (without annotation on `fluentbit-gke-5qssr`)**:
  - Tier 2 Workload: `deployment-tier2-logger-6495699864-8mxgl` (`priority: 1,000,000,000`, 30s graceful shutdown)
- **Node B (`gke-gke-test-cluster-1-test-ca-pool-8a83b8d6-s9jr`) — Annotated `fluentbit-gke-bnwmk` with `cluster-autoscaler.kubernetes.io/enable-ds-eviction: "false"`**:
  - Tier 2 Workload: `deployment-tier2-logger-6495699864-lpbqv` (`priority: 1,000,000,000`, 30s graceful shutdown)

### Side-by-Side Scale-Down Timeline

| Phase / Metric | Node A (`gke-gke-test-cluster-1-test-ca-pool-34c175af-w7jm`) — Default (No Annotation) | Node B (`gke-gke-test-cluster-1-test-ca-pool-8a83b8d6-s9jr`) — With `enable-ds-eviction: "false"` |
| :--- | :--- | :--- |
| **`fluentbit-gke` Pod** | `fluentbit-gke-5qssr` | `fluentbit-gke-bnwmk` |
| **Tier 2 Workload Pod** | `deployment-tier2-logger-6495699864-8mxgl` | `deployment-tier2-logger-6495699864-lpbqv` |
| **Marked Unneeded (`DeletionCandidateOfClusterAutoscaler`)** | `2026-10-13T11:11:57Z` | `2026-10-13T11:11:57Z` |
| **Scale-Down Cordon (`ToBeDeletedByClusterAutoscaler`)** | `2026-10-13T11:12:58Z` | `2026-10-13T11:13:06Z` |
| **Tier 2 Eviction Wave (`priority >= 1,000,000,000`)** | • Workload `deployment-tier2-logger-6495699864-8mxgl` evicted (`deletionTimestamp: 2026-10-13T11:13:40Z`).<br>• **`fluentbit-gke-5qssr` EVICTED** (`deletionTimestamp: 2026-10-13T11:14:55Z`). | • Workload `deployment-tier2-logger-6495699864-lpbqv` evicted (`deletionTimestamp: 2026-10-13T11:13:46Z`).<br>• **`fluentbit-gke-bnwmk` NEVER EVICTED** (`deletionTimestamp: <none>`). |
| **`fluent-bit` Engine Behavior** | Evicted (`deletionTimestamp: 2026-10-13T11:14:55Z`); replacement pod `fluentbit-gke-7s9s2` spawned. | **Zero interruption** — never received `SIGTERM`, never paused `in_tail` inputs, and never restarted. |
| **Workload `SIGTERM_RECEIVED` -> `FINAL_EXIT`** | `2026-10-13T11:12:55.842153958Z` -> `<none>` | `2026-10-13T11:13:01.628180537Z` -> `2026-10-13T11:13:31.719237720Z` |
| **Shutdown Logs Delivered to Cloud Logging** | **1/32** lines (max gap between flushes: **0.0s**)<br>Flush `receiveTimestamp`s: `2026-10-13T11:12:56.465809486Z` | **32/32** lines (max gap between flushes: **5.07s**)<br>Flush `receiveTimestamp`s: `2026-10-13T11:13:05.574639188Z`, `2026-10-13T11:13:10.624337411Z`, `2026-10-13T11:13:15.459166416Z`, `2026-10-13T11:13:20.460721042Z`, `2026-10-13T11:13:25.458242510Z`, `2026-10-13T11:13:30.532247338Z`, `2026-10-13T11:13:35.460697338Z` |
| **GCE MIG `deleteInstances` Timestamp** | `2026-10-13T11:15:01.807689Z` | `2026-10-13T11:13:36.769693Z` |
