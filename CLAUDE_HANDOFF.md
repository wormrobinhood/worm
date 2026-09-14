# WORM handoff: security recovery and scheduled launch

Updated 2026-09-14. Source of truth: the main WORM repository (not a separate design copy).

**Release scope:** publishing safeguards, payment recovery, private browser isolation, and a configurable launch countdown. A code release does not authorize a token launch or live payments. Runtime credentials and launch scheduling remain private operator configuration.

Base commit reviewed: `ede309734385e9bddc29c663199ef2484422076d`.

## Why these changes were made

This work preserves launch, claim, creator payout, burn, gold, and inference payment paths while hardening interrupted or ambiguous outcomes. Existing claims retain their recorded allocations; historical policies must be reviewed before migrating older rows without saved splits.

Paper trading remains available. The website countdown can trigger launch checks at an explicitly configured deadline. No deadline is supplied by this release, and the scheduler cannot arm live execution itself.

## Changes and rationale

- `scripts/leakcheck.py`: Git/read failures and malformed push inputs block publication. Every historical path/blob pair is checked, including files renamed out of private directories. CI scans all reachable history. Symlinks, submodules, rehearsal files, and SQLite sidecars require review or are rejected.
- `scripts/publish-check.py`, `scripts/deploy.sh`: verify the effective WORM author/committer identity, dedicated SSH authentication, complete commit history, and the exact outgoing archive before a manual Railway upload. The upload targets an explicit project, service, and environment. Recursive build/upload exclusions protect private state.
- `.github/workflows/checks.yml`, `.github/dependabot.yml`, `SECURITY.md`: offline regression checks, full-history leak scanning, container builds, dependency updates, and a private reporting channel. Actions use immutable commit references and read-only repository permissions.

- `wormhole/outbox.py`, `tx.py`: persist private signed transaction material before submission; callers save pending bookkeeping before the network hears the transaction. Unknown transactions block new sends and recover using identical signed bytes. A interrupted `preparing` entry requires operator review rather than blind resubmission. Add conservative ETH fee/value caps and a `payments.paused` file check.
- `treasury.py`, `db.py`: fail closed on accounting errors; reserve pending payouts as well as outstanding allocations; keep successful-but-incomplete receipt evidence pending; stop calling node absence a proven failure. Snapshot split percentages on new claims. Legacy claims require explicit migration inputs. Use FULL SQLite synchronous durability and atomic grouped writes.
- `launch.py`: save token identity and pending clearance atomically; missing launch event is unresolved, not a revert.
- `server.py`: every launch-control request now requires `WH_OPS_TOKEN`, including localhost. Public status exposes no control credential or signed transaction material.
- Public compute errors and top-up events use fixed messages, so provider/wallet exception details cannot reach API responses or the live feed. This fixes the CodeQL exception-exposure finding. The page-origin test now compares parsed HTTPS authorities exactly instead of accepting lookalike domain prefixes.
- `compute.py`: reject a provider minimum above the approved top-up, cap dollars and authorization lifetime, pin Venice recipient, save authorization details before sending, and block new compute top-ups while any compute payment remains unknown. Old pending authorizations do not age into permission for another payment. Unknown Venice settlement deliberately requires evidence/operator review.
- `screen.py`, `Dockerfile.screen`, `scripts/browser-service.py`: LIVE mode uses an isolated CDP browser service; no local unsandboxed browser beside the signer. Non-live local rendering uses Chromium's sandbox. Live screen is disabled if the separate service URL is missing. Infrastructure setup is still required—see below.
- `requirements.lock`, `requirements-build.lock`: hash-pinned tested application dependencies and updated build tools. An OSV check of 54 installed packages returned advisories for local pip 24.0 and setuptools 79.0.1; replacement build versions pip 26.2.1 and setuptools 84.0.0 returned no advisories. The existing local venv was not upgraded. The final Linux image/Chromium still needs scanning.
- `scripts/backup-state.py`: stopped-service backup of BOTH databases, SQLite integrity verification, private files, and a pause marker on restore. Includes a disposable-data backup test.
- `wormhole/launch_schedule.py`, `run.py`: durable timestamp and state; a dedicated server worker checks once per second, independently of the five-minute treasury loop. No open website is needed. Existing token/pending state prevents a second scheduled launch. Future schedule survives restarts; if downtime spans the deadline, the worker acts after restart once checks pass. Failed/interrupted launches require review rather than repeated attempts.
- `web/index.html`, `design.css`, `design.js`: compact responsive countdown on Live above the stats, with local-time date and server-aligned seconds. Public states include unannounced, countdown, due, started, pending confirmation, failed/review, and launched with token link. The browser NEVER calls the launch trigger at zero. Stale connectivity is shown rather than declaring success.

## Set the launch time

No time is configured by this patch. Choose an explicit timezone and verify the intended wallet/token parameters first.

