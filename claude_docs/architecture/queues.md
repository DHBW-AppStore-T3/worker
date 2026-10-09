# Worker — Queues & Routing

## Broker (.github#5)

- **Broker:** die Anwendungs-Postgres über den Kombu-Transport `pgq` (`app/pgq.py`, identisch im Backend):
  `pgq+postgresql://appstore_worker:…@postgres:5432/<db>` (Default: abgeleitet aus `DATABASE_URL`). Kein RabbitMQ,
  kein Redis.
- **Kein Result-Backend** (`task_ignore_result=True`): Der Job schreibt Status, Transkript und die verschlüsselten
  Ergebnisse (`tasks.outputs_enc`) selbst in die Task-Zeile.
- **Serializer:** JSON.
- **Datenbankrolle `appstore_worker`:** nur `celery_queue`, Inserts in `task_events` und die Ergebnis-Spalten von
  `tasks` (Migration `5e1f0c2a9b7d` im Backend). Die Werkzeuge (Terraform, Packer, OpenStack-CLI) sehen weder diese
  Zugangsdaten noch den Fernet-Schlüssel (`app/job_context.py`, .github#7 A).

## Worker-Konfiguration & Task-Verhalten

### 1. Feste Concurrency, Prefetch 1
`--concurrency=N` (Image: 4) statt `--autoscale`: Slot *n* führt seine Werkzeuge als UID `20000+n` aus. Jeder
Prozess holt genau eine Nachricht (`worker_prefetch_multiplier=1`).

### 2. Späte Bestätigung, Lease, kein Doppellauf
- `task_acks_late=True`: Die Queue-Zeile wird erst nach dem Job gelöscht. Der Worker-Hauptprozess erneuert die Lease
  der gehaltenen Nachrichten (`WORKER_QUEUE_VISIBILITY_SECONDS`, Default 300).
- Der Job selbst hält eine Lease auf der Task-Zeile (`TASK_LEASE_SECONDS`, Default 90, alle 30 s erneuert).
- Stirbt der Worker, wird die Nachricht nach Ablauf wieder sichtbar. Der Prolog (`app/job_runtime.py`) sieht die Task
  dann RUNNING mit abgelaufener Lease und setzt sie auf FAILED (`worker_lost`), **ohne** den Job erneut zu starten.
  Stirbt nur ein Kindprozess, quittiert Celery die Nachricht; das Backend erkennt die abgelaufene Lease (Reconciler).

### 3. Events & Status
Celery-Events, Remote Control, Gossip und Mingle sind aus (der Transport hat kein Fanout). Fortschritt und Logzeilen
schreibt `JobTask.send_event` gepuffert in `task_events`; ein Trigger meldet sie per `NOTIFY task_events` an das
Backend. Der Healthcheck prüft das Alter der Heartbeat-Datei (`WORKER_HEARTBEAT_FILE`).

### 4. Cancel
Das Backend setzt `cancel_requested_at` und sendet `NOTIFY task_cancel`. Der Listener des Kindprozesses (Fallback:
der Lease-Heartbeat) beendet die Prozessgruppe des laufenden Werkzeugs (SIGTERM, nach 10 s SIGKILL); der nächste
Werkzeugaufruf wirft `JobCancelled`. Ein Destroy, das hinter dem abgebrochenen Job geparkt ist
(`celery_queue.after_task`), wird im Epilog freigegeben.

## Fehler- und Retry-Strategie

- **Deterministische Fehler (`Failure`, `app/failure.py`):** werden mit Transkript, State und Outputs in die Task-Zeile
  geschrieben (FAILED, `failure_kind=worker_failure`). Kein automatischer Retry.
- **Worker-Verlust:** FAILED (`worker_lost`), kein automatischer Neustart; Destroy oder erneuter Deploy aus der UI.
- **Build-Lock:** Postgres-Advisory-Lock je `(Projekt, Image)` auf einer eigenen Session; endet die Session (Absturz),
  ist der Lock sofort frei.
