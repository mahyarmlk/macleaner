# Changelog

## 1.0.0 (2026-10-24)

First public release.

- `scan`, `clean`, `big`, `history`, `config` and `status` commands.
- Finds developer caches (Docker, npm, pnpm, Yarn, Bun, pip, uv, Poetry, Cargo, Go, Gradle, Maven, CocoaPods, Homebrew), Xcode and simulator data, Android emulators and SDK leftovers, app caches, logs and project build folders.
- Safety checks: path guard with symlink resolution, protected folders, git-tracked build folders left alone, in-use detection for apps, Gradle, Xcode and emulators, refuses to run as root, and stops if the config file is broken.
- `scan --json` for scripts, `clean --dry-run`, `clean --safe -y` for unattended runs.
