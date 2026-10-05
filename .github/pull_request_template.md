## Zusammenfassung

<!-- Was ändert dieser PR und warum? 1–3 Sätze. -->

Closes #

**Art der Änderung:** <!-- feat | fix | refactor | test | docs | ci | chore — bei Breaking Change (Task-Name, Argumente, Status) zusätzlich "BREAKING" -->

## Prüfung

<!-- Wie wurde das Verhalten geprüft? Welche Tasks, welche App-Vorlage, welche Umgebung (lokal mit `deployment` → `make dev-up`, Staging, echtes OpenStack)? -->

## Checkliste

<!--
Jeder Punkt wird abgehakt, bevor der PR gemergt werden kann. Der CI-Check
"PR Checklist" blockiert den Merge, solange hier noch ein offenes "- [ ]" steht.

Trifft ein Punkt nicht zu: mit "n/a" markieren UND abhaken, z. B.
- [x] n/a — Task-Contract: keine Änderung an Tasks oder Payloads

Die reviewende Person prüft, dass die Häkchen stimmen, und bestätigt das mit
ihrem Approval. Lint (ruff, black, isort, mypy), Tests (unit/integration) inkl.
Coverage-Gate, Security, Build und Image Scan werden separat als
Pflicht-CI-Checks erzwungen und hier nicht wiederholt.
-->

**Funktionale Eignung**

- [ ] Vollständigkeit: Alle Akzeptanzkriterien des verlinkten Issues sind umgesetzt; Abweichungen oder offene Punkte sind oben begründet
- [ ] Korrektheit: pytest-Tests (`unit`/`integration`) decken die Akzeptanzkriterien ab, inkl. Fehlerfällen (Terraform-/Packer-Fehler, OpenStack-API-Fehler, Timeouts)
- [ ] Angemessenheit: Geänderte Terraform-/Packer-/OpenStack-Pfade mindestens einmal real gegen OpenStack ausgeführt (lokal oder Staging), nicht nur mit Mocks
- [ ] Fehlerpfad: Bei Fehler oder Abbruch wird der Task-/Deployment-Status korrekt gemeldet, und es bleiben keine verwaisten OpenStack-Ressourcen oder Locks zurück
- [ ] Wiederholbarkeit: Tasks verhalten sich bei Retry, Pause/Resume oder Worker-Neustart korrekt (idempotent, kein doppeltes Anlegen)

**Schnittstellen & Konfiguration**

- [ ] Task-Contract: Task-Namen, Argumente und Status-Rückmeldungen bleiben mit dem Backend kompatibel, sonst Backend-PR verlinkt
- [ ] App-Vorlagen: Änderungen an der Verarbeitung von Packer/Terraform sind mit den bestehenden App-Vorlagen (`template-app` u. a.) verträglich
- [ ] Konfiguration: Neue Umgebungsvariablen in `app/config.py` (mit sinnvollem Default) und im `deployment`-Repo (`.env.example`, Compose) nachgezogen

**Sicherheit & Doku**

- [ ] Sicherheit: Keine Secrets im Diff; OpenStack-Credentials, SSH-Keys und Tokens werden weder geloggt noch im Task-Ergebnis zurückgegeben; neue `pip-audit`-/`.trivyignore`-Ausnahmen begründet
- [ ] Doku: `claude_docs/HANDOVER.md` aktualisiert; Architektur/Entscheidungen in `claude_docs/` nachgezogen, falls betroffen
