#!/usr/bin/env python3
"""macleaner: find what's eating your Mac's disk and reclaim it safely.

  macleaner                    scan, then pick what to clean
  macleaner scan               only show what can be reclaimed
  macleaner clean --safe -y    clean every "safe" item without asking
  macleaner big                find large files / old downloads to move to Trash
  macleaner history            show past runs and how much they freed

Items are split into two kinds:
  safe    caches and build output that tools rebuild or re-download on demand
  review  things you might still want (emulators, old SDKs, Trash, ...)
Nothing is deleted without showing you the list and asking first, unless you
pass -y.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

__version__ = "1.0.0"

HOME = Path.home()
LIB = HOME / "Library"
DATA_VOLUME = "/System/Volumes/Data" if os.path.isdir("/System/Volumes/Data") else "/"
CONFIG_PATH = Path(os.environ.get("MACLEANER_CONFIG") or HOME / ".config/macleaner/config.json")
HISTORY_PATH = HOME / ".local/share/macleaner/history.jsonl"
SYSTEM_SIM_CACHES = Path("/Library/Developer/CoreSimulator/Caches")

DEFAULT_CONFIG = {
    # Folders searched for node_modules, build/, target/, .venv, ... of your projects.
    "project_roots": ["~/Developer", "~/Projects", "~/code", "~/src", "~/repos", "~/workspace"],
    # A project untouched for this many days counts as stale, so its build folders are "safe".
    "stale_days": 30,
    # Items smaller than this aren't listed.
    "min_size_mb": 50,
    # `macleaner big` lists files at least this large.
    "big_file_mb": 500,
    # Target keys (globs allowed, e.g. "avd:*") or paths that are never touched.
    "exclude": [],
}

SAFE, REVIEW = "safe", "review"


class UnsafePath(Exception):
    pass


# ---------------------------------------------------------------- output helpers

_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _c(code):
    return lambda s: f"\033[{code}m{s}\033[0m" if _COLOR else str(s)


bold, dim, red, green, yellow, cyan = _c("1"), _c("2"), _c("31"), _c("32"), _c("33"), _c("36")


def human(n: int | None) -> str:
    if n is None:
        return "?"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1000 or unit == "TB":
            return f"{size:.0f} {unit}" if unit in ("B", "KB") else f"{size:.1f} {unit}"
        size /= 1000
    return ""


def tilde(p: Path | str) -> str:
    s = str(p)
    return "~" + s[len(str(HOME)):] if s == str(HOME) or s.startswith(str(HOME) + "/") else s


def days_ago(ts: float) -> int:
    return max(0, int((time.time() - ts) // 86400))


def ago(ts: float) -> str:
    d = days_ago(ts)
    return "today" if d == 0 else "1 day ago" if d == 1 else f"{d} days ago"


def status(msg: str):
    if sys.stderr.isatty():
        sys.stderr.write("\r\033[K" + dim(msg))
        sys.stderr.flush()


def die(msg: str, code: int = 2):
    print(red(msg), file=sys.stderr)
    sys.exit(code)


def run(argv: list[str], timeout: float = 30) -> str | None:
    """Run a command quietly and return stdout, or None if it failed."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def free_bytes() -> int:
    return shutil.disk_usage(DATA_VOLUME).free


def disk_line() -> str:
    u = shutil.disk_usage(DATA_VOLUME)
    used = (u.total - u.free) / u.total
    width = 30
    filled = round(used * width)
    color = red if u.free < 20e9 else yellow if u.free < 50e9 else green
    bar = "█" * filled + "░" * (width - filled)
    return f"{bold('Disk')}  {color(bold(human(u.free) + ' free'))} of {human(u.total)}  {color(bar)} {used:.0%} used"


# ---------------------------------------------------------------- config

def load_config() -> dict:
    """Read the config file. A broken config stops the run: it may hold exclusions we must respect."""
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text())
        except (OSError, ValueError) as e:
            die(f"Can't read {tilde(CONFIG_PATH)}: {e}")
        if not isinstance(user, dict):
            die(f"{tilde(CONFIG_PATH)} should contain a JSON object.")
        for key, value in user.items():
            default = DEFAULT_CONFIG.get(key)
            if default is None:
                print(yellow(f"Ignoring unknown setting '{key}' in {tilde(CONFIG_PATH)}."), file=sys.stderr)
            elif isinstance(default, list):
                if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                    die(f"'{key}' in {tilde(CONFIG_PATH)} should be a list of strings.")
                cfg[key] = value
            elif isinstance(value, bool) or not isinstance(value, int) or value < 0:
                die(f"'{key}' in {tilde(CONFIG_PATH)} should be a whole number.")
            else:
                cfg[key] = value
    cfg["project_roots"] = [Path(os.path.expanduser(p)) for p in cfg["project_roots"]]
    return cfg


# ---------------------------------------------------------------- "is it in use?" checks

def processes() -> list[str]:
    """Command lines of every running process."""
    out = run(["/bin/ps", "-axww", "-o", "args="], 10)
    return out.splitlines() if out else []


Busy = Callable[[list[str]], Optional[str]]


def app_running(bundle: str, data_dir: Path | None = None) -> Busy:
    marker = f"/{bundle}.app/Contents/MacOS/"

    def check(procs):
        if any(marker in p for p in procs) or (data_dir and singleton_alive(data_dir)):
            return f"{bundle} is running"
        return None
    return check


def proc_running(pattern: str, reason: str) -> Busy:
    rx = re.compile(pattern)
    return lambda procs: reason if any(rx.search(p) for p in procs) else None


def either(*checks: Busy) -> Busy:
    def check(procs):
        for c in checks:
            reason = c(procs)
            if reason:
                return reason
        return None
    return check


def singleton_alive(app_dir: Path) -> bool:
    """Chromium/Electron apps keep a SingletonLock -> "host-pid" symlink while they run."""
    try:
        pid = os.readlink(app_dir / "SingletonLock").rsplit("-", 1)[-1]
    except OSError:
        return False
    if not pid.isdigit():
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


GRADLE_BUSY = either(proc_running(r"GradleDaemon|GradleWrapperMain|KotlinCompileDaemon",
                                  "a Gradle build or daemon is running (try `gradle --stop`)"),
                     app_running("Android Studio"))
