# Agent notes (public tree)

This checkout is a **public export**. There is no `.agents/` execution tree here.

- Prefer `kernel/` as the implementation root.
- Public Docker builds must use `--target public` (default). Do not copy private plugins or `tools/personal-workbench`.
- Public profile must not fall back to hard-coded private machines; missing config ⇒ empty ledger.
- Do not commit `.env`, secrets, production dumps, or Tailscale identities.
