# Worker — SSE & Live-Log-Streaming

## Der Streaming-Pfad: Vom Subprozess in den Browser (.github#5)

Während eines Deployments fallen kontinuierlich Log-Ausgaben von `git`, `packer` und `terraform` an. Sie landen als
Zeilen in `task_events`; jede API-Instanz kann sie streamen.

```
Worker Subprozess (Terraform/Packer stdout/stderr)
                     │
                     ▼ Line-by-Line Reader Thread
StructuredLogger (app/utils/logger.py)
                     │
                     ├── 1. Memory Buffer (für das Transkript in tasks.logs)
                     ├── 2. Console Sink (stdout für docker logs)
                     └── 3. Event Sink: JobTask.send_event(...) (app/job_task.py)
                                 │
                                 ▼ Job.emit: Puffer, Batch-INSERT alle 200 ms (app/job_runtime.py)
task_events (Postgres) ── Trigger: NOTIFY task_events '<task>:<event>:<typ>'
                                 │
                                 ▼ eine LISTEN-Verbindung je API-Prozess (backend app/services/task_events.py)
Backend SSE Endpoint GET /deployments/{id}/stream (id: = Event-ID, Last-Event-ID)
                                 │
                                 ▼ HTTP Server-Sent Events
Frontend DeploymentDetailView (useDeploymentStream)
```

- Event-Typen und Payloads (`app/task_contract.py`, identisch im Backend) sind dieselben wie zu Celery-Zeiten
  (`task-progress`, `task-log`, `task-succeeded`, `task-failed`, `task-revoked`); das Frontend blieb unverändert.
- Höchstens 10 000 Logzeilen je Task gehen live raus, danach eine Marke; das vollständige Transkript steht nach dem
  Ende in `tasks.logs`.
- Das Terminal-Event schreibt der Worker im Epilog in derselben Transaktion wie Status und Ergebnis, nach allen
  anderen Events.

## Thread-Sicherheit in `StructuredLogger`

Der Logger liest stdout/stderr von Subprozessen (`subprocess.Popen`) in Hintergrund-Threads, während der Haupt-Task-Thread Phasen-Marker (z. B. `LogCategory.PHASE`) setzt.
- Zugriff auf den gemeinsamen Buffer ist durch ein `threading.RLock` abgesichert.
- Log-Einträge sind strukturiert typisiert: Timestamp (UTC), Kategorie (`phase`, `operation`, `output`, `error`), Loglevel und Nachricht.
- ANSI-Escape-Sequenzen von Terraform/Packer werden via Regex bereinigt, um saubere Textausgaben im UI zu gewährleisten.
