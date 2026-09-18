# Worker — Queues & Routing

## Broker & Result-Backend

- **Broker:** RabbitMQ (`amqp://...`).
- **Result-Backend:** Redis (`redis://...`).
- **Serializer:** JSON (`task_serializer="json"`, `result_serializer="json"`).

## Worker-Konfiguration & Task-Verhalten

### 1. Prefetch Multiplier (`worker_prefetch_multiplier = 1`)
Da Terraform- und Packer-Läufe zwischen mehreren Minuten und einer halben Stunde dauern können, holt sich jeder Worker-Prozess immer **genau einen Task**. Dadurch wird verhindert, dass ein ausgelasteter Worker weitere Tasks im Voraus reserviert und andere Worker blockiert.

### 2. Späte Bestätigung (`task_acks_late = True`)
Der Task wird erst in RabbitMQ quittiert (ACK), nachdem er vollständig beendet wurde. Sollte der Worker-Container während eines Laufs abstürzen (z. B. OOM oder Host-Neustart), verbleibt der Task in RabbitMQ und kann von einem neu gestarteten Worker erneut evaluiert werden.

### 3. Events & Status-Tracking
- `task_track_started = True`: Status wechselt direkt bei Beginn auf `STARTED`.
- `worker_send_task_events = True` & `task_send_sent_event = True`: Der Celery-Event-Bus wird aktiv beliefert, damit der Listener im Backend Statusänderungen ohne Polling erfassen kann.

## Fehler- und Retry-Strategie

- **Deterministische Fehler (`Failure`-Exception):**
  Syntax-Fehler in Terraform-Dateien, ungültige OpenStack-Flavors oder fehlgeschlagene Builds werfen eine strukturierte `Failure`-Exception. Diese wird als JSON serialisiert und an das Backend zurückgegeben. Ein automatischer Retry findet hier **nicht** statt, da er denselben deterministischen Fehler erneut provozieren würde.
- **Infrastruktur-Fehler & Locks:**
  Bei temporären Netzwerkfehlern oder Redis-Lock-Timeouts beim Packer-Build wird der Fehler mit präzisen Diagnosedaten protokolliert, sodass der Dozent/Admin im UI den Fehler sieht und gezielt neu triggern kann.
