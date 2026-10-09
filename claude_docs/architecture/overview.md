# Worker — Architekturübersicht

Der Worker führt alle zeitintensiven Infrastruktur-Operationen asynchron und außerhalb des FastAPI-Web-Threads aus.

## Orchestrierungs-Pipeline

Ein Task (z. B. `tasks.deploy_application`) durchläuft eine definierte Pipeline von Services:

```
Task-Nachricht (Postgres-Queue `celery_queue`, .github#5)
        │
        ▼
1. Fernet Decryption (utils/crypto.py)
   Entschlüsselt OpenStack-Credentials & Git-Tokens in-process
        │
        ▼
2. Git Clone (services/git_service.py)
   Klont das Ziel-App-Repository am spezifizierten Release-Tag nach /tmp/worker_repos/
        │
        ▼
3. Packer Discovery & Build (services/packer_discovery.py, packer_executor.py)
   Prüft auf vorhandene Packer-Templates (.pkr.hcl / .json)
   Sichert den Build über einen Postgres-Advisory-Lock (services/build_lock.py) ab
   Erstellt bei Bedarf ein neues VM-Glance-Image auf OpenStack
        │
        ▼
4. OpenStack Auth (services/openstack_auth.py)
   Schreibt eine task-isolierte clouds.yaml für Terraform / OpenStack SDK
        │
        ▼
5. Terraform Provisionierung (services/terraform_executor.py)
   Initialisiert Terraform mit Postgres-Remote-State (Schema deployment_<id>)
   Führt init / plan / apply bzw. destroy aus
   Streamt alle stdout/stderr-Zeilen live in task_events (JobTask.send_event)
        │
        ▼
6. Abschluss & Ergebnis
   Liefert Status, extrahierte Outputs (z. B. IPv4/IPv6, SSH-Keys) oder strukturiertes Failure-Objekt zurück
```

## Kernkomponenten in `app/`

- **`celery_app.py`**: Initialisierung und Konfiguration des Celery-Workers (`task_acks_late=True`, `worker_prefetch_multiplier=1`).
- **`config.py`**: Pydantic BaseSettings für Umgebungsvariablen (`DATABASE_URL` der Rolle `appstore_worker`, `TFSTATE_DATABASE_URL`, `CREDENTIAL_ENCRYPTION_KEY`, Lease-/Slot-Einstellungen).
- **`job_task.py` / `job_runtime.py`**: Basisklasse aller Tasks; Prolog/Epilog auf der Task-Zeile, Lease, Cancel, Event-Schreiber (.github#5).
- **`job_context.py`**: Minimal-Umgebung und UID je Slot für Terraform/Packer/OpenStack-CLI (.github#7 A).
- **`pgq.py` / `task_contract.py`**: Kombu-Transport und Event-/Ergebnis-Vertrag, identisch mit dem Backend.
- **`tasks.py`**: Die fünf Celery-Tasks:
  1. `tasks.deploy_application`
  2. `tasks.destroy_deployment`
  3. `tasks.pause_deployment`
  4. `tasks.resume_deployment`
  5. `tasks.redeploy_resource`
- **`services/`**: Kapselung der Einzelschritte (Git, Packer, Terraform, OpenStack, Locks).
- **`utils/`**:
  - `logger.py`: Thread-sicherer `StructuredLogger` mit drei Sinks (Speicher-Buffer, Container-Konsole, Live-Events).
  - `crypto.py`: Entschlüsselung von Credentials-Envelopes.
