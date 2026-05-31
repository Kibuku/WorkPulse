# WorkPulse

A local-first personal productivity tracker. Sits in your system tray,
watches what you actually work on, and shows you where the day went —
without sending your data anywhere.

> WorkPulse is **v1: Personal Tracker** — the foundation layer.
> The full architecture (Personal WorkPulse + Institution Brain) is described
> in `WorkPulse_Vision_v1.1.docx`. Institutional layers (v3 Fingerprint Engine,
> v4 Institution Brain, v6 Attribution) are future builds.

---

## What it does, today

- **Tracks window focus** — every app, every window, every minute, locally
- **Tags activity to your projects** — by folder, by window title, and (when an
  API key is configured) by AI-classifying anything new it sees
- **Watches file changes** in your project folders
- **Shows you a daily dashboard** at `http://127.0.0.1:5700/` with: where the
  time went, last 14 days, what you were working on, what needs review
- **Learns from you** — one click on an unmapped window in the dashboard, and
  WorkPulse remembers that pattern forever

Everything stays on your machine. Nothing is uploaded.

---

## Set up on a new Windows machine

Three steps. Roughly 5 minutes.

1. **Install Python 3.12+** from <https://www.python.org/downloads/>. During
   the installer, tick *"Add python.exe to PATH"*.

2. **Get the WorkPulse files** onto your machine. Copy or clone the repo
   somewhere — `C:\WorkPulse`, `D:\WorkPulse`, anywhere you like.

3. **Run the setup script** from that folder:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\setup.ps1
   ```

   It will:
   - create a virtual environment under `.venv\`
   - install dependencies from `requirements.txt`
   - create your personal `config\config.yaml` from the template
   - ask for your name and (optional) organization
   - **optionally** prompt for an Anthropic API key and Gmail App Password —
     both fully skippable; you can add them later from the Settings page
   - register a Startup-folder shortcut so WorkPulse launches at every logon
   - launch the tray and open the dashboard

When you're done, look for the WorkPulse icon in your system tray. Click it to
open the dashboard.

---

## Configure your projects

WorkPulse needs to know which folders, files, and window titles belong to
which project. Open `config\config.yaml` and add entries under `streams:` and
`watcher.stream_path_patterns:`. The example template at
`config\config.example.yaml` shows the shape; see below for a starter pattern.

```yaml
streams:
  client-x:    "Client X — Consulting Work"
  side-thing:  "Personal Side Project"
  studies:     "Master's Studies"

watcher:
  stream_path_patterns:
    - path: "ClientX/MajorProject"
      stream: "client-x"
    - path: "ClientX"
      stream: "client-x"
    - path: "Strathmore"
      stream: "studies"