XCODE_BUSY = app_running("Xcode")


# ---------------------------------------------------------------- targets

@dataclass
class Target:
    key: str
    name: str
    group: str
    safety: str
    paths: list[Path] = field(default_factory=list)
    # contents: empty each path but keep the folder · remove: delete the paths · cmd: run `cmds`
    mode: str = "contents"
    cmds: list[list[str]] = field(default_factory=list)
    note: str = ""
    sudo: bool = False
    size: int | None = None        # preset for things du can't measure (Docker)
    approx: bool = False           # size is an upper bound
    busy: Busy | None = None       # returns why it can't be cleaned right now
    blocked: str | None = None     # result of `busy` at scan time
    path_sizes: dict = field(default_factory=dict)

    def total(self) -> int | None:
        if self.size is not None:
            return self.size
        if self.paths and not self.path_sizes:
            return None
        return sum(self.path_sizes.values())


def reclaimable(targets: list[Target]) -> int:
    """Total bytes for a set of targets, counting nested/shared paths only once."""
    sizes: dict[Path, int] = {}
    extra = 0
    for t in targets:
        if t.size is not None:
            extra += t.size
        sizes.update(t.path_sizes)
    total = 0
    for p, n in sizes.items():
        if not any(q != p and p.is_relative_to(q) for q in sizes):
            total += n
    return total + extra


def existing(*paths: Path) -> list[Path]:
    return [p for p in paths if p.exists() or p.is_symlink()]


def children(p: Path) -> list[Path]:
    try:
        return sorted(p.iterdir())
    except OSError:
        return []


def newest_mtime(p: Path, skip: set[str] = frozenset()) -> float:
    latest = 0.0
    for child in children(p):
        if child.name in skip:
            continue
        try:
            latest = max(latest, child.lstat().st_mtime)
        except OSError:
            pass
    try:
        return latest or p.stat().st_mtime
    except OSError:
        return 0.0


# Folders inside ~/Library/Caches that get their own, better-labelled row.
CLAIMED_CACHES = {
    "Homebrew", "pip", "pypoetry", "Yarn", "CocoaPods", "go-build", "ms-playwright", "node-gyp",
}
# Apple's own caches: small, often protected, and some hold state macOS doesn't expect to lose.
APPLE_CACHES = {"CloudKit", "FamilyCircle", "GameKit", "GeoServices", "Metadata", "PassKit", "SiriTTS",
                "TemporaryItems", "familycircled"}
# Application Support folder name -> app bundle name, where they differ.
APP_BUNDLES = {"Code": "Visual Studio Code", "Code - Insiders": "Visual Studio Code - Insiders",
               "Docker Desktop": "Docker", "Slack": "Slack", "discord": "Discord"}
ELECTRON_CACHE_DIRS = ("Cache", "Code Cache", "GPUCache", "CachedData", "CachedExtensionVSIXs",
                       "CachedProfilesData", "DawnCache", "DawnGraphiteCache", "DawnWebGPUCache",
                       "GraphiteDawnCache", "ShaderCache", "Service Worker/CacheStorage",
                       "Service Worker/ScriptCache", "Crashpad/completed", "logs")


