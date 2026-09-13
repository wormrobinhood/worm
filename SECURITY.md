# Security

Report suspected vulnerabilities privately through [GitHub private vulnerability reporting](https://github.com/wormrobinhood/worm/security/advisories/new). Include the affected commit, impact, and a reproduction using disposable data. Do not put credentials, wallet keys, signed transactions, personal information, or production database contents in public issues.

Only the current main branch is maintained. Reports are reviewed by the project maintainers; there is no guaranteed response time or bug bounty.

Local Git hooks and CI check for unsafe files and secret patterns. Publication also requires the dedicated WORM identity and a scan of the exact outgoing archive. Never bypass a failed check. These controls supplement review and cannot guarantee that no sensitive information is present.

Runtime secrets belong in private deployment variables. Backups, rehearsal files, the private transaction outbox, and local privacy deny lists must never be committed or uploaded with a build. Live execution requires separate operator authorization; a code deployment does not authorize token launching or payments.
