# Contributing

Thanks for helping. Bug reports, fixes and verified API details are all welcome, especially for anything marked `TODO(verify)`.

## Set up and test

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest -q
.venv/bin/python -m mrkt_autobuy mock --cycles 2
```

Both must pass before you open a pull request. Tests and `mock` run offline and need no keys. If you add a feature, add offline tests for its logic, and fixtures so the `mock` run shows it.

## The safety rule

Never weaken a safety default. In particular:

- `DRY_RUN=true` stays the default and stays a hard lock: no bot command or stored setting may lift it.
- Every spend path goes through the one locked spend step and checks `MAX_PRICE_TON`, `DAILY_BUDGET_TON` and `MAX_BUYS_PER_HOUR`.
- `/panic` must keep stopping everything.
- Secrets (tokens, `initData`, session files, phone numbers) are never logged in full.

Pull requests that relax these will not be merged.

## Code style

- Python 3.11+, 4-space indent, type hints, `from __future__ import annotations`.
- Keep pricing, matching and the ledger pure: no I/O, and the clock is passed in.
- API-specific code lives in `client.py`. Mark any guess about the MRKT API with `TODO(verify)`.
- Everything is in English: code, comments, bot strings, CLI output and docs.
- Keep lines under about 120 characters and match the style of the surrounding code.
