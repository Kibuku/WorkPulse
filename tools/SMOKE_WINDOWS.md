# WorkPulse v2 — Windows smoke test

Goal: confirm v2 actually runs on a real Windows box **this week**, so any
Windows-only breakage surfaces early instead of at rollout. Takes ~10 minutes.
Paste the output of each step back.

## 0. Prerequisites
- **Python 3.11+** from python.org — during install tick **“Add python.exe to PATH”**.
  Verify in a new PowerShell: `py --version`
- **Git** (or just copy the `workpulse-v2` folder onto the Windows machine).

## 1. Get the code
```powershell
git clone <your-repo-url> workpulse-v2
cd workpulse-v2
```
(Or copy the folder over and `cd` into it.)

## 2. Create a venv and install
```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[win,dev]"
```
If PowerShell blocks the activate script, run once:
`Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` then retry activate.

## 3. Run the test suite  → expect `299 passed`
```powershell
pytest -q
```

## 4. Run the smoke test  → expect `8 checks: 8 pass`
```powershell
python tools\smoke.py
```
This checks imports, the **Win32 activity sensor**, DB + migrations, the web
app + static assets, and the **Task Scheduler** autostart renderer. The sensor
line should show your actual foreground window title.

## 5. Let the sensor record for ~1 minute
```powershell
python -m workpulse.signals.activity
```
Switch between a couple of apps, then press **Ctrl+C**. Confirm a log was written:
```powershell
dir logs\activity_*.jsonl
Get-Content (Get-ChildItem logs\activity_*.jsonl | Select-Object -Last 1) -Tail 5
```
You should see JSON lines with `app`, `title`, `duration_s`. Lock the screen for
a few seconds during the run to sanity-check that `LockApp.exe` time is filtered.

## 6. Start the dashboard
```powershell
python -m workpulse.web.app
```
Open **http://localhost:5700** in a browser. It should render the dashboard.
Open DevTools (F12) → **Console** and check for red errors. Ctrl+C to stop.

## What to paste back
1. Output of steps **3** (pytest tail) and **4** (full smoke report).
2. The `Get-Content` sample from step **5**.
3. Whether the dashboard rendered in step **6**, and any **red Console errors**.
4. Anything that errored, with the full traceback.

Known-incomplete on Windows (expected, not bugs — later phases):
- **Browser tab tracking** is macOS-only right now (Windows records app/window
  activity but not the active URL). It degrades silently — no crash.
- No packaged installer yet (Phase 4); this manual flow stands in for now.