def discover_tools(cfg) -> tuple[list[Target], list[str]]:
    T: list[Target] = []
    notes: list[str] = []
    dev = LIB / "Developer"

    def add(t: Target):
        if t.mode != "cmd" or t.paths:
            t.paths = existing(*t.paths)
            if not t.paths:
                return
        T.append(t)

    # --- General
    caches = LIB / "Caches"
    add(Target("user-caches", "App caches (~/Library/Caches)", "System", SAFE,
               [p for p in children(caches) if p.name not in CLAIMED_CACHES | APPLE_CACHES
                and not p.name.startswith("com.apple.")], mode="remove",
               note="Apps rebuild these. Quit browsers first for the best result."))
    add(Target("logs", "Logs & crash reports (~/Library/Logs)", "System", SAFE, [LIB / "Logs"]))
    if (HOME / ".Trash").exists():
        add(Target("trash", "Empty the Trash", "System", REVIEW, [HOME / ".Trash"], mode="cmd",
                   cmds=[["/usr/bin/osascript", "-e", 'tell application "Finder" to empty trash']],
                   note="Permanently deletes everything in the Trash."))
    add(Target("ios-backups", "iPhone/iPad backups", "System", REVIEW,
               [LIB / "Application Support/MobileSync/Backup"],
               note="Only delete if your devices back up to iCloud."))

    # --- Electron / Chromium app caches in Application Support
    for app in children(LIB / "Application Support"):
        found = existing(*(app / d for d in ELECTRON_CACHE_DIRS))
        if found:
            bundle = APP_BUNDLES.get(app.name, app.name)
            add(Target(f"app-cache:{app.name}", f"{app.name} app caches", "App caches", SAFE, found,
                       busy=app_running(bundle, app)))

    # --- Homebrew
    brew = shutil.which("brew")
    if brew:
        add(Target("homebrew", "Homebrew downloads & old versions", "Package managers", SAFE,
                   [caches / "Homebrew"], mode="cmd", cmds=[[brew, "cleanup", "--prune=all", "-s"]]))

    # --- JavaScript
    add(Target("npm", "npm cache", "Package managers", SAFE,
               [HOME / ".npm/_cacache", HOME / ".npm/_npx", HOME / ".npm/_logs"]))
    add(Target("yarn", "Yarn cache", "Package managers", SAFE, [caches / "Yarn"]))
    add(Target("bun", "Bun install cache", "Package managers", SAFE, [HOME / ".bun/install/cache"]))
    pnpm = shutil.which("pnpm")
    if pnpm:
        store = (run([pnpm, "store", "path"], 10) or "").strip()
        if store:
            add(Target("pnpm", "pnpm store (unused packages)", "Package managers", SAFE,
                       [Path(store)], mode="cmd", cmds=[[pnpm, "store", "prune"]], approx=True,
                       note="Removes only packages no project uses; size shown is the whole store."))
    add(Target("node-gyp", "node-gyp headers", "Package managers", SAFE, [caches / "node-gyp"]))
    nvm_versions = HOME / ".nvm/versions/node"
    if nvm_versions.exists():
        keep = {(run(["node", "--version"], 5) or "").strip()}
        try:
            keep.add((HOME / ".nvm/alias/default").read_text().strip())
        except OSError:
            pass
        keep.discard("")
        for v in children(nvm_versions):
            if not any(v.name == k or v.name.startswith("v" + k.lstrip("v") + ".") for k in keep):
                add(Target(f"nvm:{v.name}", f"Node {v.name} (nvm)", "Package managers", REVIEW, [v],
                           mode="remove", note="Not your current or default Node version."))

    # --- Python
    add(Target("pip", "pip cache", "Package managers", SAFE, [caches / "pip"]))
    add(Target("poetry", "Poetry cache", "Package managers", SAFE, [caches / "pypoetry"]))
    uv = shutil.which("uv")
    add(Target("uv", "uv cache", "Package managers", SAFE, [HOME / ".cache/uv"],
               **({"mode": "cmd", "cmds": [[uv, "cache", "clean"]]} if uv else {})))
    add(Target("huggingface", "Hugging Face models & datasets", "Package managers", REVIEW,
               [HOME / ".cache/huggingface"], note="Re-downloaded when a model is used again."))
    add(Target("playwright", "Playwright browsers", "Package managers", SAFE, [caches / "ms-playwright"],
               note="Run `npx playwright install` to get them back."))

    # --- JVM / Rust / Go
    add(Target("gradle-caches", "Gradle caches", "Android & JVM", SAFE, [HOME / ".gradle/caches"],
               busy=GRADLE_BUSY, note="Re-downloaded on the next build (the first build will be slow)."))
    add(Target("gradle-wrappers", "Gradle wrapper distributions", "Android & JVM", SAFE,
               [HOME / ".gradle/wrapper/dists"], busy=GRADLE_BUSY))
    add(Target("gradle-daemon", "Gradle daemon logs", "Android & JVM", SAFE, [HOME / ".gradle/daemon"],
               busy=GRADLE_BUSY))
    add(Target("maven", "Maven repository (~/.m2)", "Android & JVM", SAFE, [HOME / ".m2/repository"]))
    add(Target("cargo", "Cargo registry cache", "Package managers", SAFE,
               [HOME / ".cargo/registry/cache", HOME / ".cargo/registry/src", HOME / ".cargo/git/checkouts"],
               busy=proc_running(r"(^|/)(cargo|rustc) ", "cargo is running")))
    go = shutil.which("go")
    if go:
        add(Target("go-mod", "Go module cache", "Package managers", SAFE, [HOME / "go/pkg/mod"],
                   mode="cmd", cmds=[[go, "clean", "-modcache"]]))
    add(Target("go-build", "Go build cache", "Package managers", SAFE, [caches / "go-build"]))
    add(Target("cocoapods", "CocoaPods cache", "Xcode & iOS", SAFE, [caches / "CocoaPods"]))

    # --- Xcode & simulators
    add(Target("xcode-deriveddata", "Xcode DerivedData", "Xcode & iOS", SAFE, [dev / "Xcode/DerivedData"],
               busy=XCODE_BUSY))
    add(Target("xcode-previews", "SwiftUI preview data", "Xcode & iOS", SAFE, [dev / "Xcode/UserData/Previews"],
               busy=XCODE_BUSY))
    add(Target("sim-caches", "Simulator caches", "Xcode & iOS", SAFE, [dev / "CoreSimulator/Caches"],
               busy=either(XCODE_BUSY, app_running("Simulator"))))
    add(Target("xcode-devicesupport", "Device support files", "Xcode & iOS", REVIEW,
               [dev / "Xcode/iOS DeviceSupport", dev / "Xcode/watchOS DeviceSupport",
                dev / "Xcode/tvOS DeviceSupport", dev / "Xcode/visionOS DeviceSupport"],
               busy=XCODE_BUSY, note="Recreated the next time you plug in a device."))
    add(Target("xcode-archives", "Xcode archives", "Xcode & iOS", REVIEW, [dev / "Xcode/Archives"],
               note="Needed to symbolicate crash reports of builds you shipped."))
    has_xcode = any(p.name.startswith("Xcode") and p.suffix == ".app" for p in children(Path("/Applications")))
    if has_xcode and run(["/usr/bin/xcrun", "simctl", "help"], 10) is not None:
        unavailable = []
        try:
            data = json.loads(run(["/usr/bin/xcrun", "simctl", "list", "devices", "unavailable", "-j"], 20) or "{}")
            for devices in data.get("devices", {}).values():
                unavailable += [dev / "CoreSimulator/Devices" / d["udid"] for d in devices if "udid" in d]
        except (ValueError, AttributeError):
            pass
        add(Target("sim-unavailable", "Simulators for runtimes you no longer have", "Xcode & iOS", SAFE,
                   unavailable, mode="cmd", cmds=[["/usr/bin/xcrun", "simctl", "delete", "unavailable"]]))
        try:
            runtimes = json.loads(run(["/usr/bin/xcrun", "simctl", "runtime", "list", "-j"], 20) or "{}")
        except ValueError:
            runtimes = {}
        for ident, rt in (runtimes.items() if isinstance(runtimes, dict) else []):
            if not isinstance(rt, dict) or not isinstance(rt.get("sizeBytes"), int):
                continue
            platform = str(rt.get("platformIdentifier", "")).split(".")[-1].replace("simulator", "")
            add(Target(f"sim-runtime:{rt.get('version', ident)}",
                       f"{platform} {rt.get('version', '')} simulator runtime".strip(), "Xcode & iOS", REVIEW,
                       mode="cmd", cmds=[["/usr/bin/xcrun", "simctl", "runtime", "delete", ident]],
                       size=rt["sizeBytes"], busy=XCODE_BUSY,
                       note="Re-download it from Xcode › Settings › Components."))
    if SYSTEM_SIM_CACHES.is_dir():
        add(Target("sim-system-caches", "Simulator runtime caches (system-wide)", "Xcode & iOS", SAFE,
                   children(SYSTEM_SIM_CACHES), mode="remove", sudo=True,
                   busy=either(XCODE_BUSY, app_running("Simulator")),
                   note=("Xcode isn't installed, so nothing uses these." if not has_xcode
                         else "Rebuilt the next time a simulator boots.") + " Asks for your password."))

    # --- Android
    sdk = Path(os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT") or LIB / "Android/sdk")
    avd_root = Path(os.environ.get("ANDROID_AVD_HOME") or HOME / ".android/avd")
    used_images: dict[str, list[str]] = {}
    for avd in [p for p in children(avd_root) if p.suffix == ".avd" and p.is_dir()]:
        name = avd.stem
        try:
            m = re.search(r"^image\.sysdir\.1\s*=\s*(.+)$", (avd / "config.ini").read_text(), re.M)
            if m:
                used_images.setdefault(m.group(1).strip().strip("/"), []).append(name)
        except OSError:
            pass
        emulator_busy = proc_running(rf"qemu-system.*(-avd\s+|@){re.escape(name)}(\s|$)",
                                     f"the {name} emulator is running")
        add(Target(f"avd-snapshots:{name}", f"Emulator {name}: quick-boot snapshots", "Android & JVM", SAFE,
                   [avd / "snapshots"], busy=emulator_busy,
                   note="The emulator cold-boots once, then saves a new snapshot."))
        add(Target(f"avd:{name}", f"Emulator {name} (whole device)", "Android & JVM", REVIEW,
                   [avd, avd_root / f"{name}.ini"], mode="remove", busy=emulator_busy,
                   note=f"Last used {ago(newest_mtime(avd))}. Recreate it in Device Manager."))
    for api in children(sdk / "system-images"):
        for tag in children(api):
            for abi in children(tag):
                rel = f"system-images/{api.name}/{tag.name}/{abi.name}"
                users = used_images.get(rel, [])
                add(Target(f"sysimg:{api.name}/{tag.name}/{abi.name}",
                           f"Android system image {api.name} {tag.name}", "Android & JVM", REVIEW,
                           [abi], mode="remove", busy=proc_running(r"qemu-system", "an emulator is running"),
                           note=f"Used by {', '.join(users)}." if users else "Not used by any emulator."))
    ndks = [p for p in children(sdk / "ndk") if p.is_dir()]
    ndks.sort(key=lambda p: [int(x) if x.isdigit() else 0 for x in re.split(r"[.\-]", p.name)])
    for ndk in ndks[:-1]:  # keep the newest
        add(Target(f"ndk:{ndk.name}", f"Android NDK {ndk.name}", "Android & JVM", REVIEW, [ndk], mode="remove",
                   busy=GRADLE_BUSY, note="An older NDK; the newest one is kept."))
    add(Target("android-cache", "Android tooling cache", "Android & JVM", SAFE, [HOME / ".android/cache"]))

    # --- Docker
    docker = shutil.which("docker")
    if docker:
        out = run([docker, "system", "df", "--format", "{{json .}}"], 20)
        if out is None:
            if (LIB / "Containers/com.docker.docker").exists():
                notes.append("Docker isn't running. Start Docker Desktop and re-scan to include Docker cleanup.")
        else:
            rows = {}
            for line in out.splitlines():
                try:
                    row = json.loads(line)
                    rows[row["Type"]] = parse_docker_size(row.get("Reclaimable", "0B"))
                except (ValueError, KeyError, TypeError):
                    pass
            add(Target("docker-build-cache", "Docker build cache", "Docker", SAFE, mode="cmd",
                       cmds=[[docker, "builder", "prune", "-af"]], size=rows.get("Build Cache", 0),
                       note="Next image builds start from scratch."))
            add(Target("docker-images", "Docker unused images & stopped containers", "Docker", REVIEW,
                       mode="cmd", cmds=[[docker, "container", "prune", "-f"], [docker, "image", "prune", "-af"]],
                       size=rows.get("Images", 0) + rows.get("Containers", 0),
                       note="Keeps running containers and all volumes. "
                            "Docker Desktop's disk shrinks over a few minutes."))

    snaps = run(["/usr/bin/tmutil", "listlocalsnapshots", "/"], 10) or ""
    n_snaps = sum(1 for line in snaps.splitlines() if "com.apple" in line)
    if n_snaps:
        notes.append(f"{n_snaps} Time Machine local snapshot(s) may be holding space. "
                     "Free it with: tmutil thinlocalsnapshots / 999999999999 4")
    return T, notes


def parse_docker_size(s: str) -> int:
    m = re.match(r"\s*([\d.]+)\s*([kKMGT]?B)", s or "")
    if not m:
        return 0
    mult = {"B": 1, "kB": 1e3, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12}[m.group(2)]
    return int(float(m.group(1)) * mult)


# Build-output folders, and the file that must sit next to them for us to trust the match.
JS = ("package.json",)
GRADLE = ("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts")
PY = ("pyproject.toml", "requirements.txt", "setup.py", "uv.lock", "Pipfile")
ARTIFACTS = {
    "node_modules": JS, ".next": JS, ".nuxt": JS, ".turbo": JS, ".svelte-kit": JS, ".output": JS,
    ".expo": JS, ".parcel-cache": JS, ".astro": JS, ".vercel": JS,
    ".wrangler": ("wrangler.toml", "wrangler.json", "wrangler.jsonc"),
    "target": ("Cargo.toml",),
    "build": GRADLE + ("pubspec.yaml",), ".gradle": GRADLE, ".kotlin": GRADLE, ".cxx": GRADLE,
    ".dart_tool": ("pubspec.yaml",),
    "Pods": ("Podfile",), ".build": ("Package.swift",), "DerivedData": ("*.xcodeproj", "*.xcworkspace"),
    ".venv": PY, "venv": PY,
}


def _has_marker(names: set[str], markers: tuple[str, ...]) -> bool:
    return any(fnmatch.filter(names, m) if "*" in m else m in names for m in markers)


def find_git() -> str | None:
    """git, unless the only one is Apple's stub that pops up an installer dialog."""
    git = shutil.which("git")
    if not git:
        return None
    if os.path.realpath(git) == "/usr/bin/git" and run(["/usr/bin/xcode-select", "-p"], 5) is None:
        return None
    return git


def git_tracked(git: str | None, repo: Path, arts: list[Path]) -> set[Path]:
    """The folders among `arts` that hold files committed to git, so they aren't throwaway build output."""
    if not (repo / ".git").exists():
        return set()
    if git is None:
        return set(arts)  # can't check, so don't touch anything
    rels = [str(a.relative_to(repo)) for a in arts]
    out = run([git, "-C", str(repo), "ls-files", "-z", "--", *rels], 60)
    if out is None:
        return set(arts)
    files = [f for f in out.split("\0") if f]
    return {a for a, rel in zip(arts, rels) if any(f == rel or f.startswith(rel + "/") for f in files)}


def discover_projects(cfg) -> list[Target]:
    found: dict[Path, list[Path]] = {}  # project root -> artifact folders

    def walk(d: Path, depth: int, project: Path | None):
        entries = children(d)
        names = {e.name for e in entries}
        if ".git" in names:
            project = d
        for e in entries:
            if e.is_symlink() or not e.is_dir():
                continue
            if e.name in ARTIFACTS:
                if _has_marker(names, ARTIFACTS[e.name]) and not (e / ".git").exists():
                    found.setdefault(project or d, []).append(e)
            elif depth < 6 and not e.name.startswith(".") and e.name not in ("Library", "Applications"):
                walk(e, depth + 1, project)

    for root in cfg["project_roots"]:
        if root.is_dir():
            status(f"Looking for build folders in {tilde(root)}…")
            walk(root, 0, None)

    git = find_git()
    with ThreadPoolExecutor(max_workers=8) as pool:
        tracked = dict(zip(found, pool.map(lambda kv: git_tracked(git, *kv), found.items())))

    targets = []
    for proj, arts in found.items():
        arts = [a for a in arts if a not in tracked[proj]]
        if not arts:
            continue
        last = max([newest_mtime(proj, skip=set(ARTIFACTS)),
                    *(f.stat().st_mtime for f in existing(proj / ".git/index", proj / ".git/HEAD"))])
        stale = days_ago(last) >= cfg["stale_days"]
        kinds = ", ".join(sorted({a.name for a in arts}))
        targets.append(Target(f"project:{tilde(proj)}", f"{tilde(proj)}  ({kinds})", "Project build folders",
                              SAFE if stale else REVIEW, arts, mode="remove",
                              note=f"Last touched {ago(last)}. Reinstall/rebuild to get them back."))
    return targets


def group_projects(targets: list[Target], cfg) -> list[Target]:
    """Fold the per-project rows into one row for stale projects and one for active ones."""
    projects = [t for t in targets if t.key.startswith("project:")]
    rest = [t for t in targets if not t.key.startswith("project:")]
    days = cfg["stale_days"]
    for safety, key, title in ((SAFE, "projects-stale", "{n} untouched for {d}+ days"),
                               (REVIEW, "projects-active", "{n} you worked on in the last {d} days")):
        group = sorted((t for t in projects if t.safety == safety), key=lambda t: -(t.total() or 0))
        if not group:
            continue
        n = f"{len(group)} project" + ("" if len(group) == 1 else "s")
        merged = Target(key, "Build folders in " + title.format(n=n, d=days), "Project build folders",
                        safety, [p for t in group for p in t.paths], mode="remove")
        for t in group:
            merged.path_sizes.update(t.path_sizes)
        biggest = ", ".join(f"{t.key.split('/')[-1]} {human(t.total())}" for t in group[:3])
        merged.note = (f"node_modules, build/, .gradle, target/, .venv… Biggest: {biggest}. "
                       "Reinstall/rebuild to get them back. `--projects` lists each one.")
        rest.append(merged)
    return rest


def measure(targets: list[Target]):
    """Fill path_sizes with `du`, one process per target, in parallel."""
    todo = [t for t in targets if t.paths and t.size is None]

    def du(t: Target):
        try:
            r = subprocess.run(["/usr/bin/du", "-sk", *map(str, t.paths)], capture_output=True, text=True,
                               timeout=900, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired):
            return
        for line in r.stdout.splitlines():
            kb, _, path = line.partition("\t")
            if kb.isdigit():
                t.path_sizes[Path(path)] = int(kb) * 1024
        if "Operation not permitted" in r.stderr and not any(t.path_sizes.values()):
            t.path_sizes.clear()  # unknown, not zero

    pool = ThreadPoolExecutor(max_workers=8)
    try:
        for done, _ in enumerate(pool.map(du, todo), 1):
            status(f"Measuring… {done}/{len(todo)}")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
        status("")


def scan(cfg, per_project: bool = False) -> tuple[list[Target], list[str]]:
    status("Looking for caches…")
    targets, notes = discover_tools(cfg)
    targets += discover_projects(cfg)
    excluded = cfg["exclude"]

    def is_excluded(t: Target) -> bool:
        return any(fnmatch.fnmatch(t.key, x) or any(tilde(p) == x.rstrip("/") or str(p) == x.rstrip("/")
                                                    for p in t.paths) for x in excluded)
    targets = [t for t in targets if not is_excluded(t)]
    measure(targets)
    if not per_project:
        targets = group_projects(targets, cfg)
    min_size = cfg["min_size_mb"] * 1e6
    for t in targets:
        if t.key == "trash" and t.total() is None:
            t.note += " (Size unknown: give your terminal Full Disk Access to measure it.)"
    targets = [t for t in targets if (t.total() is None and t.key == "trash") or (t.total() or 0) >= min_size]
    procs = processes()
    for t in targets:
        t.blocked = t.busy(procs) if t.busy else None
    targets.sort(key=lambda t: (t.safety != SAFE, -(t.total() or 0)))
    return targets, notes


# ---------------------------------------------------------------- presentation

def print_targets(targets: list[Target], notes: list[str], show_keys: bool = False):
    print()
    print(disk_line())
    if not targets:
        print(green("\nNothing big to clean up. 🎉"))
    n = 0
    for safety, title, blurb in ((SAFE, "SAFE TO CLEAN", "rebuilt or re-downloaded automatically when needed"),
                                 (REVIEW, "REVIEW FIRST", "you might still want these")):
        group = [t for t in targets if t.safety == safety]
        if not group:
            continue
        color = green if safety == SAFE else yellow
        print(f"\n{color(bold(title))}  {dim('· ' + blurb)}  {bold(human(reclaimable(group)))}")
        for t in group:
            n += 1
            size = ("≤" if t.approx else "") + human(t.total())
            name = t.name if len(t.name) <= 58 else t.name[:57] + "…"
            print(f"  {dim(f'{n:>3}')}  {bold(f'{size:>9}')}  {name:<58} {dim(t.group)}")
            if t.note or show_keys:
                print(f"  {'':>3}  {'':>9}  {dim(t.note)}{cyan('  [' + t.key + ']') if show_keys else ''}")
            if t.blocked:
                print(f"  {'':>3}  {'':>9}  {yellow('⚠ ' + t.blocked + ', so this will be skipped. Quit it first.')}")
    if targets:
        print(f"\n{bold('Total reclaimable:')} {bold(cyan(human(reclaimable(targets))))}")
    for note in notes:
        print(yellow("• ") + note)


def targets_json(targets: list[Target]) -> str:
    return json.dumps([{"key": t.key, "name": t.name, "group": t.group, "safety": t.safety, "bytes": t.total(),
                        "approximate": t.approx, "needs_password": t.sudo, "in_use": t.blocked, "note": t.note,
                        "paths": [str(p) for p in t.paths]} for t in targets], indent=2)


def parse_selection(text: str, targets: list[Target]) -> list[Target]:
    chosen: set[int] = set()
    removed: set[int] = set()
    for tok in re.split(r"[,\s]+", text.strip().lower()):
        if not tok:
            continue
        bucket = removed if tok.startswith("!") else chosen
        tok = tok.lstrip("!")
        if tok in ("s", "safe"):
            bucket.update(i for i, t in enumerate(targets) if t.safety == SAFE)
        elif tok in ("a", "all"):
            bucket.update(range(len(targets)))
        elif re.fullmatch(r"\d+-\d+", tok):
            a, b = map(int, tok.split("-"))
            if not (1 <= a <= b <= len(targets)):
                raise ValueError(f"don't have items {tok}")
            bucket.update(range(a - 1, b))
        elif tok.isdigit():
            if not 1 <= int(tok) <= len(targets):
                raise ValueError(f"don't have item {tok}")
            bucket.add(int(tok) - 1)
        else:
            raise ValueError(f"don't understand '{tok}'")
    return [targets[i] for i in sorted(chosen - removed)]


def prompt_selection(targets: list[Target]) -> list[Target]:
    print(dim("\nPick what to clean: 1,3,5-8 · s = all safe · s,!4 = safe except #4 · a = everything · Enter = quit"))
    while True:
        try:
            text = input(bold("› "))
        except (EOFError, KeyboardInterrupt):
            print()
            return []
        if not text.strip():
            return []
        try:
            return parse_selection(text, targets)
        except ValueError as e:
            print(red(f"Sorry, I {e}."))


def confirm(selected: list[Target]) -> bool:
    print(f"\n{bold('About to clean:')}")
    for t in selected:
        extra = yellow("  (asks for your password)") if t.sudo else ""
        if t.blocked:
            extra = yellow(f"  (skipped: {t.blocked})")
        print(f"  {human(t.total()):>9}  {t.name}{extra}")
    print(f"  {bold(f'{human(reclaimable(selected)):>9}')}  {bold('total')}")
    try:
        return input(bold("Go ahead? [y/N] ")).strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


# ---------------------------------------------------------------- cleaning

def _real(p: Path) -> Path:
    """Resolve symlinks in the parent folders, but not in the last part (a symlink is removed, not followed)."""
    p = Path(os.path.abspath(p))
    return Path(os.path.realpath(p.parent)) / p.name


def protected(cfg) -> set[Path]:
    lib = HOME / "Library"
    paths = [HOME, lib, lib / "Application Support", lib / "Containers", lib / "Group Containers",
             lib / "Caches", lib / "Developer", lib / "Mobile Documents", lib / "CloudStorage", HOME / ".Trash",
             HOME / "Documents", HOME / "Desktop", HOME / "Downloads", HOME / "Pictures", HOME / "Movies",
             HOME / "Music", HOME / "Public", HOME / ".ssh", HOME / ".gnupg", HOME / ".config", HOME / ".local",
             HOME / ".cache", SYSTEM_SIM_CACHES, *cfg["project_roots"]]
    return {Path(os.path.realpath(p)) for p in paths}


def guard(p: Path, cfg) -> Path:
    """Return the real path to delete, or raise UnsafePath if it's outside the places macleaner may touch."""
    if not Path(p).is_absolute():
        raise UnsafePath(f"refusing to delete relative path {p}")
    real = _real(p)
    home = Path(os.path.realpath(HOME))
    in_home = real.is_relative_to(home) and len(real.relative_to(home).parts) >= 2
    roots = [Path(os.path.realpath(r)) for r in (SYSTEM_SIM_CACHES, *cfg["project_roots"])]
    in_root = any(real != r and real.is_relative_to(r) for r in roots)
    if real in protected(cfg) or not (in_home or in_root):
        raise UnsafePath(f"refusing to delete {p}")
    return real


def _rmtree(path: Path, on_error):
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=lambda f, p, e: on_error(f, p))
    else:
        shutil.rmtree(path, onerror=lambda f, p, e: on_error(f, p))


