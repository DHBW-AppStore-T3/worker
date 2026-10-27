# Worker — Live-Events

```
Pod-Job-Meldungen (Phasen, Wartezustand, Fehlergründe)
        │
        ▼
StructuredLogger (app/utils/logger.py)
        ├─ Speicher-Buffer      → tasks.logs (Transkript, am Ende)
        ├─ Konsole              → docker logs
        └─ Event-Sink           → job_runtime.emit (app/job_runtime.py)
                                      │ Puffer, Batch-INSERT alle 200 ms (app/job_runtime.py)
                                      ▼
                           task_events (Postgres)
                                      │ Trigger task_events_notify → NOTIFY task_events
                                      ▼
                  eine LISTEN-Verbindung je API-Prozess (backend app/services/task_events.py)
                                      │
                                      ▼
                  GET /deployments/{id}/stream  (SSE, id: = Event-ID, Last-Event-ID)
                                      │
                                      ▼
                  Frontend (useDeploymentStream)
```

- Event-Typen (`app/task_contract.py`, identisch im Backend): `task-progress`, `task-log`, `task-succeeded`, `task-failed`, `task-revoked`.
- Höchstens 10 000 Logzeilen je Task gehen live raus, danach eine Marke; ein Event ist höchstens 32 KiB (längere Meldungen werden gekürzt). Das volle Transkript steht nach dem Ende in `tasks.logs`.
- Das Terminal-Event schreibt der Worker im Epilog in derselben Transaktion wie Status und Ergebnis, nach allen anderen Events. Bei Cancel oder `worker_lost` schreibt die API es selbst.
- `StructuredLogger` ist threadsicher (`RLock`); Einträge tragen UTC-Zeit, Kategorie (`phase`, `operation`, `output`, `error`, `status`), Level und Meldung; ANSI-Sequenzen werden entfernt.
