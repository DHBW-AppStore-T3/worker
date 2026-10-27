# Worker — Queue, Lease, Cancel

## Queue

Die Task-Zeile ist der Queue-Eintrag (Tabelle `tasks` der API, kein Celery, kein Broker). Die API legt die Task als `PENDING` mit `job` (Name des Jobs, z. B. `pods.deploy_application`) und `job_kwargs` (seine Argumente) an und sendet in derselben Transaktion `NOTIFY task_queued`.

Der Runner (`app/runner.py`, Start: `python -m app.runner`) holt die nächste Task mit einer Anweisung (`job_runtime.claim`): die älteste `PENDING`-Zeile, `FOR UPDATE SKIP LOCKED`, wird `RUNNING` mit `claimed_by` und Lease. Zwei Worker bekommen nie dieselbe Task. Eine Task wartet, solange eine andere Task desselben Deployments eine Lease hält: der Destroy nach einem Cancel startet erst, wenn der abgebrochene Job losgelassen wurde (oder seine Lease abgelaufen ist; Security-Review A-5, Race 2).

`job_kwargs` liest der Worker in der Claim-Anweisung; der Trigger `tasks_drop_job_kwargs` löscht sie, sobald die Task `PENDING` verlässt (sie können hochgeladene Dateien enthalten).

## Ereignisgesteuert, mit Sicherheitsnetz

| Was | Auslöser | Rückfall |
|---|---|---|
| neue Task | `NOTIFY task_queued` (API, mit dem Commit) | Runner sucht alle `WORKER_POLL_INTERVAL_SECONDS` (5 s) |
| Task eines Deployments frei (nach Cancel) | `NOTIFY task_released` (Epilog des Jobs) | dieselbe Suche; nötig, wenn die Lease eines toten Workers abläuft |
| freier Slot | Ende eines Jobs im Prozess | – |
| Cancel | `NOTIFY task_cancel` | Lease-Heartbeat (alle 30 s) |
| Live-Events | Trigger `NOTIFY task_events` (API) | – |

Beim (Wieder-)Aufbau der LISTEN-Verbindung sucht der Runner sofort. `tests/test_worker_process.py` setzt die Suche auf 60 s: jeder Job dort startet durch eine Benachrichtigung.

## Datenbankrolle `appstore_worker`

Lesen: `tasks` (Claim-Spalten, `job`, `job_kwargs`, `created_at`); schreiben: Status, Lease, Fortschritt und Ergebnis-Spalten von `tasks`, INSERT in `task_events` (Migrationen im Backend). Kein Schreiben von `job`/`job_kwargs`, kein Anlegen von Tasks, kein Zugriff auf Nutzer, Apps, Deployments.

## Prozessmodell

- Ein Prozess, `WORKER_CONCURRENCY` (4) Jobs gleichzeitig, je ein Thread (die Jobs warten fast nur auf die Kubernetes-API). Mit `WORKER_SIMULATE` immer 1 (FakeKube ist nicht thread-sicher).
- Gesundheit: der Runner berührt `WORKER_HEARTBEAT_FILE` in jeder Runde; der Healthcheck schlägt bei Alter > 120 s fehl.
- Zeitlimit `WORKER_JOB_TIMEOUT_SECONDS` (7200 s): danach scheitert der Job bei der nächsten Prüfung (`JobTimeLimit`, `failure_kind=time_limit`). Läuft er 300 s später noch, gibt der Worker die Lease auf; die API setzt `worker_lost`. Jeder Aufruf der Kubernetes-API hat ein Timeout (`k8s/kube.py`, `REQUEST_TIMEOUT`).
- SIGTERM: keine neuen Tasks, die laufenden Jobs dürfen fertig werden (Grace-Period des Pods > längste Wartezeit); ein zweites Signal beendet sofort.
- Unbekannter Job-Name (API und Worker nicht im Takt): die Task wird sofort `failed` (`failure_kind=infrastructure`).

## Lease und Doppellauf

Der Job hält eine Lease auf der Task-Zeile (`TASK_LEASE_SECONDS`, 90 s, Erneuerung alle 30 s). Stirbt der Worker, bleibt die Task `RUNNING`; kein Worker holt sie wieder (nur `PENDING` wird geholt). Nach Ablauf der Lease setzt der Reconciler der API sie `worker_lost`. Ein Job läuft nie zweimal.

## Cancel

Die API setzt die Task `CANCELLED` mit `cancel_requested_at` und sendet `NOTIFY task_cancel`. Der Listener (Rückfall: Lease-Heartbeat) unterbricht den Job (`JobCancelled`); Pod-Jobs prüfen den Cancel in jeder Warteschleife. Der Epilog gibt die Lease frei und sendet `NOTIFY task_released`; der mit dem Cancel angelegte Destroy startet dann.

## Fehler

- Deterministischer Fehler (`Failure`): FAILED mit Transkript, State, Outputs, `failure_kind=worker_failure`. Kein Retry.
- Worker-Verlust: FAILED (`worker_lost`), kein Neustart; Destroy oder erneuter Deploy aus der UI.
