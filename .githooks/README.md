# WORM publication hooks

These hooks check WORM changes for secrets and personal details before they leave
the working copy. Enable them after cloning:

```sh
git config core.hooksPath .githooks
```

- `pre-commit` checks staged files.
- `commit-msg` checks the proposed commit message.
- `pre-push` checks outgoing commits, commit identities, and the push destination.

All three call `scripts/leakcheck.py`. Before publishing, also run:

```sh
python3 scripts/leakcheck.py --all
python3 scripts/publish-check.py
```

The publication preflight verifies the dedicated WORM publishing identity and
destination, reachable commit history, and the outgoing source archive. These
checks supplement manual review; passing them does not prove that every possible
secret or security defect has been detected.

See [Keeping secrets out of git](../README.md#keeping-secrets-out-of-git) and
[the security policy](../SECURITY.md) for the repository's guidance.
