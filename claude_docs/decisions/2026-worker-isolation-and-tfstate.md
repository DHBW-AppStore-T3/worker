# Entscheidung: Worker-Datenbankisolation und Postgres-tfstate

> **Abgelöst (Oktober 2026):** Der Worker hat seit .github#5 die eingeschränkte
> Rolle `appstore_worker` auf der App-Datenbank, und den OpenTofu-State hält die
> API (HTTP-State-Backend `/internal/tfstate`, je Task ein eigenes Token, siehe
> `app/services/terraform_executor.py`). `postgres-tfstate` entfällt; bestehende
> States übernimmt `python -m app.import_pg_tfstate` im Backend.

## Kontext
Traditionell greifen Backend- und Worker-Services oft auf dieselbe relationale Datenbank zu. Bei einem Infrastruktur-Worker, der beliebigen Code aus fremden App-Repositories klont und ausführt, birgt ein direkter Datenbankzugriff jedoch erhebliche Sicherheitsrisiken (SQL-Injection-Gefahr, versehentliche Datenmanipulation, Leaking von Nutzerdaten).

## Entscheidung
1. **Kein direkter Zugriff auf die App-Datenbank:**
   Der Worker besitzt keine Datenbank-Credentials für die Haupt-Postgres-Datenbank des Backends.
2. **Postgres-tfstate als dedizierter State-Store:**
   Für den Remote-State von Terraform wird ein isolierter PostgreSQL-Container (`postgres-tfstate`) betrieben. Jedes Deployment erhält ein eigenes Schema `deployment_{deployment_id}`.
3. **In-Process Fernet-Entschlüsselung:**
   OpenStack-Zugangsdaten werden vom Backend verschlüsselt in die Celery-Task-Payload gelegt und vom Worker im Arbeitsspeicher mit dem geteilten `CREDENTIAL_ENCRYPTION_KEY` entschlüsselt.
4. **Status-Rückmeldung ausschließlich über Celery:**
   Ergebnisse, Logs und Fehlerdaten fließen ausschließlich über das Celery Result-Backend (Redis) und Celery-Events an das Backend zurück.

## Konsequenzen
- Deutlich verringerte Angriffsfläche bei kompromittierten App-Repositories.
- Klare Trennung der Verantwortlichkeiten: Backend besitzt fachliche Datenhoheit, Worker ist reiner Infrastruktur-Executor.
