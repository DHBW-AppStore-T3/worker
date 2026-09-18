# Worker — Häufige Fehler & Lösungen

## 1. `Failure`-Exception wird im Backend nicht geparst
- **Symptom:** Backend zeigt generischen Celery-Fehler statt strukturierter Log-/Fehlermeldung.
- **Ursache:** Die Repr-Formatierung von `Failure` wurde verändert. Das Backend nutzt eine Regex `Failure\('(.+)'\)`, um das serialisierte JSON aus dem Traceback zu extrahieren.
- **Lösung:** `Failure.__repr__` und `Failure.__reduce__` in `app/tasks.py` dürfen nicht verändert werden.

## 2. Terraform State Lock Fehler
- **Symptom:** `Error acquiring the state lock: ... conditional check failed`.
- **Ursache:** Ein vorheriger Task wurde hart abgebrochen oder zwei Tasks greifen gleichzeitig auf dasselbe Schema in `postgres-tfstate` zu.
- **Lösung:** Sperre in der tfstate-DB freigeben oder mit `terraform force-unlock <LOCK_ID>` aufheben.

## 3. Packer Redis-Lock Timeout
- **Symptom:** `PackerBuildLockError: Timed out waiting for build lock on image ...`.
- **Ursache:** Ein paralleler Task baut bereits dasselbe Packer-Image und überschreitet das Lock-Timeout (default: 30 Min).
- **Lösung:** Prüfen, ob der erste Build noch aktiv ist oder als Zombie hängengeblieben ist; ggf. Redis-Key `lock:packer:<image_name>` bereinigen.
