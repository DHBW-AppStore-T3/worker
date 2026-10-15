# Worker — Häufige Fehler & Lösungen

## 1. `Failure`-Exception wird im Backend nicht geparst
- **Symptom:** Backend zeigt generischen Celery-Fehler statt strukturierter Log-/Fehlermeldung.
- **Ursache:** Die Repr-Formatierung von `Failure` wurde verändert. Das Backend nutzt eine Regex `Failure\('(.+)'\)`, um das serialisierte JSON aus dem Traceback zu extrahieren.
- **Lösung:** `Failure.__repr__` und `Failure.__reduce__` in `app/tasks.py` dürfen nicht verändert werden.

## 2. OpenTofu State Lock Fehler
- **Symptom:** `Error acquiring the state lock` (HTTP 423 vom State-Backend der API).
- **Ursache:** Ein anderer, noch laufender Task desselben Deployments hält die Sperre.
- **Lösung:** Nichts tun: Die API gibt eine Sperre frei, sobald ihr Task nicht mehr `RUNNING` ist (Reaper/Lease), der nächste Task übernimmt sie. `401` statt `423` heißt: Job ohne gültiges State-Token (Task nicht `RUNNING` oder Token falsch).

## 3. Packer Redis-Lock Timeout
- **Symptom:** `PackerBuildLockError: Timed out waiting for build lock on image ...`.
- **Ursache:** Ein paralleler Task baut bereits dasselbe Packer-Image und überschreitet das Lock-Timeout (default: 30 Min).
- **Lösung:** Prüfen, ob der erste Build noch aktiv ist oder als Zombie hängengeblieben ist; ggf. Redis-Key `lock:packer:<image_name>` bereinigen.
