# Contributing

Thanks for helping. macleaner is a single file, `macleaner.py`, with no dependencies, and it must keep running on the Python 3.9 that ships with macOS.

## Develop

```sh
python3 -m unittest discover -s tests -v   # tests
uvx ruff check .                            # lint
python3 macleaner.py scan --keys            # try it (read-only)
python3 macleaner.py clean --safe --dry-run # see what would be deleted
```

## Adding something to clean

Add a `Target` in `discover_tools()`. Before you do, check that:

1. **It's really disposable.** The owning tool rebuilds or re-downloads it on its own. If a person might want to keep it, mark it `REVIEW`, not `SAFE`.
2. **The official command is used when there is one.** For example, `brew cleanup` or `pnpm store prune` (`mode="cmd"`).
3. **It isn't deleted while in use.** Pass `busy=` (see `app_running`, `proc_running`, `GRADLE_BUSY`) for anything a running app or build holds open.
4. **It lives where `guard()` allows**: at least two levels inside your home folder, or inside a configured project root. Don't widen `guard()` or shrink `protected()` without discussing it in an issue first.
5. **Tests cover it** when it adds new logic.

Write user-facing text in plain words. Say what happens and how to get the thing back.
