"""Print the pip requirements of the Home Assistant components hermes_voice depends on (run.sh installs them).

The pytest plugin only brings Home Assistant core; each integration's own pip requirements (hassil for
``conversation``) come from its manifest.
"""

import importlib.util
import json
from pathlib import Path

root = Path(importlib.util.find_spec("homeassistant.components").submodule_search_locations[0])
mine = json.loads((Path(__file__).parents[1] / "custom_components/hermes_voice/manifest.json").read_text())
todo, seen, reqs = list(mine["dependencies"]), set(), []
while todo:
    name = todo.pop()
    if name in seen:
        continue
    seen.add(name)
    manifest = json.loads((root / name / "manifest.json").read_text())
    reqs += manifest.get("requirements", [])
    todo += manifest.get("dependencies", [])
print("\n".join(sorted(set(reqs))))
