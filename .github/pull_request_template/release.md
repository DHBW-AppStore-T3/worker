## Release `dev` → `main`

<!--
Release-PR: bringt den aktuellen Stand von `dev` in Produktion. Nach dem
Merge pusht CI das Image als `latest` und startet `deployment/production.yml`.
Dabei wird der Worker neu gestartet. Jeder Merge auf `main` in backend,
worker oder frontend löst ein vollständiges Produktions-Deployment mit den
jeweils aktuellen `latest`-Images aus.

Anlegen:
  gh pr create --base main --head dev --title "release: <Datum>" \
    --body-file .github/pull_request_template/release.md
oder im Browser: .../compare/main...dev?expand=1&template=release.md

Mit Merge-Commit mergen, nicht squashen, damit `dev` und `main` nicht
auseinanderlaufen.
-->

**Enthaltene PRs:**

<!-- Alle PRs seit dem letzten Release, z. B. aus `git log --oneline origin/main..origin/dev`. BREAKING-PRs (Task-Name, Argumente, Status) kennzeichnen. -->

-

**Staging:** <!-- Link zum Staging-Deploy-Lauf und zum Hermes-Report für diesen `dev`-Stand; Beispiel-Deployment, das auf Staging durchlief -->

**Release-Reihenfolge:** <!-- Nur falls backend/frontend/deployment mitziehen, z. B. "1. worker, 2. backend". Sonst "nur worker". -->

**Rollback auf:** <!-- Aktuell produktives Image-Tag (`sha-…`) -->

## Release-Checkliste

<!--
Jeder Punkt wird abgehakt, bevor der PR gemergt werden kann. Trifft ein
Punkt nicht zu: mit "n/a" markieren UND abhaken, z. B.
- [x] n/a — Laufende Tasks: keine Änderung an Task-Payloads

Die Änderungs-Checklisten der enthaltenen PRs werden hier nicht wiederholt.
Dieser PR prüft, ob der gesammelte Stand in Produktion darf. Lint, Tests,
Coverage-Gate (≥ 60 % bei PRs nach `main`), Security, Build und Image Scan
laufen als CI-Checks.
-->

- [ ] Staging: Genau dieser `dev`-Stand ist auf Staging deployt; Staging-Deploy grün, Hermes-Healthcheck GUT; mindestens ein Deployment (anlegen → läuft → löschen) lief dort gegen OpenStack durch
- [ ] Enthaltene PRs: Liste oben vollständig; jeder enthaltene PR hat eine vollständige Checkliste; PRs, die ohne Review auf `dev` gemergt wurden, sind in diesem PR reviewt
- [ ] Kompatibilität: Änderungen an Task-Namen, Argumenten oder Status-Rückmeldungen passen zum produktiven backend, auch zwischen den einzelnen Release-Merges; die Reihenfolge ist oben festgelegt
- [ ] Laufende Tasks: Der Neustart bricht keine laufenden Deployments ab (Zeitpunkt ohne aktive Tasks gewählt), und noch wartende Tasks mit altem Payload werden vom neuen Worker korrekt verarbeitet
- [ ] Konfiguration: Neue Umgebungsvariablen (inkl. OpenStack-Zugangsdaten) sind in `deployment/docker-compose.prod.yml` durchgereicht (auf `deployment/main`) und ihre Werte im Secret `PRODUCTION_ENV_FILE` gesetzt, bevor gemergt wird
- [ ] Rollback: Image-Tag für den Rückweg ist oben notiert (`deployment/claude_docs/rollback/service.md`)