Option 1: set `WH_LAUNCH_AT` to a future ISO 8601 timestamp (with `Z` or an explicit offset) before starting WORM. It initializes the schedule only if no schedule exists in the database. Later environment edits do not overwrite a persisted schedule or silently rearm a cancelled/failed launch.

Option 2: authenticated `POST /api/launch/schedule`, header `X-Ops-Token: <private WH_OPS_TOKEN>`, JSON `{"at":"<future ISO 8601 timestamp with timezone>"}`. Send `{"at":null}` to cancel before launch starts. Do not put the credential in browser code or publish it. Public `GET /api/launch/status` reads state. Existing authenticated `POST /api/launch` remains an explicit immediate launch request.

At the deadline WORM starts launch checks within the worker's next iteration when the service is running and free to proceed. It requires LIVE enabled, a signer, no pause marker, verified accounting, no unresolved payments, and no existing token. Confirmation time depends on the chain; zero on the countdown does not promise an already-mined token. The scheduler does not enable LIVE or trading itself.

## Required before deployment

Read `docs/PAYMENT-RECOVERY.md` fully. In particular:

1. Back up and verify restore of BOTH `wormhole.db` and `transactions.sqlite3`; preserve pending operation associations. No real restore drill was performed.
2. Review historical claim allocations before supplying `WH_LEGACY_OWNER_SHARE`, `WH_LEGACY_BURN_SHARE`, `WH_LEGACY_GOLD_SHARE`. No historical data was changed here. Mixed-policy history requires row-level review, not a guessed global percentage.
3. Configure private `WH_OPS_TOKEN`. Configure verified `WH_VENICE_PAY_TO` only if using live Venice payments.
4. Build and test the isolated browser service; use `WH_SCREEN_CDP_URL=http://<private-browser-service>:9223`. The client handles Chromium's localhost discovery and retains the configured private destination. The relay accepts IPv4 and IPv6. Do not publish CDP. Give this service no secrets or shared data mounts and apply the platform's available resource/network controls. The application image contains no Chromium installation; container rendering always requires the separate service.
5. Verify final host flags/wallet/token metadata and persistent volume. Local rehearsal flags and public-host flags differ; code sync is not configuration sync. Operate only one signer instance per wallet.
6. Review the default fee/value and compute caps against the approved budget. Do not silently relax them to make a launch pass.
7. Run an explicitly authorized final-policy rehearsal if desired. Keep real trading disabled: the sell path remains deliberately unfinished; this task did not implement trading.

## Validation

- The offline suite covers the publishing guard regressions and private CDP discovery in addition to the recovery and scheduler cases below. Check the release's CI result for the final test count and Linux image build result.
- Tests include broadcast/lookup interruption, identical-byte recovery, persistence failure before send, atomic launch rollback, pending reservations, immutable split, legacy migration gate, old Venice pending blockade, recipient/amount bounds, localhost authentication, actual runner launch recovery, stopped-service backup, timezone validation, due-time execution without a browser, restart persistence, and no automatic repeat after failure.
- `pip install --dry-run --require-hashes -r requirements.lock` succeeds against the installed tested environment; this is not a clean Linux image build.
- No real payment or launch was used for validation.

Do not discard changed assertions as regressions: several old tests explicitly expected callback-after-broadcast, write-off on node absence, or provider-minimum overspending. Their expectations were updated to the safer semantics and new fault-injection tests added.

## Local engine evidence update — 2026-09-14

Phase A implementation is prepared for the authorized release. See [ENGINE-EVIDENCE.md](docs/ENGINE-EVIDENCE.md) for changes, migration behavior, regression coverage and remaining limitations. Prices must be fresh; assessments are immutable; outcome/lab resolution is atomic; scans use durable jobs; readiness requires a verified cohort and measured skill. Historical data stays intact and is not automatically repaired or certified. No wallet, live flag, token schedule or deployment setting was changed.

Validation: 445 offline tests passed (30 new audit and shadow regression cases), with one pre-existing Starlette/AnyIO deprecation warning. `git diff --check` passed. No production-data repair or live execution was performed.

## Publication follow-up: operator-controlled trading and prospective rules

The user authorized publishing the evidence update and explicitly requires trading to stay off until they enable it. Production must have `WH_TRADING=0`; do not change `WH_LIVE`, keys, launch schedule, or `LIVE_SELL_READY`. Readiness and AI cannot turn trading on.

New AI scoring rules now enter a fixed future-cohort evaluation after the historical screen. They do not affect scores until that evaluation passes. See `wormhole/shadow.py`: 80 later launches, one per creator, discovery creators excluded, outcome-blind cohort selection, minimum samples on both sides, lifetime alpha spending and one evaluation only. Unknown results do not become successes. Existing active rules remain stored/in use; this does not retroactively certify them. Exit arms remain separate strategy-lab experiments, not prospectively certified by this rule workflow.
