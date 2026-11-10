# Setting up the app on someone's Mac (one time, ~15 minutes)

What they get: a **Reading Challenge** app in Applications. Double-clicking it starts a small local server
(127.0.0.1:8642, token-guarded) and opens the home screen in their browser. No Terminal after setup.

1. **Copy this folder onto their Mac**, e.g. `~/Reading Challenge App/`.
   - With `git clone` (needs access to the GitHub repo): the app then updates itself each time it opens
     (`git pull --ff-only`, never prompting; any failure just runs the copy that's there).
   - Or a zip/AirDrop of the folder: no automatic updates; re-copy to update.
   - Never copy a data folder or `out/` along with it (children's names). `.gitignore` already keeps them out
     of git.
2. In Terminal, in that folder: `bash tools/setup_mac.sh`. It installs `uv` if missing (from astral.sh), creates
   `.venv`, installs `requirements.txt`, and writes `~/Applications/Reading Challenge.app`.
3. Open the app. In **Settings**:
   - **Choose folder…**: the shared iCloud Drive data folder (the one with `2026-27/` etc.). On their Mac it is
     usually under iCloud Drive → Shared.
   - **Claude API key**: paste a key made for this (Claude Console → API keys). Give it its own workspace with a
     monthly spend limit (a month is about $2). It is stored in their login Keychain under
     "Reading Challenge (Claude API key)".
4. Drag the app to their Dock. Give them `docs/how-to.md`.

## Where things are on their Mac

| What | Where |
|---|---|
| Settings (data folder path) | `~/Library/Application Support/Reading Challenge/settings.json` |
| API key | Keychain Access → "Reading Challenge (Claude API key)" |
| Log (errors) | `~/Library/Logs/Reading Challenge.log` |
| The app | `~/Applications/Reading Challenge.app` (just runs `tools/launch.sh`) |

## Developer workflow is unchanged

`python -m rc --data D <command>` still works, and a key from `op run --env-file=.env -- ...` takes priority
over the Keychain. `python -m rc app --data D` runs the app against any folder without touching settings.
