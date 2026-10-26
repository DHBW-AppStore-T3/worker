# Worker — Queue, Lease, Cancel

## Broker

Die Anwendungs-Postgres über den Kombu-Transport `pgq` (`app/pgq.py`, identisch im Backend): `pgq+postgresql://appstore_worker:…@<host>/<db>` (Standard: abgeleitet aus `DATABASE_URL`). Kein RabbitMQ, kein Redis, kein Result-Backend (`task_ignore_result=True`), Serializer JSON.

Routing (`celery_app.py`, gleich in der API): `pods.*` → Queue `pods`. Der Worker konsumiert nur diese Queue (`-Q pods`), auch im Dev-Image.

## Datenbankrolle `appstore_worker`

Rechte nur auf `celery_queue`, INSERT in `task_events`, die Ergebnis-Spalten von `tasks` (Migrationen im Backend). Kein Zugriff auf Nutzer, Apps, Zugangsdaten.

## Prozessmodell

- `--concurrency=4` (Images), feste Zahl statt Autoscale. Dev: `--pool=solo`.
- `worker_prefetch_multiplier=1`, `task_acks_late=True`: die Queue-Zeile wird erst nach dem Job gelöscht. Der Hauptprozess erneuert die Sichtbarkeit gehaltener Nachrichten (`WORKER_QUEUE_VISIBILITY_SECONDS`, 300 s).
- Celery-Events, Remote Control, Gossip, Mingle, Heartbeat sind aus (kein Fanout im Transport). Gesundheit: `HeartbeatFile` berührt `WORKER_HEARTBEAT_FILE` alle 30 s; der Docker-Healthcheck schlägt bei Alter > 120 s fehl.
- Zeitlimit: Soft `WORKER_JOB_TIMEOUT_SECONDS` (7200 s), Hard +300 s; danach läuft die Lease ab und die API setzt `worker_lost`.
- Unbekannter Task-Name (API und Worker nicht im Takt): die Task-Zeile wird sofort `failed` (`job_runtime.fail_unknown`).

## Lease und Doppellauf

Der Job hält eine Lease auf der Task-Zeile (`TASK_LEASE_SECONDS`, 90 s, Erneuerung alle 30 s). Stirbt der Worker, wird die Nachricht wieder sichtbar; der Prolog setzt die Task `worker_lost` ohne erneuten Lauf. Stirbt nur ein Kindprozess, quittiert Celery die Nachricht; die API erkennt die abgelaufene Lease (Reconciler).

## Cancel

Die API setzt `cancel_requested_at` und sendet `NOTIFY task_cancel`. Der Listener (Rückfall: Lease-Heartbeat) unterbricht den Job (`JobCancelled`); Pod-Jobs prüfen den Cancel in jeder Warteschleife. Ein hinter dem Job geparkter Destroy (`celery_queue.after_task`) wird im Epilog freigegeben.

## Fehler

- Deterministischer Fehler (`Failure`): FAILED mit Transkript, State, Outputs, `failure_kind=worker_failure`. Kein Retry.
- Worker-Verlust: FAILED (`worker_lost`), kein Neustart; Destroy oder erneuter Deploy aus der UI.
