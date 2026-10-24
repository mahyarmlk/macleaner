from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import macleaner as m  # noqa: E402

OLD = time.time() - 400 * 86400


def cfg_for(*roots: Path, **overrides) -> dict:
    cfg = dict(m.DEFAULT_CONFIG, project_roots=list(roots))
    cfg.update(overrides)
    return cfg


def touch(path: Path, text: str = "x", mtime: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if mtime:
        os.utime(path, (mtime, mtime))
    return path


class TempHome(unittest.TestCase):
    """Runs each test with HOME pointing at a fresh temporary folder."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(os.path.realpath(self._tmp.name))
        self.home = self.root / "home"
        self.home.mkdir()
        self.outside = self.root / "outside"
        self.outside.mkdir()
        patcher = mock.patch.object(m, "HOME", self.home)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)
        self.dev = self.home / "Projects"
        self.dev.mkdir()
        self.cfg = cfg_for(self.dev)


class TestGuard(TempHome):
    def test_refuses_top_level_and_protected_folders(self):
        for p in (Path("/"), Path("/etc/hosts"), self.home, self.home / "Library", self.home / "Documents",
                  self.home / "Library/Application Support", self.home / ".npm", self.dev, self.outside / "x"):
            with self.subTest(p=p), self.assertRaises(m.UnsafePath):
                m.guard(p, self.cfg)

    def test_refuses_relative_paths(self):
        with self.assertRaises(m.UnsafePath):
            m.guard(Path(".npm/_cacache"), self.cfg)

    def test_allows_cache_folders_inside_home(self):
        target = self.home / ".npm/_cacache"
        self.assertEqual(m.guard(target, self.cfg), target)
        self.assertEqual(m.guard(self.dev / "app/node_modules", self.cfg), self.dev / "app/node_modules")

    def test_refuses_paths_reached_through_a_symlink_that_leaves_home(self):
        (self.home / ".npm").mkdir()
        (self.home / ".npm/_cacache").symlink_to(self.outside)
        with self.assertRaises(m.UnsafePath):
            m.guard(self.home / ".npm/_cacache/content", self.cfg)


class TestRemove(TempHome):
    def test_removes_read_only_trees(self):
        tree = self.home / "go/pkg/mod"
        f = touch(tree / "a/b/file")
        f.chmod(0o444)
        (tree / "a/b").chmod(0o555)
        self.assertEqual(m.remove_path(tree / "a", self.cfg), 0)
        self.assertFalse((tree / "a").exists())

    def test_never_follows_symlinks(self):
        precious = touch(self.outside / "precious.txt")
        cache = self.home / ".cache/thing"
        touch(cache / "junk")
        (cache / "link").symlink_to(self.outside)
        m.remove_path(cache, self.cfg)
        self.assertFalse(cache.exists())
        self.assertTrue(precious.exists())

    def test_removes_only_the_link_when_the_victim_is_a_symlink(self):
        precious = touch(self.outside / "precious.txt")
        link = self.home / ".cache/link"
        link.parent.mkdir(parents=True)
        link.symlink_to(self.outside)
        m.remove_path(link, self.cfg)
        self.assertFalse(link.is_symlink())
        self.assertTrue(precious.exists())

    def test_contents_mode_keeps_the_folder(self):
        cache = self.home / ".npm/_cacache"
        touch(cache / "a/1")
        touch(cache / "b")
        t = m.Target("npm", "npm", "g", m.SAFE, [cache])
        self.assertIsNone(m.clean_target(t, self.cfg, dry_run=False))
        self.assertTrue(cache.is_dir())
        self.assertEqual(list(cache.iterdir()), [])

    def test_contents_mode_refuses_a_symlinked_folder(self):
        precious = touch(self.outside / "precious.txt")
        (self.home / ".npm").mkdir()
        (self.home / ".npm/_cacache").symlink_to(self.outside)
        t = m.Target("npm", "npm", "g", m.SAFE, [self.home / ".npm/_cacache"])
        self.assertIn("symlink", m.clean_target(t, self.cfg, dry_run=False))
        self.assertTrue(precious.exists())

    def test_dry_run_deletes_nothing(self):
        f = touch(self.home / ".npm/_cacache/a")
        t = m.Target("npm", "npm", "g", m.SAFE, [self.home / ".npm/_cacache"])
        with mock.patch("builtins.print"):
            m.clean_target(t, self.cfg, dry_run=True)
        self.assertTrue(f.exists())

    def test_sudo_delete_blocked_by_macos_says_so(self):
        cache = self.home / ".cache/sys/dyld"
        cache.mkdir(parents=True)
        t = m.Target("sys", "sys", "g", m.SAFE, [cache], mode="remove", sudo=True)
        blocked = subprocess.CompletedProcess([], 1, stderr="rm: dyld: Operation not permitted\n")
        wrong_password = subprocess.CompletedProcess([], 1, stderr="sudo: 3 incorrect password attempts\n")
        with mock.patch("builtins.print"), mock.patch.object(m.subprocess, "run", return_value=blocked):
            self.assertIn("Full Disk Access", m.clean_target(t, self.cfg, dry_run=False))
        with mock.patch("builtins.print"), mock.patch.object(m.subprocess, "run", return_value=wrong_password):
            self.assertEqual(m.clean_target(t, self.cfg, dry_run=False), "couldn't get admin rights")

    def test_an_unsafe_path_stops_the_whole_target(self):
        keep = touch(self.home / ".cache/ok/file")
        t = m.Target("x", "x", "g", m.SAFE, [self.home / ".cache/ok", self.home / "Documents"], mode="remove")
        (self.home / "Documents").mkdir()
        with self.assertRaises(m.UnsafePath):
            m.clean_target(t, self.cfg, dry_run=False)
        self.assertTrue(keep.exists())


class TestProjects(TempHome):
    def make_project(self, name: str, marker: str, artifact: str, mtime: float | None = None) -> Path:
        proj = self.dev / name
        touch(proj / marker, mtime=mtime)
        touch(proj / artifact / "junk.bin", "y" * 100)
        if mtime:
            os.utime(proj / artifact, (mtime, mtime))
        return proj

    def test_finds_build_folders_next_to_their_marker_file(self):
        self.make_project("old-web", "package.json", "node_modules", mtime=OLD)
        self.make_project("new-rust", "Cargo.toml", "target")
        self.make_project("not-gradle", "README.md", "build")  # no build.gradle, so not ours
        targets = {t.key: t for t in m.discover_projects(self.cfg)}
        self.assertEqual(set(targets), {"project:~/Projects/old-web", "project:~/Projects/new-rust"})
        self.assertEqual(targets["project:~/Projects/old-web"].safety, m.SAFE)
        self.assertEqual(targets["project:~/Projects/new-rust"].safety, m.REVIEW)

    def test_skips_folders_committed_to_git(self):
        if not m.find_git():
            self.skipTest("git not available")
        proj = self.make_project("app", "build.gradle", "build")
        touch(proj / ".gradle/state")
        git = ["git", "-C", str(proj), "-c", "user.name=t", "-c", "user.email=t@example.com"]
        subprocess.run([*git, "init", "-q"], check=True)
        subprocess.run([*git, "add", "build.gradle", "build"], check=True)
        subprocess.run([*git, "commit", "-qm", "init"], check=True)
        (t,) = m.discover_projects(self.cfg)
        self.assertEqual([p.name for p in t.paths], [".gradle"])

    def test_does_not_descend_into_build_folders_or_symlinks(self):
        self.make_project("web", "package.json", "node_modules")
        touch(self.dev / "web/node_modules/dep/package.json")
        touch(self.dev / "web/node_modules/dep/node_modules/x")
        (self.dev / "loop").symlink_to(self.dev)
        (t,) = m.discover_projects(self.cfg)
        self.assertEqual(t.paths, [self.dev / "web/node_modules"])

    def test_grouping_merges_projects_by_safety(self):
        a = m.Target("project:~/Projects/a", "a", "g", m.SAFE, [Path("/a")], path_sizes={Path("/a"): 10})
        b = m.Target("project:~/Projects/b", "b", "g", m.SAFE, [Path("/b")], path_sizes={Path("/b"): 20})
        c = m.Target("project:~/Projects/c", "c", "g", m.REVIEW, [Path("/c")], path_sizes={Path("/c"): 5})
        other = m.Target("npm", "npm", "g", m.SAFE)
        grouped = {t.key: t for t in m.group_projects([a, b, c, other], self.cfg)}
        self.assertEqual(set(grouped), {"npm", "projects-stale", "projects-active"})
        self.assertEqual(grouped["projects-stale"].total(), 30)
        self.assertIn("b 20 B", grouped["projects-stale"].note)


class TestBusy(TempHome):
    def test_app_running_matches_the_bundle_executable(self):
        procs = ["/Applications/Editor.app/Contents/MacOS/Editor --flag", "/usr/sbin/cfprefsd agent"]
        self.assertEqual(m.app_running("Editor")(procs), "Editor is running")
        self.assertIsNone(m.app_running("Xcode")(procs))

    def test_singleton_lock(self):
        app = self.home / "Library/Application Support/App"
        app.mkdir(parents=True)
        (app / "SingletonLock").symlink_to(f"host-{os.getpid()}")
        self.assertTrue(m.singleton_alive(app))
        (app / "SingletonLock").unlink()
        (app / "SingletonLock").symlink_to("host-99999999")
        self.assertFalse(m.singleton_alive(app))

    def test_busy_targets_are_skipped_when_cleaning(self):
        f = touch(self.home / ".gradle/caches/x")
        t = m.Target("gradle-caches", "Gradle", "g", m.SAFE, [self.home / ".gradle/caches"],
                     busy=lambda procs: "Gradle is running")
        with mock.patch.object(m, "HISTORY_PATH", self.home / "history.jsonl"), mock.patch("builtins.print"):
            m.do_clean([t], self.cfg, dry_run=False)
        self.assertTrue(f.exists())


class TestHelpers(unittest.TestCase):
    def targets(self):
        return [m.Target(str(i), str(i), "g", m.SAFE if i < 3 else m.REVIEW) for i in range(6)]

    def test_parse_selection(self):
        ts = self.targets()
        keys = lambda text: [t.key for t in m.parse_selection(text, ts)]  # noqa: E731
        self.assertEqual(keys("s"), ["0", "1", "2"])
        self.assertEqual(keys("s,!2"), ["0", "2"])
        self.assertEqual(keys("1, 4-5"), ["0", "3", "4"])
        self.assertEqual(keys("a !1"), ["1", "2", "3", "4", "5"])
        for bad in ("7", "0", "3-9", "nope"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                m.parse_selection(bad, ts)

    def test_reclaimable_counts_nested_paths_once(self):
        outer = m.Target("avd", "avd", "g", m.REVIEW, path_sizes={Path("/x/avd"): 100})
        inner = m.Target("snap", "snap", "g", m.SAFE, path_sizes={Path("/x/avd/snapshots"): 40})
        docker = m.Target("docker", "d", "g", m.SAFE, size=7)
        self.assertEqual(m.reclaimable([outer, inner, docker]), 107)
        self.assertEqual(m.reclaimable([inner]), 40)

    def test_parse_docker_size(self):
        self.assertEqual(m.parse_docker_size("1.927GB (35%)"), 1_927_000_000)
        self.assertEqual(m.parse_docker_size("24.95MB"), 24_950_000)
        self.assertEqual(m.parse_docker_size("512kB"), 512_000)
        self.assertEqual(m.parse_docker_size("0B"), 0)
        self.assertEqual(m.parse_docker_size(""), 0)

    def test_human(self):
        self.assertEqual(m.human(None), "?")
        self.assertEqual(m.human(999), "999 B")
        self.assertEqual(m.human(21_200_000_000), "21.2 GB")

    def test_json_output(self):
        t = m.Target("npm", "npm cache", "g", m.SAFE, [Path("/x")], path_sizes={Path("/x"): 5})
        (row,) = json.loads(m.targets_json([t]))
        self.assertEqual(row["key"], "npm")
        self.assertEqual(row["bytes"], 5)


class TestConfig(unittest.TestCase):
    def load(self, content: str) -> dict:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write(content)
        self.addCleanup(os.unlink, f.name)
        with mock.patch.object(m, "CONFIG_PATH", Path(f.name)), mock.patch("sys.stderr"):
            return m.load_config()

    def test_defaults_merge(self):
        cfg = self.load('{"stale_days": 7, "exclude": ["avd:*"]}')
        self.assertEqual(cfg["stale_days"], 7)
        self.assertEqual(cfg["exclude"], ["avd:*"])
        self.assertEqual(cfg["min_size_mb"], m.DEFAULT_CONFIG["min_size_mb"])

    def test_broken_config_stops_the_run(self):
        for content in ("{not json", "[]", '{"exclude": "avd:*"}', '{"stale_days": -1}', '{"stale_days": true}'):
            with self.subTest(content=content), self.assertRaises(SystemExit):
                self.load(content)


class TestCli(unittest.TestCase):
    def test_version(self):
        with self.assertRaises(SystemExit) as cm, mock.patch("sys.stdout"):
            m.main(["--version"])
        self.assertEqual(cm.exception.code, 0)

    def test_refuses_to_run_as_root(self):
        with mock.patch("os.geteuid", return_value=0), mock.patch("sys.stderr"), \
                self.assertRaises(SystemExit) as cm:
            m.main(["status"])
        self.assertEqual(cm.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
