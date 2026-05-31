# WorkPulse Installer

Builds a single double-clickable `WorkPulseSetup-<version>.exe` that installs
WorkPulse on a fresh Windows machine — no Python, no terminal, no prerequisites.

## What ships in the installer

- Embedded Python 3.12 (no impact on any system Python install)
- All WorkPulse runtime dependencies (FastAPI, uvicorn, watchdog, psutil, pystray, etc.)
- The WorkPulse source tree (`scripts\`, `config\config.example.yaml`, `setup.ps1`, `README.md`)

Final installer size: ~40–60 MB compressed.

## Build it (developer machine)

**Prereqs (one-time):**
```powershell
winget install JRSoftware.InnoSetup
```

**Build:**
```powershell
cd D:\WorkPulse
powershell -ExecutionPolicy Bypass -File .\installer\build.ps1
```

Output: `installer\dist\WorkPulseSetup-1.0.0.exe`

The build script is re-runnable. Downloaded artefacts (embedded Python zip,
get-pip.py) are cached in `installer\cache\` so subsequent builds skip the
slow downloads.

## What the installer does on the target machine

1. Per-user install (no UAC) at `%LOCALAPPDATA%\Programs\WorkPulse`
2. Runs `setup.ps1 -Yes -SkipSecrets -SkipLaunch -BundledPython ...` which:
   - Copies `config\config.example.yaml` → `config\config.yaml`
   - Generates `config\identity.yaml` with a random `actor_id`, hostname,
     and `actor_label = $env:USERNAME` (user can edit later)
3. Creates Start Menu shortcuts: launch tray, open dashboard, uninstall
4. Optionally creates a Desktop shortcut (user-toggled in the wizard)
5. Creates a Startup-folder shortcut so the tray auto-launches at every logon
6. Offers to launch WorkPulse immediately after install

## What the installer does NOT touch

- Existing Python installs anywhere on the machine
- Any environment variables (the bundled Python is referenced by absolute
  path; nothing is added to PATH)
- The user's `~\Documents`, `~\Desktop`, etc. — only the install folder
- API keys / SMTP passwords — user configures those from the dashboard
  Settings page after install (no command line needed)

## Uninstall behaviour

- Prompts to confirm
- Removes all installed files **except** `config\config.yaml`,
  `config\identity.yaml`, `config\secrets.json`, `logs\`, and `vault\`
- Those personal-data folders are left in `%LOCALAPPDATA%\Programs\WorkPulse`
  for the user to keep or delete manually — destroying years of activity
  history during an uninstall would be hostile

## Files in this directory

| File | Purpose | Tracked in git? |
|---|---|---|
| `build.ps1` | Build orchestration script | yes |
| `WorkPulse.iss` | Inno Setup script | yes |
| `README.md` | This file | yes |
| `build\` | Staging directory (Python + deps + source) | no (gitignored) |
| `dist\` | Compiled installer output | no (gitignored) |
| `cache\` | Downloaded Python zip + get-pip.py | no (gitignored) |
