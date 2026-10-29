## Zusammenfassung

<!-- Was ändert dieser PR und warum? 1–3 Sätze. -->

Closes #

**Art der Änderung:** <!-- feat | fix | refactor | test | docs | ci | chore — bei Breaking Change (Job-Name, Argumente, Status) zusätzlich "BREAKING" -->

## Prüfung

<!-- Wie wurde das Verhalten geprüft? Welche Jobs (`pods.deploy_application`, `pods.deploy_vm_application`, `pods.destroy_deployment`, …), welche App-Vorlage, welche Umgebung (lokal mit `deployment` → `make init` im k3d-Cluster, Staging)? -->

## Checkliste

<!--
Checkliste für Änderungs-PRs (Feature-/Fix-Branch → `dev`, Hotfix → `main`).
Release-PRs (`dev` oder `promote/*` → `main`) verwenden stattdessen
`.github/pull_request_template/release.md`.

Jeder Punkt wird abgehakt, bevor der PR gemergt werden kann. Der CI-Check
"PR Checklist" blockiert den Merge, solange hier noch ein offenes "- [ ]" steht.

Trifft ein Punkt nicht zu: mit "n/a" markieren UND abhaken, z. B.
- [x] n/a — Task-Contract: keine Änderung an Jobs oder Payloads

Die reviewende Person prüft, dass die Häkchen stimmen, und bestätigt das mit
ihrem Approval (auf `dev` ist keines erzwungen: dann prüft die Autorin oder der
Autor die Häkchen selbst und die Fragen kommen im Release-PR zurück). Lint
(ruff, black, isort, mypy), Tests (unit/integration), Coverage-Gate (≥ 80 %
Zeilen, ≥ 70 % Branches auf jedem Branch), Security, Build und Image Scan laufen
als CI-Checks und werden hier nicht wiederholt.
-->

**Funktionale Eignung**

- [ ] Vollständigkeit: Alle Akzeptanzkriterien des verlinkten Issues sind umgesetzt; Abweichungen oder offene Punkte sind oben begründet
- [ ] Korrektheit: pytest-Tests (`unit`/`integration`) decken die Akzeptanzkriterien ab, inkl. Fehlerfällen (Kubernetes-API-Fehler, Pod nicht bereit/nicht schedulbar, Timeouts, Abbruch)
- [ ] Angemessenheit: Geänderte Pfade gegen einen echten Cluster ausgeführt (lokaler k3d-Cluster mit `make init` oder Staging), nicht nur mit Mocks oder `FakeKube`; die Wirkung von NetworkPolicies prüft nur Staging (lokal ist `network.policies` aus, `k8s/tests/netcheck/netcheck.py`)
- [ ] Fehlerpfad: Bei Fehler oder Abbruch wird der Task-/Deployment-Status korrekt gemeldet, und es bleiben keine verwaisten Namespaces, Volumes oder Leases zurück (`_cleanup_after_failure`)
- [ ] Abbruch & Neustart: Cancel, SIGTERM und Lease-Verlust (`worker_lost`) sind bedacht; ein Job läuft nie zweimal und legt bei erneutem Lauf nichts doppelt an

**Schnittstellen & Konfiguration**

- [ ] Task-Contract: Änderungen an `app/task_contract.py` oder `app/k8s/` (Job-Namen, Argumente, Events, Status) sind im Backend identisch nachgezogen (`tests/test_shared_files.py` prüft den Hash), sonst Backend-PR verlinkt
- [ ] App-Vorlagen: Änderungen an der Verarbeitung von `appstore.yaml` und den erzeugten Kubernetes-Objekten (Pods, Services, Ingress, NetworkPolicies, KubeVirt-VMs) sind mit der Admission-Policy (`deployment/k8s/charts/appstore/templates/admission-policy*.yaml`) und den bestehenden App-Vorlagen (`template-app` u. a.) verträglich; neue Rechte des Pod-Deployers stehen in dessen ClusterRole und in der Policy
- [ ] Konfiguration: Neue Umgebungsvariablen in `app/config.py` (mit sinnvollem Default) und im `deployment`-Repo nachgezogen (Chart `k8s/charts/appstore` und `k8s/environments/`; der lokale Cluster nutzt dasselbe Chart mit den Werten in `k8s/local/up.sh`); Grenzwerte (`APP_*`) haben in API und Worker dieselben Werte

**Sicherheit & Doku**

- [ ] Sicherheit: Keine Secrets im Diff; Passwörter, Zugangsdaten und Tokens der Apps werden weder geloggt noch im Task-Ergebnis im Klartext zurückgegeben; neue `pip-audit`-/`.trivyignore`-Ausnahmen begründet
- [ ] Doku: `claude_docs/HANDOVER.md` aktualisiert; `README.md` (Start, Tests, Konfiguration, Module) und `claude_docs/` (Architektur, Entscheidungen) nachgezogen, falls betroffen; bei geändertem Ablauf (Jobs, Status, App-Vorlagen) auch die Org-Doku in `DHBW-AppStore-T3/.github` unter `docs/` (Entwicklerguide, Sequenzdiagramme, Diagramme `c4/`)
