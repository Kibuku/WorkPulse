# WorkPulse v2 — Windows validation runbook

Goal: confirm the **real install path** works on a Windows box before the pilot —
the same `setup.ps1` a pilot user runs, including the Task Scheduler agents that
have never been exercised on Windows. ~15 minutes. Paste the output of each step.

## 0. Prerequisites
- **Python 3.11+** from python.org — during install tick **"Add python.exe to PATH"**.
  Verify in a fresh PowerShell: `py --version`
- **Git** (`git --version`). Or copy the `workpulse-v2` folder over manually.

## 1. Get the code
```powershell
git clone <your-repo-url> workpulse-v2
cd workpulse-v2
```

## 2. Install — the one command a pilot user runs
```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```
This creates the venv, installs WorkPulse, seeds `config\config.yaml`, applies the
database schema, and registers the **Task Scheduler** agents. Expect it to end with
`WorkPulse is installed` and a "Done. Next steps" block. **This is the step most
likely to surface a Windows-only problem — capture its full output.**

## 3. Verify the install took
```powershell
.\.venv\Scripts\Activate.ps1
workpulse version      # → WorkPulse 2.0.0
workpulse status       # lists the agents + a health line
workpulse doctor       # health checks (some FAIL is normal on a brand-new DB)
```
In `workpulse status`, confirm the six agents (`activity`, `watcher`, `nightly`,
`dream-refresh`, `calendar-sync`, `doctor`) are **registered**. You can also check
in Windows: open **Task Scheduler** → look for `com.workpulse.*` tasks.

## 4. Smoke test — the ctypes sensor, DB, web, and schtasks renderer
```powershell
pip install -e ".[win,dev]"    # adds pytest for the next two checks
python tools\smoke.py          # → expect: 8 checks: 8 pass
pytest -q                      # → expect: 312 passed
```
The smoke line for the sensor should show your actual foreground window title.

## 5. Confirm the sensor records real activity
```powershell
python -m workpulse.signals.activity
```
Switch between a couple of apps for ~30s, then **Ctrl+C**. Check a log was written:
```powershell
Get-Content (Get-ChildItem logs\activity_*.jsonl | Select-Object -Last 1) -Tail 5
```
Expect JSON lines with `app`, `title`, `duration_s`. Lock the screen briefly during
the run to sanity-check that `LockApp.exe` / `LogonUI.exe` time is filtered out.

## 6. The dashboard
```powershell
workpulse web          # → http://localhost:5700
```
Open it in **Chrome**. Confirm: the onboarding **tour** pops up, the **taxonomy
wizard** / streams banner works (add a stream via a domain chip, give it a
"recognize by" hint), and DevTools (F12) → **Console** has no red errors. Ctrl+C to stop.

## 7. (Optional) The update path
```powershell
workpulse update       # git pull + re-apply; on an up-to-date clone it's a clean no-op
```

## Clean up (optional)
```powershell
workpulse uninstall    # removes the Task Scheduler agents
```

## What to paste back
1. **Step 2** full output (the install) — this is the key one.
2. `workpulse status` and `workpulse doctor` (step 3).
3. `python tools\smoke.py` result and `pytest -q` tail (step 4).
4. The `Get-Content` sample (step 5).
5. Did the dashboard render, tour appear, and console stay clean? (step 6)
6. Any traceback, in full.

## Known-incomplete on Windows (expected, not bugs)
- **Browser-tab tracking** is macOS-only for now — Windows records app/window
  activity but not the active URL. It degrades silently (no crash). This is the
  main Phase 5 gap to close after this validation.
