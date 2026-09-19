# Handover — Worker

Lebendes Übergabedokument gemäß [HARNESS.md](https://github.com/DHBW-AppStore-T3/.github/blob/main/docs/HARNESS.md) Abschnitt 1.2.
Jede Session liest dieses Dokument zu Beginn und aktualisiert es vor dem Abschluss.

---

## 1. Status & Fokus
- **Stack:** Python 3.11, Poetry, Celery 5 (RabbitMQ als Broker, Redis als Result-Backend), Terraform 1.x, Packer 1.x, GitPython.
- **Rolle:** Asynchrone Ausführung von Infrastruktur-Deployments auf OpenStack (Klonen von App-Repos, Packer-Image-Builds, Terraform-Provisionierung, State-Verwaltung, Streaming von Logs).
- **Die 2 Flows (Harness Engineering):**
  - **Flow 1 (`/user-story`):** Asynchrone Task-Contracts und Pydantic-Payloads werden vorab im Issue spezifiziert.
  - **Flow 2 (`/harness-workflow`):** Autonome Umsetzung via `dev`-Trunk -> TDD (Pytest) -> PR auf `dev` -> CI grün -> Auto-Merge -> Automatisches Staging Deployment -> Hermes Discord Statusmeldung.
  - **Push auf `main`:** Bleibt rein menschlich! Abgesichert durch automatisiertes **Test Coverage Gate** (Coverage >= 60% erforderlich).
- **Isolation:** Der Worker verbindet sich bewusst **nicht** mit der primären AppStore-App-Datenbank. State liegt isoliert in `postgres-tfstate`; Credentials werden via Fernet in-process entschlüsselt.
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
4. **Packer Concurrency Lock:** Redis-basierter `PackerBuildLock` verhindert redundante Builds identischer Images bei gleichzeitigen Tasks.

---

## 4. Letzte Übergaben (Historie)
- **2026-09-18 (Harness 2-Flow Reengineering):** `ci.yml` für `dev`-Branch und Staging-Deploy konfiguriert; Test Coverage Gate für `main` integriert; `HANDOVER.md` aktualisiert.
- **2026-09-18:** Sicherheits-Dependencies (PR #2) und `claude_docs/` + Graphify (PR #3) in `main` gemergt.
- **2026-09-17:** Sicherheits-Dependencies in PR #2 aktualisiert (`cryptography`, `gitpython`, `.trivyignore`).
