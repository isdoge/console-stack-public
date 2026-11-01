# console-stack (public kernel)

Self-hosted operations console kernel: machines, monitoring ingest, plugin runtime, and a React UI.

This repository is the **public composition** of [console-stack](https://github.com/isdoge/console-stack) (private). It contains the `kernel/` source, an empty public plugin composition (optional `audit` sample), synthetic `instance/public` config, and a Docker `public` image target.

## What is included

- `kernel/` — backend, frontend, Go agent
- `plugins/` — contract + empty `composition.json` + optional `audit/` sample
- `instance/public/` — synthetic empty ledger / composition for public profile
- `console/Dockerfile` — default stage `public` (kernel only; no private plugins)
- `scripts/` — generic build / migrate / backup helpers safe for public use

## What is not included

- Private plugins (ledger, feed, renewals, panels, backup_evidence, personal_workbench)
- Private backend bridges and production YAML / secrets / agent identity
- Private Git history from the operator’s monorepo

## Quick start (Docker)

```bash
# from this repository root
DOCKER_BUILDKIT=1 docker build -f console/Dockerfile --target public -t console-stack-public:local .
```

Published image (first public release):

```text
ghcr.io/isdoge/console-stack-public:<tag>
```

Compose defaults bind the web UI to loopback. Point Postgres and roles via `.env` (see `.env.example`). Public profile uses an empty machine ledger unless you supply config.

## License / provenance

- Project license: [LICENSE](./LICENSE) (MIT)
- Third-party notices: [NOTICE](./NOTICE) (npm production deps, public Python runtime, gopsutil, referenced UI upstreams)
- Public `requirements.txt` excludes private plugin parsers (feedparser/openpyxl/pdfplumber)
- Remaining gaps (full lockfile closure inlining, favicon provenance, native wheel internals) are listed at the end of NOTICE

## Source identity

See `.b7-source-sha` for the private-monorepo commit this tree was exported from.
