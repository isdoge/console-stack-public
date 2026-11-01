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

## Install / upgrade

- Install (Docker Compose, PostgreSQL 16, single host): [docs/install.md](docs/install.md)
- Upgrade, offline upgrade, backup and rollback: [docs/upgrade.md](docs/upgrade.md)

`docker-compose.yml` builds the `public` target by default (kernel only, empty plugin composition, empty ledger). Set `CONSOLE_IMAGE` in `.env` to use a published image instead of building.

Published images (`ghcr.io/isdoge/console-stack-public`):

| Tag | Meaning |
| --- | --- |
| `sha-<public commit>` | immutable per public commit (use this in `.env`) |
| `src-<private source commit>` | the private monorepo commit the tree was exported from |
| `latest` | moving pointer to the newest `main` build |

## License / provenance

- Project license: [LICENSE](./LICENSE) (MIT)
- Full dependency-closure license texts: [kernel/THIRD_PARTY_LICENSES.txt](./kernel/THIRD_PARTY_LICENSES.txt) (generated; GAPS listed at the end)
- The image ships LICENSE / NOTICE / THIRD_PARTY_LICENSES.txt under `/app` and serves them at `/LICENSE.txt`, `/NOTICE.txt`, `/THIRD_PARTY_LICENSES.txt`
- Third-party notices: [NOTICE](./NOTICE) (npm production deps, public Python runtime, gopsutil, referenced UI upstreams)
- Public `requirements.txt` excludes private plugin parsers (feedparser/openpyxl/pdfplumber)
- Remaining gaps (favicon provenance, native wheel internals, base-image packages) are listed at the end of NOTICE / THIRD_PARTY_LICENSES.txt

## Source identity

See `.b7-source-sha` for the private-monorepo commit this tree was exported from.
