# worker

Teil der DHBW-AppStore-T3 Organisation — sechs eigenständige Repos, kein Monorepo.
Harness-Gesamtkonzept: https://github.com/DHBW-AppStore-T3/.github/blob/main/docs/HARNESS.md

- Übergabedokument (Ist-Zustand): `claude_docs/HANDOVER.md`
- README: Varianten, Start, Tests, Konfiguration, Module
- Architektur: `claude_docs/architecture/`, Entscheidungen: `claude_docs/decisions/`, Fehlersuche: `claude_docs/debugging/`

## Befehle
Im Container (`make shell-worker` in `deployment/`) oder mit lokalem Poetry:
- Einzeltest: `poetry run pytest tests/test_foo.py -v`
- Lanes wie CI: `poetry run pytest -m unit`, `poetry run pytest -m integration` (Postgres nötig)
- Lint wie CI: `poetry run ruff check . && poetry run black --check . && poetry run isort --check-only . && poetry run mypy app/ --ignore-missing-imports`
- Formatieren: `poetry run black . && poetry run isort . && poetry run ruff check --fix .`