```

**You don't have to write all these by hand.** As you use WorkPulse, the
"Needs your attention" panel shows untagged windows. Click `Tag as ▾` and
choose a project — WorkPulse writes the rule to `config\learned_tags.json`
and applies it forever after. Over a couple of weeks, manual config becomes
almost unnecessary.

---

## AI features (optional)

Two features call the Anthropic API when configured:

- **Smart auto-tagging** — when an untagged window has been focused for 30+
  seconds, WorkPulse asks Claude to classify it from the title + recent
  context, then saves the answer as a permanent local rule. ~$0.001 per
  unique new window. Caches result, so each pattern is only classified once.
- **Word document classification** — Word docs that don't match any other
  rule get their content read (locally, via stdlib zipfile) and classified.

If you don't add a key, both features stay dormant. The tracker still runs
fine — you just tag windows manually via the dashboard dropdown.

### Three backends, one façade

WorkPulse picks an AI backend automatically based on what's available:

| Order | Backend | Cost | When it fires |
|---|---|---|---|
| 1 | **Anthropic Claude** (cloud) | ~$0.001 per classification | When an API key is configured |
| 2 | **Ollama** (local LLM) | Free, runs on your machine | When no Anthropic key, but Ollama is running with the configured model pulled |
| 3 | **None** | Free | Nothing configured — manual tagging only via the dashboard dropdown |

The Settings page shows which backend is active and what's missing.

### To enable cloud (Anthropic):

1. Click **Settings** (top-right of the dashboard)
2. Paste an Anthropic API key — get one at
   <https://console.anthropic.com/settings/keys>
3. Save

### To enable free local AI (Ollama):

1. Install Ollama from <https://ollama.com/download>
2. Pull the default model: `ollama pull llama3.2:3b` (~2 GB)
3. Refresh the dashboard — Settings will show Ollama as the active backend
4. You can override the model in `config/config.yaml`: `llm.local_model: "qwen2.5:3b"` (or any model you've pulled)

Local AI is slower (1–5 seconds per classification vs ~1 second for cloud) and somewhat less accurate, but it costs nothing and never sends window titles or document content over the network.

---

## Email reports (optional, off by default)

WorkPulse can email you daily and weekly summaries. Off by default. To turn
on:

1. Create a Gmail App Password at <https://myaccount.google.com/apppasswords>
   (your normal Gmail password won't work — Google requires an App Password
   for SMTP).
2. Open the dashboard Settings page.
3. Paste the App Password, enter your Gmail address, tick the
   *"Email me daily &amp; weekly reports"* box.

---

## Where your data lives

Everything is per-machine. None of this is in version control or sent anywhere.

| Location | What it holds |
|---|---|
| `logs\activity_<date>.jsonl` | every window-focus session, day by day |
| `logs\file_events_<date>.jsonl` | every file create/modify/delete in your watched folders |
| `logs\ai_sessions.jsonl` | metadata about Claude API calls (no transcripts) |
| `logs\tray.log` | startup / runtime logs for debugging |
| `vault\` | your project files (or symlinks to where they actually live) |
| `reports\` | generated daily / weekly reports |
| `config\config.yaml` | your project rules — **personal** |
| `config\identity.yaml` | your name + org + a stable random `actor_id` — **personal** |
| `config\secrets.json` | your API key + SMTP password — **personal** |
| `config\learned_tags.json` | rules WorkPulse learned from your corrections — **personal** |

Per the Vision doc §9, no administrator override and no bypass key exist.
Personal data does not leave the machine, ever, via the WorkPulse architecture.

---

## Moving to a second machine

Each install starts blank and gets its own identity. To carry your project
rules across:

1. Set up the new machine with `.\setup.ps1` as above (any answers; the
   identity files will be regenerated)
2. Copy `config\config.yaml` from the old machine to the new one
3. Optionally copy `config\learned_tags.json` if you want your existing
   learned rules to apply

Or use the import flag at setup:

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1 -ImportConfig "C:\path\to\old\config.yaml"
```

Logs and vault are not portable by design (they belong to one machine's
history). Copy them across yourself if you want a continuous record.

---

## Sharing with someone else

Send them the WorkPulse folder (without your `logs\`, `vault\`,
`config\config.yaml`, `config\identity.yaml`, `config\secrets.json`, or
`config\learned_tags.json` — `.gitignore` covers all of these). They run
`setup.ps1`, define their own projects in their own `config\config.yaml`,
and they're off. Their data stays on their machine. Your data stays on
yours. This is the architectural separation the Vision doc §9 calls for.

---

## Troubleshooting

**Dashboard not opening on logon?**
Check `logs\tray.log`. The tray writes a startup line on every launch; any
crash leaves a Python traceback there.

**Dashboard says "Smart auto-tagging is off"?**
You don't have an API key configured. Click Settings, paste a key, save.
Or ignore the banner — manual tagging via the `Tag as ▾` dropdowns works fine.

**A window I see all the time isn't getting tagged?**
Click `Tag as ▾` next to it in the "Needs your attention" panel. Pick a
project. WorkPulse adds a rule and applies it from then on.

**The classifier tagged something wrong.**
Click `Tag as ▾` and pick the right project — the new rule overwrites the
old one (the most recent rule for any pattern always wins).

---

## What's coming (future versions)

Per the Vision doc roadmap:

- **v2** — Intelligent briefings (daily/weekly AI-generated summaries)
- **v3** — Fingerprint Engine: opt-in semantic fingerprints of completed tasks
- **v4** — Institution Brain: one per organisation, receives fingerprints
  (never raw data), pushes relevant context back to individual WorkPulses
- **v6** — Cryptographic attribution layer
- **v7** — Micro-attribution payment infrastructure

The local `actor_id` in `config\identity.yaml` is the seed that v3+ will use
to sign fingerprints. It does not get stamped on raw activity logs today.

---

License & ownership: George Kamau Kibuku. See `WorkPulse_Vision_v1.1.docx`
for the full design philosophy.
