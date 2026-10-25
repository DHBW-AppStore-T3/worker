# Worker

Celery-Worker des App Stores (der "Pod-Deployer"). Konsumiert Deployment-Tasks aus der Postgres-Queue (`pgq`) und rendert bzw. verwaltet die Kubernetes-Objekte einer App (Namespace `dep-<id>`, StatefulSets/Deployments, Services, Ingress, NetworkPolicies).

Der frühere OpenStack-VM-Pfad (Packer-Image, OpenTofu, OpenStack-Credentials) ist entfernt (.github#12). Die Windows-App läuft künftig über KubeVirt (eigenes Thema).

## Setup

Dieses Repository wird nicht eigenständig gestartet. Der Worker braucht Postgres und vom Backend dispatchte Tasks; der Stack wird über das deployment-Repository hochgefahren (`make dev-up`, lokal ohne Cluster mit `WORKER_SIMULATE`, siehe `.github#6`). Anleitung: [deployment/README.md](https://github.com/DHBW-AppStore-T3/deployment#readme).

## Was der Worker tut

| Task | Zweck |
|---|---|
| `pods.deploy_application` | Namespace und Objekte aus der `appstore.yaml` rendern, anwenden, auf Bereitschaft warten |
| `pods.destroy_deployment` | Namespace samt Inhalt löschen |
| `pods.pause_deployment` | Workloads auf 0 skalieren (Daten bleiben) |
| `pods.resume_deployment` | Pausierte Workloads wieder starten |
| `pods.redeploy_resource` | Einzelne Ressource neu ausrollen |

## Code-Struktur

```
app/
├── celery_app.py     # Celery-Instanz (Queue "pods", pgq-Transport, Heartbeat-Datei)
├── config.py         # Pydantic-Settings aus Env-Variablen
├── pod_tasks.py      # Die Celery-Tasks (pods.*)
├── phase_tracker.py  # Fortschrittsphasen für den Stepper im Frontend
├── job_task.py       # Basisklasse aller Tasks
├── job_runtime.py    # Prolog/Epilog auf der Task-Zeile, Lease, Cancel, Event-Schreiber
├── task_contract.py  # Event-/Ergebnis-Vertrag (identisch mit dem Backend)
├── pgq.py            # Kombu-Transport auf Postgres (identisch mit dem Backend)
├── k8s/              # spec (appstore.yaml), render, kube (Cluster-Zugriff), fake (Simulation)
└── utils/            # crypto (Ergebnisse versiegeln), logger (strukturiertes Logging)
```

## Entwicklung

```bash
poetry run pytest -m unit -v
poetry run ruff check . && poetry run black --check . && poetry run isort --check-only .
```
