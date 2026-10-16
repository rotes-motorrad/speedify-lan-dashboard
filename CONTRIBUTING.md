# Contributing

Start with a small, reproducible issue or focused change. The monitor should remain lightweight and read-only.

For a bug report, include:

- Dashboard version, Linux distribution, Python version, and Speedify version.
- Whether the issue involves a live Speedify feed or explicit demo mode.
- The selected chart window and a short description of expected versus observed behavior.
- Relevant dashboard-service error lines, after removing personal information.

If a CLI format mismatch is suspected, capture a few seconds with:

```bash
/usr/share/speedify/speedify_cli -s stats 3
```

Before posting output, remove public/private IPs, adapter/network names that identify you, and any account details. Never post credentials, tokens, cookies, or `show user` output. Prefer a synthetic minimal sample with the same field structure.

Run these checks before proposing a change:

```bash
python3 -m unittest -v test_dashboard.py
node test_frontend.cjs
bash -n install.sh
```

Use explicit demo mode for illustrative screenshots, so examples are not confused with real measurements or personal network details. Confirm layout changes on both desktop and a narrow phone viewport.

Keep runtime dependencies in Python's standard library and browser-native code unless a new dependency has a clear, documented benefit. Preserve missing-data gaps, accurate units, bounded history, and the distinction between synthetic aggregate traffic and physical connection traffic.
