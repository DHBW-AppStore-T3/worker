# Handover — Worker

Lebendes Übergabedokument gemäß [HARNESS.md](https://github.com/DHBW-AppStore-T3/.github/blob/main/docs/HARNESS.md) Abschnitt 1.2.
Jede Session liest dieses Dokument zu Beginn und aktualisiert es vor dem Abschluss.

---

## 1. Status & Fokus
- **Stack:** Python 3.11, Poetry, Celery 5 (RabbitMQ als Broker, Redis als Result-Backend), Terraform 1.x, Packer 1.x, GitPython.
- **Rolle:** Asynchrone Ausführung von Infrastruktur-Deployments auf OpenStack (Klonen von App-Repos, Packer-Image-Builds, Terraform-Provisionierung, State-Verwaltung, Streaming von Logs).
- **Isolation:** Der Worker verbindet sich bewusst **nicht** mit der primären AppStore-App-Datenbank. State liegt isoliert in `postgres-tfstate`; Credentials werden via Fernet in-process entschlüsselt.
- **CI:** Lint (Ruff + Black + isort + mypy), Tests (Unit & Integration via Pytest), Security Scans (Trivy), Docker Build & Image Scan.

---

## 2. In Arbeit & Nächste Schritte
- [ ] Sicherheits-Dependencies bereinigen (siehe offener PR #2: Pin von `cryptography`, `gitpython` und `msgpack`).
- [ ] Graphify-Graph für `worker` erzeugen und ins zentrale Cross-Repo-Mapping einpflegen.
- [ ] Task-Payload Validierung via typisierte Pydantic-Schemas weiter vereinheitlichen (analog zu FastAPI im Backend).

---

## 3. Bekannte Fallstricke & Blocker
1. **Drei Formatter im Einsatz:** `worker` nutzt `ruff`, `black` und `isort`. Alle sind auf `line-length = 120` konfiguriert; bei Lint-Fehlern in CI prüfen, welches Tool die Datei zuletzt formatiert hat.
2. **Postgres-tfstate Verbindung:** `TFSTATE_DATABASE_URL` muss im Container gesetzt sein; schemas heißen `deployment_{uuid_ohne_bindestriche}`.
3. **Prefetch & Acks:** `worker_prefetch_multiplier = 1` und `task_acks_late = True` verhindern, dass ein Worker blockierende Langläufer-Deployments hamstert oder bei einem Container-Crash Tasks verliert.
4. **Packer Concurrency Lock:** Redis-basierter `PackerBuildLock` verhindert redundante Builds identischer Images bei gleichzeitigen Tasks.

---

## 4. Letzte Übergaben (Historie)
- **2026-09-18:** `CLAUDE.md` und `claude_docs/` (`overview`, `queues`, `sse-streaming`, `debugging`, `decisions`, `HANDOVER.md`) gemäß HARNESS.md 1.1/1.2 vollständig aufgesetzt.
- **2026-09-17:** Sicherheits-Dependencies in PR #2 aktualisiert (`cryptography`, `gitpython`, `.trivyignore`).
