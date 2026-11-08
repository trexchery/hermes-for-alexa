#!/usr/bin/env bash
# Runs the Home Assistant tests (ha/tests) in a python:3.14 container (Home Assistant 2026.11 needs Python 3.14).
# Usage: ha/tests/run.sh [pytest args]     Env: HA_VERSION (default 2026.10.4), PYTEST_HA (default 0.13.375)
# pytest-homeassistant-custom-component 0.13.375 is the newest release and pins Home Assistant 2026.10.4, so that is
# the default. Installing HA 2026.11.0 over it breaks inside the plugin; set both variables when a plugin release
# for 2026.11 exists.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
src="$here"
if command -v cygpath >/dev/null 2>&1; then src="$(cygpath -m "$here")"; fi
MSYS_NO_PATHCONV=1 docker run --rm \
  -v "$src:/src:ro" \
  -v hermes-voice-pip-cache:/root/.cache/pip \
  -e HA_VERSION="${HA_VERSION:-2026.10.4}" -e PYTEST_HA="${PYTEST_HA:-0.13.375}" \
  python:3.14-slim bash -ec '
    export PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1
    pip install -q "pytest-homeassistant-custom-component==$PYTEST_HA"
    pip install -q "homeassistant==$HA_VERSION"
    pip install -q $(python /src/tests/ha_requirements.py)
    pip list 2>/dev/null | grep -i -E "^(homeassistant|pytest-homeassistant-custom-component|probatio|voluptuous) "
    cp -r /src /work && cd /work
    # import homeassistant first: it aliases httpx before the pytest plugin imports it (needed when HA_VERSION
    # differs from the version the plugin pins)
    python -c "import sys, homeassistant, pytest; sys.exit(pytest.main(sys.argv[1:]))" -p no:cacheprovider -q "$@"
  ' _ "$@"
