# macleaner

[![CI](https://github.com/mahyarmlk/macleaner/actions/workflows/ci.yml/badge.svg)](https://github.com/mahyarmlk/macleaner/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**Get your disk space back on a developer's Mac, without guessing what's safe to delete.**

On a 256 or 512 GB SSD, dev tools quietly eat the disk: Docker build cache, Gradle and npm caches, `node_modules` in projects you haven't opened in months, Android emulators, simulator runtimes. macleaner finds all of it, shows how big each item is and whether it's safe to remove, and deletes only what you pick.

```
Disk  42.0 GB free of 494.4 GB  ████████████████████████░░░░░░ 92% used

SAFE TO CLEAN  · rebuilt or re-downloaded automatically when needed  64.0 GB
    1    24.0 GB  Build folders in 40 projects untouched for 30+ days        Project build folders
                  node_modules, build/, .gradle, target/, .venv… Biggest: my-app 2.0 GB, api 1.5 GB…
    2    12.0 GB  Docker build cache                                         Docker
    3     9.0 GB  Gradle caches                                              Android & JVM
    4     6.0 GB  npm cache                                                  Package managers
    5     5.0 GB  Xcode DerivedData                                          Xcode & iOS
    6     4.0 GB  App caches (~/Library/Caches)                              System
   …
REVIEW FIRST  · you might still want these  20.0 GB
   14    10.0 GB  Emulator Pixel_8 (whole device)                            Android & JVM
                  Last used 45 days ago. Recreate it in Device Manager.
   …
Total reclaimable: 84.0 GB

Pick what to clean: 1,3,5-8 · s = all safe · s,!4 = safe except #4 · a = everything · Enter = quit
›
```

- **One file with no dependencies.** It runs on the Python 3.9 that ships with macOS.
- **Nothing is deleted until you confirm**, and `--dry-run` shows exactly what would go.
- **Built for developers.** It knows Docker, Xcode, Android, Gradle, npm/pnpm/Yarn/Bun, pip/uv/Poetry, Cargo, Go, Maven, CocoaPods and Homebrew.

## Install

With [pipx](https://pipx.pypa.io) or [uv](https://docs.astral.sh/uv/):

```sh
pipx install git+https://github.com/mahyarmlk/macleaner
# or
uv tool install git+https://github.com/mahyarmlk/macleaner
```

Or grab the single file:

```sh
mkdir -p ~/.local/bin
curl -fsSL https://raw.githubusercontent.com/mahyarmlk/macleaner/main/macleaner.py -o ~/.local/bin/macleaner
chmod +x ~/.local/bin/macleaner
```

Or clone the repo and run `./install.sh`, which links `~/.local/bin/macleaner` to your checkout.

## Use

```sh
macleaner                     # scan, pick items by number, confirm, clean
macleaner scan                # report only, changes nothing
macleaner scan --projects     # list every project's build folders on its own row
macleaner scan --keys         # show item keys (for --only and "exclude")
macleaner scan --json         # machine-readable results
macleaner clean --safe        # select everything marked safe, then confirm
macleaner clean --safe -n     # dry run: show what would be deleted
macleaner clean --only npm,docker-*,gradle-caches -y
macleaner big                 # big files and old downloads to move to the Trash
macleaner history             # what past runs freed
macleaner status              # free space at a glance
```

At the prompt, type `1,3,5-8` to pick items, `s` for every safe item, `s,!4` for every safe item except #4, or `a` for everything.

### Run it on a schedule

`clean --safe -y` cleans every safe item without prompting. It skips anything that needs your password or is in use. It exits with status 1 if any item failed, so it works in cron or launchd:

```sh
macleaner clean --safe -y
```

## What it cleans

**Safe**: the owning tool rebuilds or re-downloads these. The cost is a slower first build or install next time.

| Area | Items |
|---|---|
| Docker | build cache (`docker builder prune -af`) |
| JavaScript | npm, Yarn and Bun caches, unused pnpm store packages (`pnpm store prune`), node-gyp headers |
| Python | pip, uv (`uv cache clean`) and Poetry caches, Playwright browsers |
| JVM / Android | Gradle caches, wrapper distributions and daemon logs, Maven repository, Android tooling cache, emulator quick-boot snapshots |
| Rust / Go | Cargo registry, Go module (`go clean -modcache`) and build caches |
| Apple | Xcode DerivedData, SwiftUI previews, simulator caches, simulators for runtimes you no longer have, system-wide simulator caches (asks for your password) |
| Homebrew | downloads and old versions (`brew cleanup --prune=all -s`) |
| Projects | `node_modules`, `.next`, `.turbo`, `.wrangler`, `build/`, `.gradle`, `target/`, `.venv`, `Pods`, `.build`… in projects untouched for 30+ days |
| System | `~/Library/Caches` (except Apple's own), `~/Library/Logs`, Electron/Chromium app caches (VS Code, Slack, Discord…) |

**Review first**: you might still want these.

Whole Android emulators, Android system images (it shows which emulators use each one), older NDKs, build folders in projects you worked on recently, Docker images and stopped containers, old nvm Node versions, Xcode archives and device support files, simulator runtimes, Hugging Face models, iOS device backups, and emptying the Trash.

**`macleaner big`** lists your own large files and old downloads. It never deletes them: the ones you pick go to the Trash, and you decide whether to empty it.

## How it stays safe

macleaner deletes files, so it checks a lot before it does:

- **It shows the list and asks first.** Nothing is removed unless you pick it and confirm, or pass `-y` with an explicit `--safe` or `--only`.
- **It's fenced in.** Every path is resolved through symlinks and has to sit at least two levels inside your home folder or a configured project root. The only exception is the system-wide simulator cache. It never deletes folders like `~/Library`, `~/Documents`, `~/Downloads`, `~/.ssh` or `~/Library/Application Support`.
- **It doesn't follow symlinks.** A symlink is removed, never what it points to, and a cache folder that is itself a symlink is left alone.
- **It only takes real build output.** A folder only counts when the matching manifest (`package.json`, `build.gradle`, `Cargo.toml`, `Podfile`…) sits next to it. Folders that contain files committed to git are skipped, and so are nested git repositories.
- **It leaves things in use alone.** App caches are skipped while the app is running. Gradle caches are skipped while a Gradle daemon or Android Studio is running, Xcode data while Xcode is open, and an emulator while it is running. The check runs again right before deleting.
- **It never touches** Docker volumes, app data outside cache folders, Apple's own caches, or your documents.
- **It refuses to run with `sudo`.** It asks for your password itself, for the one item that needs it.
- **It stops on a broken config** rather than ignoring your exclusions.
- **It's tested.** The [test suite](tests/test_macleaner.py) covers the guard rails, and CI runs a real read-only scan on macOS.

Before your first real clean, run `macleaner clean --safe --dry-run` to see exactly what would go.

## Config

`macleaner config` creates `~/.config/macleaner/config.json` (or set `MACLEANER_CONFIG` to use another path):

```json
{
  "project_roots": ["~/Developer", "~/Projects", "~/code", "~/src", "~/repos", "~/workspace"],
  "stale_days": 30,
  "min_size_mb": 50,
  "big_file_mb": 500,
  "exclude": ["avd:*", "project:~/Projects/important-app", "~/.m2/repository"]
}
```

| Setting | Meaning |
|---|---|
| `project_roots` | Folders searched for project build output |
| `stale_days` | A project untouched this long counts as stale, so its build folders are marked safe |
| `min_size_mb` | Smaller items are hidden |
| `big_file_mb` | Size threshold for `macleaner big` |
| `exclude` | Item keys (globs allowed) or paths that are never touched. `macleaner scan --keys` shows the keys |

## Good to know

- Quit an app before clearing its caches. If it's still running, macleaner skips it and tells you.
- APFS and Docker Desktop sometimes release freed space a minute or two later, so the "freed" number can start out low.
- Your terminal can't see inside `~/.Trash` without Full Disk Access, so the Trash size may show as `?`. Emptying the Trash still works.
- Time Machine local snapshots can hold space too. macleaner tells you when there are any and how to thin them.

## Contributing

Found a cache it should know about, or something that looked unsafe? See [CONTRIBUTING.md](CONTRIBUTING.md). Please report anything that could delete the wrong files privately, as described in [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE)
