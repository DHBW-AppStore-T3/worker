# Worker — Überblick

Der Worker ist der Pod-Deployer und holt die Tasks aus der Tabelle `tasks` (`app/runner.py`). Jeder Job läuft auf derselben Job-Runtime (`app/job_runtime.py`): die Task-Zeile `tasks` ist die einzige Wahrheit über den Job.

## Job-Runtime

1. **Prolog** (`job_runtime.start`): PENDING → RUNNING mit `claimed_by` und Lease. Trifft die Nachricht auf einen RUNNING-Task mit abgelaufener Lease (Wiederzustellung nach totem Worker), wird er `worker_lost` gesetzt und **nicht** erneut ausgeführt. Abgebrochene und beendete Tasks werden übersprungen.
2. **Während des Jobs:** ein Heartbeat erneuert die Lease alle `TASK_LEASE_SECONDS/3` und erkennt Cancel oder verlorene Lease; ein Listener auf `NOTIFY task_cancel` reagiert sofort. Events und Logzeilen gehen gepuffert in `task_events`; `current_phase` und `progress_pct` der Task-Zeile werden mitgeführt.
3. **Epilog** (`Job.finish`): Status, Transkript (`logs`) und versiegelte Ergebnisse (`outputs_enc`) in einer Transaktion mit dem Terminal-Event; Lease frei, hinter dem Task geparkte Nachrichten sichtbar, `NOTIFY task_released`.

Ein Job ist eine Task-Zeile (`taskId`). Fehler: `Failure` (`app/failure.py`) trägt Transkript, State, Outputs und Commit in die fehlgeschlagene Zeile.

## Jobs (Queue `pods`, `app/pod_tasks.py`)

Der Worker klont nichts und führt nichts aus dem App-Repository aus. Die API liest `appstore.yaml` am festgelegten Commit, prüft sie und legt die Spec in den Job; der Worker prüft sie erneut (`app/k8s/spec.py`), rendert die Kubernetes-Objekte selbst (`app/k8s/render.py`) und wendet sie per Server-Side Apply an (`app/k8s/kube.py`). Kein eigener State: der Zustand eines Deployments ist sein Namespace `dep-<8 hex>`. Vor jedem Zugriff prüft der Worker, dass der Namespace zu diesem Deployment gehört.

| Job | Phasen |
|---|---|
| `pods.deploy_application` (`runtime: kubernetes`) | `STARTING`, `SPEC_LOAD`, `K8S_NAMESPACE`, `K8S_POLICIES` (Quota, LimitRange, NetworkPolicy), `K8S_APPLY`, `K8S_WAIT_READY`, `OUTPUTS_AND_CLEANUP` |
| `pods.deploy_vm_application` (`runtime: kubevirt-vm`) | gleiche Phasen; Namespace, PVC-Klon des Golden Images, VirtualMachine, noVNC-Proxy |
| `pods.destroy_deployment` | `STARTING`, `K8S_DELETE`, `K8S_WAIT_GONE`, `CLEANUP` |
| `pods.pause_deployment` / `resume_deployment` | `K8S_SCALE_DOWN`, `K8S_WAIT_STOPPED` / `K8S_SCALE_UP`, `K8S_WAIT_READY` |
| `pods.redeploy_resource` (Workload neu starten) | `K8S_RESTART`, `K8S_WAIT_READY` |

Bei einem Fehler im Deploy löscht der Worker den halb angelegten Namespace und wirft `Failure` mit lesbarem Grund (ImagePullBackOff, keine Kapazität, Quota). Ein Cancel überlässt das Aufräumen dem Destroy, den die API hinter den Job parkt. Zugangsdaten (Passwörter je Workload) erzeugt der Worker (`secrets.token_urlsafe`) und liefert sie als versiegelte Outputs.

Rechte: Der pod-deployer legt `dep-*`-Namespaces an und bindet darin zwei ClusterRoles (`K8S_DEPLOYER_CLUSTER_ROLE`, `K8S_READER_CLUSTER_ROLE`), begrenzt durch die Admission-Policy aus dem Repo `deployment`.

## Schlüssel

`RESULTS_ENCRYPTION_KEY` versiegelt die Ergebnisse (`tasks.outputs_enc`), gleich wie in der API. Ist er nicht gesetzt, versiegelt `CREDENTIAL_ENCRYPTION_KEY` (Rückfall für Altbestand). Weitere Schlüssel hat der Worker nicht.
