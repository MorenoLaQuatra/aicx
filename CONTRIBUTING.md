# Contributing

Contributions are welcome. Keep provider credentials out of fixtures, logs, and
bug reports.

Before submitting a change, run:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m compileall -q src
```

Tests that exercise provider CLIs must create an isolated temporary `AICX_HOME`.
Never point destructive or login tests at a contributor's default `~/.codex` or
`~/.claude` directory.

The project intentionally has no runtime dependencies. Discuss additions that
affect credentials, provider processes, or the package footprint before making
them mandatory.
