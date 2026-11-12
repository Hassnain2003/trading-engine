# Security Policy — trading-engine

## ⚠️ Never commit keys

API keys, secrets, ntfy topics, Discord webhooks, SMTP credentials, and any
personal data must live in `.env` only. `.env` is ignored by `.gitignore` and
must never be committed. Use `.env.example` as the template and replace values
with placeholders before sharing anything.

## Reporting a vulnerability

If you find a secret accidentally committed, a hardcoded credential, or any
other security issue in this repository:

1. Do **not** open a public issue.
2. Remove or rotate the exposed credential immediately (assume it is
   compromised).
3. Contact the repository owner through GitHub's private vulnerability
   reporting mechanism, or open a private advisory.

## What we scan

- `gitleaks` runs on every commit (pre-commit) and on every push/PR (CI).
- `pip-audit` runs in CI against pinned dependencies.
- A pre-commit hook blocks committing `*.db`, `data/*.csv`, and `.env*` files.

## Mainnet interlock

The engine refuses to start on Bybit mainnet unless BOTH
`ALLOW_MAINNET=true` and `I_UNDERSTAND_THE_RISK=yes` are set. Demo/sandbox is
the default and requires no extra flags.
