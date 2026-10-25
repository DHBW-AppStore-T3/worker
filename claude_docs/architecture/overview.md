# Worker — Architekturübersicht

Der Worker (Pod-Deployer) führt alle zeitintensiven Operationen asynchron und außerhalb des FastAPI-Web-Threads aus. Er legt für jedes Deployment einen Namespace `dep-<id>` im Cluster an und verwaltet dessen Objekte. Er führt nichts aus den App-Repositories aus und läuft ohne Root.

Der frühere VM-Pfad (Git-Clone, Packer, OpenTofu, OpenStack) ist entfernt (.github#12).

## Ablauf eines Tasks

```
Task-Nachricht (Postgres-Queue `celery_queue`, .github#5, Queue "pods")
        │
        ▼
1. Prolog (job_runtime): Task-Zeile claimen, Lease, Cancel-Listener
        │
        ▼
2. appstore.yaml laden und prüfen (k8s/spec.py), Plattformgrenzen anwenden
        │
        ▼
3. Objekte rendern (k8s/render.py) und anwenden (k8s/kube.py)
   Phasen + Live-Events über phase_tracker.py / task_events
        │
        ▼
4. Auf Bereitschaft warten (Snapshot der Pods), Ausgaben bauen (my-access)
        │
        ▼
5. Epilog: Ergebnis versiegelt in die Task-Zeile (task_contract.seal_results)
```

## Kernkomponenten in `app/`

- **`celery_app.py`**: Celery-Konfiguration (`task_acks_late=True`, `worker_prefetch_multiplier=1`, Queue `pods`).
- **`config.py`**: Pydantic BaseSettings (`DATABASE_URL` der Rolle `appstore_worker`, `RESULTS_ENCRYPTION_KEY`, `K8S_*`, `APP_*`-Plattformgrenzen, `WORKER_SIMULATE`).
- **`pod_tasks.py`**: die fünf Tasks `pods.deploy_application`, `pods.destroy_deployment`, `pods.pause_deployment`, `pods.resume_deployment`, `pods.redeploy_resource`.
- **`job_task.py` / `job_runtime.py`**: Basisklasse aller Tasks; Prolog/Epilog auf der Task-Zeile, Lease, Cancel, Event-Schreiber (.github#5).
- **`pgq.py` / `task_contract.py`**: Kombu-Transport und Event-/Ergebnis-Vertrag, identisch mit dem Backend.
- **`k8s/`**: `spec` (appstore.yaml-Vertrag), `render`, `kube` (Cluster-Zugriff), `fake` (Simulation ohne Cluster).
- **`utils/logger.py`**: Thread-sicherer `StructuredLogger` mit drei Sinks (Speicher-Buffer, Container-Konsole, Live-Events).
