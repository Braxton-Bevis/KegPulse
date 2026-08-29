# Off-machine backups

KegPulse keeps everything that matters — people, balances, pours, kegs,
calibrations — in one SQLite file in the data directory
(`%LOCALAPPDATA%\KegPulse\KegPulse\kegpulse.db` on Windows). A nightly job
copies a scrubbed snapshot of it to a **private** GitHub repository so the tab
survives a dead laptop.

## What runs

- `scripts/backup_to_github.py` takes a consistent snapshot with SQLite's
  backup API (the running app is never touched), removes the administrator PIN
  verifier, writes a JSON export of every table under `export/`, and commits +
  pushes to `Braxton-Bevis/kegpulse-backups` only when something changed.
- `scripts/backup-to-github.ps1` wraps it and appends to
  `%LOCALAPPDATA%\KegPulse\KegPulse\logs\github-backup.log`.
- `scripts/register-backup-task.ps1` registers the Windows scheduled task
  **KegPulse GitHub backup**: every day at 04:00, plus a catch-up run at logon
  (for nights the laptop was asleep). Run it once per machine; re-running is
  safe.

The working clone lives at `%LOCALAPPDATA%\KegPulse\KegPulse\github-backup`.
Pushes use the machine's existing GitHub credentials (`gh auth login` or Git
Credential Manager).

## What is *not* backed up

Pour photos, avatars, and videos stay on the machine — they are large, and the
database remains fully usable without them. Copy the `pour-photos` folder and
`Videos\KegPulse` separately if you want the media too.

## Restoring

1. Install KegPulse on the new machine and run it once so the data directory
   exists, then stop it.
2. Download `kegpulse.db` from the backup repository and copy it over the live
   `kegpulse.db` in the data directory.
3. Start KegPulse and set the administrator PIN again from Settings — the
   snapshot deliberately does not contain it.

Run a backup by hand at any time:

```powershell
.\scripts\backup-to-github.ps1
```
