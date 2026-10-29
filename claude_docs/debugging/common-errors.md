# Häufige Fehler

| Symptom | Ursache | Lösung |
|---|---|---|
| Start: `WORKER_SIMULATE requires APP_ENV=development` | Simulation außerhalb der Entwicklung | `WORKER_SIMULATE` nur lokal setzen |
| Task bleibt PENDING | Worker läuft nicht oder konsumiert die falsche Queue (`-Q`) | `make dev-logs-worker`; alle Jobs (`pods.*`) liegen in der Queue `pods` |
| Worker kann nicht verbinden / `permission denied` | Rolle `appstore_worker` ohne Login | `make migrate-dev` (setzt das Login, startet den Worker neu) |
| `task_unknown` / Task sofort `failed` | API und Worker haben verschiedene Versionen (Task-Name unbekannt) | beide auf denselben Stand bringen |
| Task `failed` mit `worker_lost` | Worker starb oder Lease lief ab | kein Neustart durch das System; Destroy oder neu deployen |
| Outputs fehlen in der API | `RESULTS_ENCRYPTION_KEY` weicht von der API ab | angleichen |
| Pod-Job: `ImagePullBackOff`, keine Kapazität, Quota | Grund steht in der `Failure`-Meldung | App-Image, Cluster-Kapazität, Quota prüfen |
| Pod-Job: Wartezeit überschritten | `K8S_READY_TIMEOUT_SECONDS` (600 s) | Ursache im Transkript; Wert in der Worker-Umgebung anpassen |
| Upload abgelehnt | Siegel der API fehlt oder passt nicht (`UPLOAD_MAC_REQUIRED`) | Deployment neu anlegen |
