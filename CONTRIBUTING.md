# Contributing

- Run `python -m pytest` before opening a pull request (CI runs it on every PR to `main`).
- Home Assistant component tests: `ha/tests/run.sh` (needs Docker).
- Keep changes small. Never add secrets, hostnames, account ids or personal data to the repo: they are configuration
  (`deploy/state.py`) or DPAPI secrets (`deploy/secrets.py`).
- A change to the turn endpoint contract (`docs/ARCHITECTURE.md`) needs a new API version.
- `main` is protected (see `scripts/secure-repo.ps1`): open a pull request; it merges (squash) once CI passes.