def remove_path(p: Path, cfg) -> int:
    """Delete a file or folder tree without following symlinks. Returns how many entries couldn't be removed."""
    p = guard(p, cfg)
    failed = 0

    def retry(func, path):
        nonlocal failed
        try:  # read-only files and folders (e.g. Go's module cache)
            os.chmod(os.path.dirname(path), 0o700, follow_symlinks=False)
            if not os.path.islink(path):
                os.chmod(path, 0o700)
            func(path)
        except (OSError, NotImplementedError):
            failed += 1

    try:
        if p.is_symlink() or not p.is_dir():
            p.unlink()
        else:
            _rmtree(p, retry)
    except FileNotFoundError:
        pass
    except OSError:
        failed += 1
    return failed


def clean_target(t: Target, cfg, dry_run: bool, verbose: bool = False) -> str | None:
    """Clean one target. Returns a problem description, or None if it went fine."""
    if t.mode == "cmd":
        for argv in t.cmds:
            if dry_run:
                print(dim(f"      would run: {' '.join(argv)}"))
                continue
            try:
                r = subprocess.run(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            except OSError as e:
                return f"couldn't run {argv[0]}: {e}"
            if r.returncode != 0:
                tail = (r.stderr or r.stdout).strip().splitlines()[-1:] or ["failed"]
                return f"`{' '.join(argv)}`: {tail[0]}"
        return None

    victims = []
    for p in t.paths:
        if t.mode == "contents":
            if p.is_symlink():
                return f"{tilde(p)} is a symlink, so it was left alone"
            victims += children(p)
        else:
            victims.append(p)
    victims = [guard(v, cfg) for v in victims]  # check everything before deleting anything
    if dry_run:
        for v in victims[:5]:
            print(dim(f"      would delete: {tilde(v)}"))
        if len(victims) > 5:
            print(dim(f"      … and {len(victims) - 5} more"))
        return None
    if t.sudo:
        print(dim(f"      macleaner needs your password to delete {tilde(t.paths[0].parent)}/…"))
        sudo = ["/usr/bin/sudo"] if sys.stdin.isatty() else ["/usr/bin/sudo", "-n"]
        # sudo prompts on the terminal itself, so capturing stderr only collects rm's complaints.
        r = subprocess.run([*sudo, "/bin/rm", "-rf", "--", *map(str, victims)], stderr=subprocess.PIPE, text=True)
        if r.returncode == 0:
            return None
        if "Operation not permitted" in r.stderr:
            return ("macOS blocked the delete even with your password. Give your terminal app Full Disk Access "
                    "(System Settings › Privacy & Security › Full Disk Access), reopen it, and run this again.")
        return "couldn't get admin rights"
    failed = 0
    for v in victims:
        failed += remove_path(v, cfg)
        if verbose:
            print(dim(f"      deleted {tilde(v)}"))
    return f"{failed} item(s) were in use or protected and were skipped" if failed else None


def do_clean(selected: list[Target], cfg, dry_run: bool, verbose: bool = False) -> int:
    """Clean the selected targets. Returns the number of targets that had problems."""
    before = free_bytes()
    procs = processes()
    problems = 0
    print()
    for t in selected:
        if dry_run:
            print(f"  {dim('•')} {t.name}")
            clean_target(t, cfg, dry_run=True)
            continue
        reason = t.busy(procs) if t.busy else None
        if reason:
            print(f"  {yellow('–')} {t.name} {dim(f'(skipped: {reason})')}")
            continue
        if t.sudo and not sys.stdin.isatty():
            print(f"  {yellow('–')} {t.name} {dim('(skipped: needs your password, run it in a terminal)')}")
            continue
        start = free_bytes()
        if t.sudo or verbose:
            print(f"  {dim('…')} {t.name}")
        else:
            print(f"  {dim('…')} {t.name}", end="\r" if _COLOR else "\n", flush=True)
        try:
            problem = clean_target(t, cfg, dry_run=False, verbose=verbose)
        except UnsafePath as e:
            problem = str(e)
        freed = max(0, free_bytes() - start)
        mark = yellow("!") if problem else green("✓")
        print(f"\033[K  {mark} {t.name}  {dim(human(freed) + ' freed') if freed else ''}" if _COLOR
              else f"  {mark} {t.name}  {human(freed) + ' freed' if freed else ''}")
        if problem:
            problems += 1
            print(f"      {yellow(problem)}")
    if dry_run:
        print(dim("\nDry run: nothing was deleted."))
        return 0
    after = free_bytes()
    freed = max(0, after - before)
    print(f"\n{bold('Freed')} {bold(green(human(freed)))}. {disk_line()}")
    if freed < reclaimable(selected) * 0.5:
        print(dim("Some space can take a minute to show up (APFS and Docker Desktop release it lazily)."))
    try:
        HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        with HISTORY_PATH.open("a") as f:
            f.write(json.dumps({"time": datetime.now().isoformat(timespec="seconds"), "freed": freed,
                                "free_after": after, "items": [t.key for t in selected]}) + "\n")
    except OSError:
        pass
    return problems


# ---------------------------------------------------------------- `big`: your own files

def find_big_files(cfg) -> list[tuple[Path, int, float]]:
    min_mb = cfg["big_file_mb"]
    status(f"Looking for files over {min_mb} MB in your home folder…")
    prune = ["(", "-path", str(LIB), "-o", "-path", str(HOME / ".*"), "-o", "-name", "node_modules",
             "-o", "-name", ".git", "-o", "-name", "*.app", ")", "-prune"]
    try:
        r = subprocess.run(["/usr/bin/find", str(HOME), "-xdev", *prune, "-o", "-type", "f",
                            "-size", f"+{min_mb}M", "-print"], capture_output=True, text=True, timeout=900)
        paths = [Path(p) for p in r.stdout.splitlines()]
    except subprocess.TimeoutExpired:
        paths = []
    items = {}
    for p in paths:
        try:
            st = p.stat()
            items[p] = (st.st_size, st.st_mtime)
        except OSError:
            pass
    # Old downloads, files or folders.
    for p in children(HOME / "Downloads"):
        try:
            mtime = p.lstat().st_mtime
        except OSError:
            continue
        if p not in items and days_ago(mtime) >= cfg["stale_days"]:
            items[p] = (None, mtime)
    pending = [p for p, (size, _) in items.items() if size is None]
    if pending:
        r = subprocess.run(["/usr/bin/du", "-sk", *map(str, pending)], capture_output=True, text=True)
        for line in r.stdout.splitlines():
            kb, _, path = line.partition("\t")
            if kb.isdigit() and Path(path) in items:
                items[Path(path)] = (int(kb) * 1024, items[Path(path)][1])
    status("")
    rows = [(p, s or 0, m) for p, (s, m) in items.items() if (s or 0) >= 10e6]
    return sorted(rows, key=lambda r: -r[1])[:60]


def move_to_trash(paths: list[str]) -> bool:
    if os.path.exists("/usr/bin/trash"):  # macOS 15+
        return subprocess.run(["/usr/bin/trash", *paths]).returncode == 0
    quoted = ", ".join('POSIX file "{}"'.format(p.replace("\\", "\\\\").replace('"', '\\"')) for p in paths)
    return subprocess.run(["/usr/bin/osascript", "-e", f"tell application \"Finder\" to delete {{{quoted}}}"],
                          capture_output=True).returncode == 0


def cmd_big(args, cfg) -> int:
    rows = find_big_files(cfg)
    print()
    print(disk_line())
    if not rows:
        print(green("\nNo big or old files found."))
        return 0
    print(f"\n{bold('BIG FILES & OLD DOWNLOADS')}  {dim('· yours to decide · picked items go to the Trash')}")
    for i, (p, size, mtime) in enumerate(rows, 1):
        print(f"  {dim(f'{i:>3}')}  {bold(f'{human(size):>9}')}  {dim(f'{days_ago(mtime):>4}d')}  {tilde(p)}")
    if not sys.stdin.isatty():
        return 0
    print(dim("\nPick items to move to the Trash: 1,3,5-8 · Enter = quit"))
    picked = []
    while True:
        try:
            text = input(bold("› ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not text:
            return 0
        try:
            picked = parse_selection(text, [Target(str(p), "", "", REVIEW) for p, _, _ in rows])
            break
        except ValueError as e:
            print(red(f"Sorry, I {e}."))
    chosen = [r for r in rows if str(r[0]) in {t.key for t in picked}]
    if not move_to_trash([str(p) for p, _, _ in chosen]):
        print(yellow("Some items couldn't be moved to the Trash."))
    print(f"Moved {bold(human(sum(s for _, s, _ in chosen)))} to the Trash.")
    try:
        empty = input(bold("Empty the Trash now to actually free the space? This deletes everything in it. [y/N] "))
    except (EOFError, KeyboardInterrupt):
        print()
        return 0
    if empty.strip().lower() in ("y", "yes"):
        before = free_bytes()
        subprocess.run(["/usr/bin/osascript", "-e", 'tell application "Finder" to empty trash'], capture_output=True)
        print(f"Freed {bold(green(human(max(0, free_bytes() - before))))}. {disk_line()}")
    return 0


# ---------------------------------------------------------------- commands

def cmd_scan(args, cfg) -> int:
    targets, notes = scan(cfg, args.projects)
    if args.json:
        print(targets_json(targets))
    else:
        print_targets(targets, notes, args.keys)
    return 0


def cmd_clean(args, cfg) -> int:
    targets, notes = scan(cfg, args.projects)
    if args.safe or args.only:
        patterns = [k.strip() for k in (args.only or "").split(",") if k.strip()]
        selected = [t for t in targets
                    if (args.safe and t.safety == SAFE) or any(fnmatch.fnmatch(t.key, k) for k in patterns)]
        if not args.yes:
            print_targets(targets, notes)
    else:
        print_targets(targets, notes)
        if not targets:
            return 0
        if not sys.stdin.isatty():
            print(dim("\nRun this in a terminal to pick items, or use --safe / --only."))
            return 0
        selected = prompt_selection(targets)
    if not selected:
        print(dim("Nothing selected."))
        return 0
    if not args.yes and not args.dry_run and not confirm(selected):
        return 0
    return 1 if do_clean(selected, cfg, args.dry_run, args.verbose) else 0


def cmd_history(args, cfg) -> int:
    if not HISTORY_PATH.exists():
        print("No cleanups recorded yet.")
        return 0
    total = 0
    print(bold(f"{'When':<20} {'Freed':>10} {'Free after':>11}  Items"))
    for line in HISTORY_PATH.read_text().splitlines():
        try:
            e = json.loads(line)
            total += e["freed"]
            items = ", ".join(e["items"])
            print(f"{e['time'].replace('T', ' '):<20} {human(e['freed']):>10} {human(e['free_after']):>11}  "
                  f"{dim(items if len(items) < 60 else items[:59] + '…')}")
        except (ValueError, KeyError, TypeError):
            continue
    print(f"\n{bold('All time:')} {green(human(total))} freed")
    return 0


def cmd_config(args, cfg) -> int:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n")
        print(f"Created {tilde(CONFIG_PATH)} with the defaults.")
    print(CONFIG_PATH)
    return 0


def cmd_status(args, cfg) -> int:
    print(disk_line())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="macleaner", description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog=__doc__.split("\n", 1)[1])
    parser.add_argument("--version", action="version", version=f"macleaner {__version__}")
    sub = parser.add_subparsers(dest="command")
    scan_p = sub.add_parser("scan", help="show what can be reclaimed, change nothing")
    scan_p.add_argument("--keys", action="store_true", help="show each item's key (for --only and exclude)")
    scan_p.add_argument("--json", action="store_true", help="print the results as JSON")
    clean = sub.add_parser("clean", help="scan, then clean what you pick (the default)")
    clean.add_argument("--safe", action="store_true", help="select every item marked safe")
    clean.add_argument("--only", metavar="KEYS", help="comma-separated item keys/globs, e.g. npm,docker-*")
    clean.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    clean.add_argument("-n", "--dry-run", action="store_true", help="show what would be deleted, delete nothing")
    clean.add_argument("-v", "--verbose", action="store_true", help="print every folder that gets deleted")
    for p in (scan_p, clean):
        p.add_argument("-p", "--projects", action="store_true", help="list project build folders one by one")
    sub.add_parser("big", help="find big files and old downloads to move to the Trash")
    sub.add_parser("history", help="show past cleanups")
    sub.add_parser("config", help="create/show the config file")
    sub.add_parser("status", help="just show free space")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if sys.platform != "darwin":
        die("macleaner only runs on macOS.")
    if os.geteuid() == 0:
        die("Don't run macleaner with sudo. It asks for your password itself when it needs it.")
    if not args.command:
        args.command = "clean"
        args.safe = args.only = args.yes = args.dry_run = args.verbose = args.projects = False
    cfg = load_config()
    commands = {"scan": cmd_scan, "clean": cmd_clean, "big": cmd_big, "history": cmd_history,
                "config": cmd_config, "status": cmd_status}
    try:
        sys.exit(commands[args.command](args, cfg))
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(130)


if __name__ == "__main__":
    main()
