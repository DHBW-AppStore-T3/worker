# Lokales Setup — Stolpersteine

- Der Worker startet nur im Stack aus `deployment/` (`make init`, später `make dev-up`). Anleitung ab Null: `deployment/docs/dev-setup.md`. Auf dem Host fehlt das DB-Schema mit der Rolle `appstore_worker`.
- Kein Hot-Reload: nach Code-Änderung `make dev-restart-worker`.
- `worker-sim` ist ein Prozess (`--pool=solo`); der `FakeKube`-Zustand (`WORKER_SIMULATE=true`) gilt für diesen Prozess und ist nach einem Neustart weg.
- Der Worker verbindet sich als `appstore_worker`; das Login entsteht erst durch `make migrate-dev` (Teil von `make init`).
- Drei Formatierer: `ruff`, `black`, `isort`, alle `line-length = 120`. Vor dem Commit: `poetry run black . && poetry run isort . && poetry run ruff check --fix .`. CI prüft zusätzlich `mypy app/`.
- `pyproject.toml` fordert Python `>=3.10,<3.12`; die Images nutzen 3.11.
- Pytest-Standard: 60 s Timeout je Test (`--timeout-method=thread`); längere Tests mit `@pytest.mark.timeout(N)`.
- Die Umgebung des Dev-Containers verfälscht Tests (ohne `CREDENTIAL_ENCRYPTION_KEY` bricht schon die Sammlung ab); Rezept mit der Umgebung von CI im `README.md`, Abschnitt Tests.
