# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-10-28

First public release.

### Added

- Sniping loop for MRKT: rules by collection, model, backdrop and symbol, polling the cheapest and newest listings every 2 s, deduplicated by listing id and price.
- Honest price math (`pricing.py`): a cluster floor with `high`/`med`/`low` confidence (a floor without a cluster of at least 3 listings is always `low`), a fee model (the MRKT sell fee is learned per collection from `salePriceWithoutFee`), the all-in cost, the net exit at the floor, the break-even price and match reason codes.
- Trap and lock filter: lucky-buy tickets and unknown price units are blocked, premarket gifts are skipped unless a rule allows the lock, and prices under `TOO_GOOD_PCT` of the floor always ask first.
- Deal cards in Telegram HTML with per-rule `auto`, `confirm` and `watch` modes, Buy / Skip / Mute 24h / Why? buttons, a price re-check before a tapped buy, a 90 s expiry and near-miss cards with a one-tap "Raise max".
- Second opinion (`peers.py`, `fx.py`): Telegram's own gift valuation, floor snapshot files from other markets, optional Portals public floors and a keyless TON to USD rate. Adds `/peers`, `/compare` and `floors --json`.
- Practice ledger (`ledger.py`): positions marked net of fees, `/pnl`, `/score`, `/scoreboard`, `/sold` and `/export`.
- `/hunt` wizard (`wizard.py`): a rule in at most 6 taps, `t.me/nft` link support and a "would have caught" preview of the last 24 h.
- `/doctor` in the bot and as a CLI command (exit code 1 on any failure).
- `/start` onboarding: what the bot does, its mode and limits, and three commands to try first (`/hunt`, `/scoreboard`, `/doctor`). `/help` lists every command.
- Explained decisions: a `decisions` table, `/why`, a JSON-lines event log, an optional daily digest with tuning hints, `/mute` and `/unmute`, and an optional "Explain more" button (needs `ANTHROPIC_API_KEY`).
- Shadow rules with a hindsight card, plus `/promote`.
- `mock` command that runs the whole pipeline offline with fixtures, with `--tap`, `--crash`, `--panic`, `--preview`, `--doctor` and `--shadow-hours`. It prints to stdout and never contacts Telegram, even with `BOT_TOKEN` set.
- Windows install (README: Self-hosting), a CI workflow (Python 3.11 and 3.12) and 239 offline tests.

### Safety

- `DRY_RUN=true` in `.env` is a hard practice lock that no bot command or stored setting can lift.
- Live mode needs `DRY_RUN=false` and `/dry off`. It shows the practice report first, uses a one-time go-live button that expires after 10 minutes and only appears when every rule is `ready`, and switches back to practice after `LIVE_ARM_HOURS`. Setting `DRY_RUN=true` cancels an open live window for good, so lifting the lock again still needs a new `/dry off`.
- Practice unless live is armed: without an unexpired live window the bot is in practice, so a fresh database with `DRY_RUN=false` (including an `Engine` built directly) starts in practice.
- Going live expires every pending PRACTICE card, and a tapped card keeps its own practice flag, so a card sent in practice can never spend real TON.
- Every buy, automatic or tapped, practice or live, goes through one locked spend step that re-reads every switch and checks `MAX_PRICE_TON`, `DAILY_BUDGET_TON` and `MAX_BUYS_PER_HOUR` against fresh totals. Live buys re-check the switches and the stored rule (enabled, paused, mode, max and filters) once more right before `POST /gifts/buy`.
- A live buy is recorded as `unknown` before the request goes out. Any result other than an explicit success (timeout, network error, 5xx, an unexpected or empty response) stays `unknown` and counts toward the limits.
- On shutdown no new buy starts, and a buy in flight gets its answer and its purchase row first.
- Card expiry, the live-window check and the breakers keep running while cycles fail.
- A missing or unknown price unit blocks the listing.
- `/panic` pauses, forces practice, voids the go-live button and expires pending cards. `/resume` needs a one-time confirm button that expires after 10 minutes; any later `/pause`, `/panic` or breaker voids it, so an old button can never undo a newer stop.
- Breakers: floor crash per rule, a losing streak, a balance mismatch and repeated failed buys.
- `/safety`, plus `/preset` limits that never go above `.env`.
- `nan` and `inf` are refused for every number in `.env`, so a limit can never be switched off by accident.
- Near-miss buttons stop working after 24 h, and "Raise max" never lowers a max you raised since the card was sent.
- New rules, from `/addrule`, `/hunt` or `rules.json`, start in shadow (practice only) until promoted. `rules.json` rejects a max of 0 or below and non-finite numbers.
- Secrets are truncated in logs, the session file is set to mode 600, and `/doctor` checks its permissions and 2FA.

### Known limitations

- The live paths have not been exercised against MRKT with a real account. Everything marked `TODO(verify)` in the code is listed in the README under "Before going live": the buy response, fee schedule, balance field, Mini App short name, token expiry, newest-first ordering, listed-at, lucky-buy and premarket fields, price units, single-gift re-check, rarity fields, Portals fees, Telegram's "same type" definition, gift slugs, collection names, the rate limit and the per-model floor field.
- Fee defaults are assumptions until you set `FEES_VERIFIED=true`.
- A buy already sent to MRKT cannot be recalled by `/panic`.
- Live buys run one at a time because of the spend lock.
- Practice assumes you won every race for the listing.
- File rules reset their `mode` and `max` from `rules.json` on each start; their stored `state` is kept unless the file sets one. An emptied or deleted `rules.json` leaves the previously loaded rules active.
- The first card of a `once` run may lack the USD amount, because the TON rate loads in the background.
