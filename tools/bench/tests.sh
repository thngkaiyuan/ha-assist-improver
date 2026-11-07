#!/bin/sh
# In Home Assistant's image (ha.sh run NAME -- sh tools/bench/tests.sh PYTEST_ARGS...): pytest added, with the image's
# own Home Assistant and Python, then pytest run from this checkout. The image's packages stay as they are: each added
# one must be new to it.
set -eu
cd "$(dirname "$0")/../.."
python3 - tools/bench/requirements.txt <<'EOF'
import importlib.metadata as metadata, sys
had = []
for name in (line.split("==")[0] for line in open(sys.argv[1]) if "==" in line and not line.startswith("#")):
    try:
        had.append(f"{name} {metadata.version(name)}")
    except metadata.PackageNotFoundError:
        pass
if had:
    sys.exit(f"The image has {', '.join(had)} already: drop it from tools/bench/requirements.txt")
EOF
unset UV_EXTRA_INDEX_URL  # From PyPI: Home Assistant's own wheel index has older pytest-asyncio.
uv pip install --quiet --no-deps -r tools/bench/requirements.txt
export PYTHONDONTWRITEBYTECODE=1  # The checkout is read-only.
# pytest's own scratch off /tmp, which is noexec as under the Supervisor: some tests run stand-in programs from it.
exec python3 -m pytest --basetemp=/var/tmp/pytest "$@"
