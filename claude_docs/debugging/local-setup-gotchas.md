# Worker — Lokale Setup-Gotchas

## 1. Worker startet nicht standalone
- **Gotcha:** Der Versuch, den Worker isoliert per `poetry run celery ...` auf dem Host zu starten, scheitert in der Regel an fehlenden Abhängigkeiten (`terraform` Binary, `packer` Binary, RabbitMQ, Redis, `postgres-tfstate`).
- **Best Practice:** Entwicklung und Testen über das `deployment`-Repo via `make dev-up` und `make shell-worker`.

## 2. Formatierungs-Konflikt zwischen Black, isort und Ruff
- **Gotcha:** `worker` ist das einzige Repo, das neben `ruff` auch noch `black` und `isort` konfiguriert hat. Alle drei sind auf `line-length = 120` gestellt.
- **Best Practice:** Vor dem Commit immer alle drei Formatter ausführen:
  ```bash
  poetry run black .
  poetry run isort .
  poetry run ruff check --fix .
  poetry run ruff format .
  ```

## 3. Temporäres Repository-Verzeichnis (`TEMP_REPO_BASE_PATH`)
- **Gotcha:** Standardpfad ist `/tmp/worker_repos`. Nach unsauberen Task-Abbrüchen können dort verwaiste Git-Klone verbleiben und Speicherplatz belegen. Der Worker bereinigt Klone im `finally`-Block, bei `SIGKILL` greift dieser jedoch nicht.
