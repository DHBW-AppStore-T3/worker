# Worker

Führt die Deployment-Aufträge des AppStore aus. Holt Jobs aus der Postgres-Queue `pgq`, schreibt Fortschritt, Logs und versiegelte Ergebnisse in die Datenbank der API.

Image und Docker-Target `pod-deployer`, Queue `pods`, läuft als UID 10001 ohne Root. Der Worker rendert die Kubernetes-Objekte einer App mit `appstore.yaml` selbst (`runtime: kubernetes` und `kubevirt-vm`) und führt nichts aus dem App-Repository aus.

Aufgaben (Celery-Namen): `pods.deploy_application`, `pods.deploy_vm_application` (KubeVirt), `pods.destroy_deployment`, `pods.pause_deployment`, `pods.resume_deployment`, `pods.redeploy_resource`.

Den früheren OpenStack-VM-Pfad (Queue `vm`, Packer, OpenTofu) gibt es nicht mehr (.github#12).

Zustand: Branch `dev` (Staging); `main` ist Produktion.

## Lokal starten

Der Worker läuft nur im Stack aus `deployment/`. Anleitung ab Null (Terminal, Git, Docker, make): [deployment/docs/dev-setup.md](https://github.com/DHBW-AppStore-T3/deployment/blob/dev/docs/dev-setup.md).

```bash
mkdir appstore && cd appstore
for r in deployment backend worker frontend; do git clone -b dev https://github.com/DHBW-AppStore-T3/$r.git; done
cd deployment
make init
```

Lokal gibt es einen Worker-Container `worker-sim` (`Dockerfile.dev`): ein Prozess (`--pool=solo`), Queue `pods`, `WORKER_SIMULATE=true` (Pod-Jobs gegen In-Memory-`FakeKube`, kein Cluster nötig; der Zustand ist nach einem Worker-Neustart weg).

| Befehl (in `deployment/`) | Wirkung |
|---|---|
| `make dev-logs-worker` | Worker-Logs |
| `make dev-restart-worker` | Neustart (nötig nach Code-Änderung, kein Hot-Reload) |
| `make shell-worker` | bash im Container (`/app`, Code per Volume) |

## Tests und Lint

Lint wie CI (im Container nach `make shell-worker` oder mit lokalem Poetry):

```bash
poetry run ruff check . && poetry run black --check . && poetry run isort --check-only . && poetry run mypy app/ --ignore-missing-imports
```

Tests ohne DB-Fixture gelten automatisch als `unit`, mit `pg`/`pg_url`-Fixture als `integration` (`tests/conftest.py`). Integrationstests bauen den Worker-Teil des Schemas aus `tests/schema.sql` in der Datenbank aus `DATABASE_URL` und starten einen echten `celery worker`.

Die Umgebung des Dev-Containers (`WORKER_SIMULATE`, `RESULTS_ENCRYPTION_KEY`, Rolle `appstore_worker` in `DATABASE_URL` …) ist nicht die von CI. Rezept mit der Umgebung von CI (in `deployment/`; Test-Datenbank einmalig anlegen):

```bash
docker compose -f docker-compose.dev.yml exec -T postgres psql -U postgres -c "CREATE DATABASE worker_test"
for lane in unit integration; do
  docker compose -f docker-compose.dev.yml exec -T worker env -i HOME=/root \
    PATH=/root/.local/bin:/usr/local/bin:/usr/bin:/bin \
    DATABASE_URL=postgresql://postgres:postgres@postgres:5432/worker_test \
    CREDENTIAL_ENCRYPTION_KEY=Q1PNlFd4It9oQPtjCcXcmB7wGDkY4w8KwpIRNSF4u7U= \
    poetry run pytest -m $lane -q --no-cov
done
```

Ergebnis mit dem Stand `dev`: 180 Unit-Tests und 17 Integrationstests bestanden.

## Konfiguration (Umgebungsvariablen)

`app/config.py`. `DATABASE_URL` ist Pflicht (Rolle `appstore_worker`). `APP_ENV` ist standardmäßig `production`.

| Variable | Bedeutung | Standard |
|---|---|---|
| `DATABASE_URL` | Anwendungs-Postgres als Rolle `appstore_worker` | – |
| `CELERY_BROKER_URL` | leer: `pgq+` + `DATABASE_URL` | leer |
| `RESULTS_ENCRYPTION_KEY` | Fernet-Schlüssel der Task-Ergebnisse, gleich wie in der API; bevorzugt vor dem Credential-Schlüssel | leer (dann Credential-Schlüssel) |
| `CREDENTIAL_ENCRYPTION_KEY` | Rückfall-Schlüssel für Ergebnisse, die vor `RESULTS_ENCRYPTION_KEY` versiegelt wurden | leer |
| `UPLOAD_MAC_REQUIRED` | Uploads müssen das Siegel der API tragen | `true` |
| `APP_IMAGE_REGISTRY_ALLOWLIST`, `APP_MAX_CPU/MEMORY/STORAGE`, `APP_VM_IMAGE_NAMESPACES` | Grenzen für `appstore.yaml`, gleiche Werte wie in der API | wie API |
| `K8S_ZONE`, `K8S_APP_DOMAIN`, `K8S_INGRESS_CLASS` (`traefik`), `K8S_TLS_SECRET`, `K8S_CLUSTER_ISSUER`, `K8S_STORAGE_CLASS` | App-Hosts `<workload>-<deployment>.<domain>`, TLS, Volumes | leer |
| `K8S_STUDENT_NODE_SELECTOR`, `K8S_STUDENT_TOLERATIONS`, `K8S_EXTRA_EGRESS_EXCEPT` | Platzierung und gesperrte Netze der Student-Pods | leer |
| `K8S_ENVIRONMENT` | Label der angelegten Namespaces (die Admission-Policy erlaubt nur diese) | leer |
| `K8S_READY_TIMEOUT_SECONDS`, `K8S_DELETE_TIMEOUT_SECONDS`, `K8S_UNSCHEDULABLE_GRACE_SECONDS` | Wartezeiten | 600, 300, 60 |
| `K8S_MAX_WORKLOADS_PER_DEPLOYMENT` | Obergrenze | 60 |
| `TASK_LEASE_SECONDS` | Lease der Task-Zeile (Erneuerung alle ⅓) | 90 |
| `WORKER_QUEUE_VISIBILITY_SECONDS` | Lease der Queue-Nachricht | 300 |
| `WORKER_JOB_TIMEOUT_SECONDS` | Soft-Limit je Job (Hard-Limit +300 s) | 7200 |
| `WORKER_HEARTBEAT_FILE` | alle 30 s berührt; Healthcheck prüft das Alter (max. 120 s) | `/tmp/worker-heartbeat` |
| `WORKER_SIMULATE` | Pod-Jobs gegen `FakeKube` (nur `APP_ENV=development`) | `false` |

## Code-Struktur

```
app/
├── celery_app.py     Celery-App, Transport pgq, Routing, Heartbeat-Datei
├── config.py         Settings
├── db.py             DB-Zugriff (psycopg 3, SQL), Rolle appstore_worker
├── job_task.py       Basisklasse aller Jobs (Events, Cancel)
├── job_runtime.py    Prolog/Epilog auf der Task-Zeile, Lease, Event-Schreiber
├── failure.py        Failure: strukturierter Fehler mit Transkript, State, Outputs
├── pod_tasks.py      Die Jobs (Queue pods, Pods und KubeVirt)
├── phase_tracker.py  Fortschrittsphasen
├── k8s/              spec (appstore.yaml), render, kube (API-Client), fake (FakeKube)
├── utils/            crypto (Fernet), logger (StructuredLogger)
├── pgq.py            Kombu-Transport (identisch mit backend)
├── task_contract.py  Event-/Ergebnis-Vertrag (identisch mit backend)
├── upload_mac.py     Siegel der API auf Uploads prüfen (im Takt mit backend/app/utils/upload_mac.py)
```

`pgq.py` und `task_contract.py` sind byte-gleich mit dem Backend; `tests/test_shared_files.py` prüft den Hash.

## Ablauf eines Jobs

Siehe `claude_docs/architecture/overview.md` (Pipeline), `queues.md` (Queue, Lease, Cancel), `sse-streaming.md` (Live-Events).
