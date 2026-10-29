# Worker-Isolation: eigene Datenbankrolle, kein Code aus App-Repositories

## Entscheidung

1. **Eingeschränkte Datenbankrolle:** Der Worker verbindet sich als `appstore_worker` mit der Anwendungs-Datenbank. Rechte: Tasks holen, INSERT in `task_events`, Ergebnis-Spalten von `tasks`. Nichts sonst.
2. **Kein fremder Code:** Der Worker klont kein App-Repository und führt nichts daraus aus; er rendert die Kubernetes-Objekte aus der geprüften `appstore.yaml` selbst. Er läuft ohne Root.
3. **Ein Schlüssel für Ergebnisse:** `RESULTS_ENCRYPTION_KEY` (gleich wie in der API).
4. **Status ausschließlich über die Datenbank:** Status, Logs, Fehler und Ergebnisse stehen in der Task-Zeile und in `task_events`.

## Grund

Jeder zusätzliche Zugriff auf Daten der API vergrößert den Schaden einer Kompromittierung des Workers, der Zugriff auf die Kubernetes-API hat.

## Historie

Bis .github#12 führte ein zweiter Worker (`vm-executor`) OpenTofu und Packer aus App-Repositories aus. Dafür gab es den State in der API, Slot-UIDs und einen zweiten Schlüssel. Der Pfad ist entfernt; die Datei behält ihren Namen, weil andere Dokumente auf sie verweisen.
