# Handover — Worker

Lebendes Übergabedokument (Harness-Konzept: [HARNESS.md](https://github.com/DHBW-AppStore-T3/.github/blob/main/docs/HARNESS.md)). Beschreibt den Ist-Zustand von `dev`; Stand wird mit jeder Änderung angepasst.

## 1. Zustand

- **Stack:** Python 3.11, Poetry, psycopg 3 (Task-Queue in Postgres, eigener Runner), kubernetes-Client.
- **Image:** ein Docker-Target `pod-deployer` (Queue `pods`, UID 10001); `ghcr.io/dhbw-appstore-t3/worker-pods`, in Kubernetes die Komponente `pod-deployer`. Der OpenStack-VM-Pfad (Image `runtime`, Queue `vm`) ist entfernt (.github#12).
- **Isolation:** Datenbankrolle `appstore_worker`; Fernet-Schlüssel nur für Ergebnisse (siehe `claude_docs/decisions/`).
- **Branches:** `dev` → Staging, `main` → Produktion. Feature-Branches gehen per PR auf `dev`.
- **Dokumentation:** `README.md` (Varianten, Start, Tests, Konfiguration, Module), `claude_docs/architecture/` (Überblick, Queues, Live-Events).

## 2. CI (`.github/workflows/ci.yml`, Branches `main` und `dev`)

Lint (ruff, black, isort, mypy) · Unit (`pytest -m unit`) · Integration (`pytest -m integration`, Postgres-Service `worker_test`, echter Runner) · Coverage (kombiniert; getrennte Gates ≥ 80 % Zeilen und ≥ 70 % Branches, auf allen Branches und PRs; ein rotes Gate stoppt Build, Push und Rollout) · Security (Trivy, pip-audit) · Build des Targets `pod-deployer` · Image-Scan · Push nach `ghcr.io` (Push auf `main`/`dev`) · Roll out on k8s: startet `k8s-bump.yml` im Repo `deployment`.

## 3. Regeln im Code

1. `app/task_contract.py` und `app/k8s/` sind byte-gleich mit dem Backend (`tests/test_shared_files.py` prüft den Hash). Änderung immer in beiden Repos, beide Hashes.
2. `app/upload_mac.py` bleibt im Takt mit `backend/app/utils/upload_mac.py`.
3. Die Grenzwerte (`APP_IMAGE_REGISTRY_ALLOWLIST`, `APP_MAX_*`, `APP_VM_IMAGE_NAMESPACES`) haben in API und Worker dieselben Werte; der Worker prüft erneut.
4. Der Name `pods.*` und die Queue `pods` gelten auch für KubeVirt-Jobs (`pods.deploy_vm_application`) und bleiben bestehen.
5. Tests ohne DB-Fixture sind automatisch `unit`, mit `pg`/`pg_url` `integration`; ein ausdrücklich als `unit` markierter Test mit DB-Fixture bricht die Sammlung ab.
6. Integrationstests brauchen eine Test-Datenbank in `DATABASE_URL`; `tests/schema.sql` baut den Worker-Teil des Schemas samt Rolle `appstore_worker` (Superuser nötig). Die Prozess-Tests (`tests/test_worker_process.py`, inkl. `kill -9`) laufen vollständig nur unter Linux.
7. Drei Formatierer (ruff, black, isort), alle `line-length = 120`.

## 4. Offen

- Die Windows-App läuft über KubeVirt (`pods.deploy_vm_application`); der Betrieb ist ein eigenes Thema (siehe `deployment/k8s/README.md`).
