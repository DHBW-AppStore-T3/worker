# Handover — Worker

Lebendes Übergabedokument gemäß [HARNESS.md](https://github.com/DHBW-AppStore-T3/.github/blob/main/docs/HARNESS.md) Abschnitt 1.2.
Jede Session liest dieses Dokument zu Beginn und aktualisiert es vor dem Abschluss.

---

## 1. Status & Fokus
- **Stack:** Python 3.11, Poetry, Celery 5 über den Postgres-Transport `pgq` (Branch `spike/dev`, .github#5; auf `dev` noch RabbitMQ + Redis), Terraform 1.x, Packer 1.x, GitPython.
- **Rolle:** Asynchrone Ausführung von Infrastruktur-Deployments auf OpenStack (Klonen von App-Repos, Packer-Image-Builds, Terraform-Provisionierung, State-Verwaltung, Streaming von Logs).
- **Die 2 Flows (Harness Engineering):**
  - **Flow 1 (`/user-story`):** Asynchrone Task-Contracts und Pydantic-Payloads werden vorab im Issue spezifiziert.
  - **Flow 2 (`/harness-workflow`):** Autonome Umsetzung via `dev`-Trunk -> TDD (Pytest) -> PR auf `dev` -> CI grün -> Auto-Merge -> Automatisches Staging Deployment -> Hermes Discord Statusmeldung.
  - **Push auf `main`:** Bleibt rein menschlich! Abgesichert durch automatisiertes **Test Coverage Gate** (Coverage >= 60% erforderlich).
- **Isolation:** Auf `spike/dev` verbindet sich der Worker mit der App-Datenbank, aber nur als Rolle `appstore_worker` (Queue, Task-Events, Ergebnis-Spalten). Werkzeuge laufen mit Minimal-Umgebung als eigene UID je Slot (`app/job_context.py`, .github#7 A). State liegt weiter in `postgres-tfstate`; Credentials werden via Fernet in-process entschlüsselt.
- **CI/CD:** `ci.yml` unterstützt jetzt `main` und `dev`. Push auf `dev` baut Images und stößt Staging-Deploy in `DHBW-AppStore-T3/deployment` an.

---

## 2. In Arbeit & Nächste Schritte
- [x] Reengineering auf 2 Flows: `ci.yml` auf `dev`-Trunk und Test Coverage Gate für `main` umgestellt.
- [x] Staging-Trigger korrigiert auf `DHBW-AppStore-T3/deployment --ref dev`.
- [x] Sicherheits-Dependencies bereinigt (PR #2 gemergt).
- [x] Graphify-Graph und `claude_docs/` aufgesetzt (PR #3 gemergt).

---

## 3. Bekannte Fallstricke & Blocker
1. **Drei Formatter im Einsatz:** `worker` nutzt `ruff`, `black` und `isort`. Alle sind auf `line-length = 120` konfiguriert; bei Lint-Fehlern in CI prüfen, welches Tool die Datei zuletzt formatiert hat.
2. **Postgres-tfstate Verbindung:** `TFSTATE_DATABASE_URL` muss im Container gesetzt sein; schemas heißen `deployment_{uuid_ohne_bindestriche}`.
3. **Coverage Gate:** PRs auf `main` verlangen mindestens 60% Testabdeckung.
4. **Packer Concurrency Lock:** `PackerBuildLock` (auf `spike/dev` ein Postgres-Advisory-Lock, vorher Redis) verhindert redundante Builds identischer Images bei gleichzeitigen Tasks.
5. **Gemeinsamer Code mit dem Backend:** `app/pgq.py` und `app/task_contract.py` sind identisch mit dem Backend; `tests/test_shared_files.py` prüft den Hash in beiden Repos. Änderung = beide Dateien und beide Hashes.
6. **Integrationstests brauchen Postgres:** `DATABASE_URL` auf eine Test-DB; `tests/schema.sql` baut den Worker-Teil des Schemas samt Rolle `appstore_worker` (Superuser nötig). Die Prozess-Tests (`tests/test_worker_process.py`, inkl. kill -9) laufen vollständig nur unter Linux.

---

## 4. Letzte Übergaben (Historie)
- **2026-10-09 (.github#5 Postgres-Queue, Branch `spike/dev`):** Celery über `pgq` statt RabbitMQ/Redis; `JobTask` (`app/job_task.py`, `app/job_runtime.py`): Prolog mit Lease, Wiederzustellung nach totem Worker = FAILED `worker_lost` ohne zweiten Lauf, Events in `task_events`, Cancel per `NOTIFY task_cancel` (killpg), Ergebnis + Fernet-versiegelte Outputs/State im Epilog, Freigabe eines geparkten Destroy. Build-Lock als Advisory-Lock. Voraussetzung .github#7 A (Minimal-Umgebung, UID je Slot). Feste Concurrency, Heartbeat-Healthcheck, `poetry.lock` committet. Gefunden im Ende-zu-Ende-Lauf mit der echten Rolle: Der Epilog las Spalten, die die Rolle nur schreiben darf (behoben, Tests laufen jetzt als Rolle). ADR: `deployment/claude_docs/decisions/2026-postgres-queue.md`.
- **2026-09-18 (Harness 2-Flow Reengineering):** `ci.yml` für `dev`-Branch und Staging-Deploy konfiguriert; Test Coverage Gate für `main` integriert; `HANDOVER.md` aktualisiert.
- **2026-09-18:** Sicherheits-Dependencies (PR #2) und `claude_docs/` + Graphify (PR #3) in `main` gemergt.
- **2026-09-17:** Sicherheits-Dependencies in PR #2 aktualisiert (`cryptography`, `gitpython`, `.trivyignore`).
