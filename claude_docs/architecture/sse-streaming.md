# Worker — SSE & Live-Log-Streaming

## Der Streaming-Pfad: Vom Subprozess in den Browser

Während eines Deployments fallen kontinuierlich Log-Ausgaben von `git`, `packer` und `terraform` an. Diese müssen live im Frontend dargestellt werden, ohne dass die Datenbank durch Tausende Einzel-Inserts überlastet wird.

```
Worker Subprozess (Terraform/Packer stdout/stderr)
                     │
                     ▼ Line-by-Line Reader Thread
StructuredLogger (app/utils/logger.py)
                     │
                     ├── 1. Memory Buffer (für finales Task-Result)
                     ├── 2. Console Sink (stdout für docker logs)
                     └── 3. Event Sink: bound_task.send_event("deployment_log", ...)
                                 │
                                 ▼ Celery Event Bus (RabbitMQ)
Backend Celery Event Listener (backend/app/services/celery_listener.py)
                                 │
                                 ▼ Redis Pub/Sub Kanal: deployment:{id}:logs
Backend SSE Endpoint (backend/app/api/v1/endpoints/deployments.py: /events)
                                 │
                                 ▼ HTTP Server-Sent Events (SSE)
Frontend DeploymentDetailView (EventSource)
```

## Thread-Sicherheit in `StructuredLogger`

Der Logger liest stdout/stderr von Subprozessen (`subprocess.Popen`) in Hintergrund-Threads, während der Haupt-Task-Thread Phasen-Marker (z. B. `LogCategory.PHASE`) setzt.
- Zugriff auf den gemeinsamen Buffer ist durch ein `threading.RLock` abgesichert.
- Log-Einträge sind strukturiert typisiert: Timestamp (UTC), Kategorie (`phase`, `operation`, `output`, `error`), Loglevel und Nachricht.
- ANSI-Escape-Sequenzen von Terraform/Packer werden via Regex bereinigt, um saubere Textausgaben im UI zu gewährleisten.
