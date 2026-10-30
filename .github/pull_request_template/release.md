## Release `dev` → `main`

<!--
Release-PR: bringt den aktuellen Stand von `dev` in Produktion. Der Merge
allein rollt noch nichts aus: Nach dem Merge pusht CI das Image
`worker-pods` (Tag `sha-<Commit>`) nach ghcr.io und startet `k8s-bump.yml` im
Repo `deployment` mit Ref `main`. Das öffnet dort einen PR "chore(k8s): roll
out worker-pods sha-… on prod", der den Digest in `k8s/images/prod.yaml`
setzt. Erst wenn eine Person diesen PR freigibt und mergt (bei
`PROD_AUTO_MERGE=true` mergt er sich selbst), rollt Argo CD (`prod-root`)
aus; dabei wird der `pod-deployer` neu gestartet.

Anlegen:
  gh pr create --base main --head dev --title "release: <Datum>" \
    --body-file .github/pull_request_template/release.md
oder im Browser: .../compare/main...dev?expand=1&template=release.md

Mit Merge-Commit mergen, nicht squashen, damit `dev` und `main` nicht
auseinanderlaufen.
-->

**Enthaltene PRs:**

<!-- Alle PRs seit dem letzten Release, z. B. aus `git log --oneline origin/main..origin/dev`. BREAKING-PRs (Job-Name, Argumente, Status) kennzeichnen. -->

-

**Staging:** <!-- Link zum CI-Lauf mit dem Roll-out auf Staging und Ergebnis von `staging_verdict` (appstore-mcp, Skill `/verify-staging`) für diesen `dev`-Stand; Beispiel-Deployment, das auf Staging durchlief -->

**Release-Reihenfolge:** <!-- Nur falls backend/frontend/deployment mitziehen, z. B. "1. worker, 2. backend". Sonst "nur worker". -->

**Rollback auf:** <!-- Aktuell produktiver Image-Digest/Tag aus `deployment/k8s/images/prod.yaml` (Schlüssel `workerPods`) -->

## Release-Checkliste

<!--
Jeder Punkt wird abgehakt, bevor der PR gemergt werden kann. Trifft ein
Punkt nicht zu: mit "n/a" markieren UND abhaken, z. B.
- [x] n/a, Laufende Tasks: keine Änderung an Job-Payloads

Die Änderungs-Checklisten der enthaltenen PRs werden hier nicht wiederholt.
Dieser PR prüft, ob der gesammelte Stand in Produktion darf. Lint, Tests,
Coverage-Gate (≥ 80 % Zeilen, ≥ 70 % Branches), Security, Build und Image Scan
laufen als CI-Checks.
-->

- [ ] Staging: Genau dieser `dev`-Stand ist auf Staging ausgerollt, CI inkl. Roll-out grün, `staging_verdict` ohne Befund; mindestens ein Deployment (anlegen → läuft → löschen) lief dort durch
- [ ] Enthaltene PRs: Liste oben vollständig; jeder enthaltene PR hat eine vollständige Checkliste; PRs, die ohne Review auf `dev` gemergt wurden, sind in diesem PR reviewt
- [ ] Kompatibilität: Änderungen an Job-Namen, Argumenten oder Status-Rückmeldungen (`app/task_contract.py`) passen zum produktiven backend, auch zwischen den einzelnen Release-Merges; die Reihenfolge ist oben festgelegt
- [ ] Laufende Tasks: Der Neustart des `pod-deployers` bricht keine laufenden Deployments ab (Zeitpunkt ohne aktive Tasks gewählt; sonst werden sie nach Lease-Ablauf `worker_lost`), und noch wartende Tasks mit altem Payload werden vom neuen Worker korrekt verarbeitet
- [ ] Konfiguration: Neue Umgebungsvariablen und Rechte des Pod-Deployers sind im Chart `deployment/k8s/charts/appstore` (ClusterRole, Admission-Policy, Werte in `k8s/environments/prod.yaml`) gesetzt, neue Secret-Schlüssel mit `k8s/bootstrap.sh prod` im Cluster angelegt; beides ist auf `deployment/main` (`k8s/promote.sh`), bevor der Digest-PR für Produktion gemergt wird
- [ ] Rollback: Digest/Tag für den Rückweg ist oben notiert (Rückweg: Digest in `deployment/k8s/images/prod.yaml` per PR auf `main` zurücksetzen)
- [ ] Doku: `README.md`, `claude_docs/HANDOVER.md` und die Org-Doku in `DHBW-AppStore-T3/.github` unter `docs/` (Entwicklerguide, Sequenzdiagramme) beschreiben den Stand, der jetzt in Produktion geht
