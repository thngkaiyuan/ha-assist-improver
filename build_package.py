"""Build an installable ZIP containing component code only."""

import hashlib
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

root = Path(__file__).resolve().parent
output = root / "dist"
output.mkdir(exist_ok=True)
version = json.loads((root / "custom_components/assist_improver/manifest.json").read_text())["version"]
const = (root / "custom_components/assist_improver/const.py").read_text()
if f'VERSION = "{version}"' not in const:
    raise SystemExit(f"const.py VERSION does not match manifest version {version}")
target = output / f"assist-improver-{version}.zip"
files = sorted(p for p in (root / "custom_components/assist_improver").rglob("*")
               if p.is_file() and p.suffix in {".py", ".json", ".yaml", ".png", ".js"})
with ZipFile(target, "w", ZIP_DEFLATED) as archive:
    for source in files:
        archive.write(source, source.relative_to(root))
with ZipFile(target) as archive:
    assert archive.testzip() is None
    assert all(n.startswith("custom_components/assist_improver/") for n in archive.namelist())
    assert "custom_components/assist_improver/manifest.json" in archive.namelist()
(target.with_suffix(".sha256")).write_text(hashlib.sha256(target.read_bytes()).hexdigest() + "\n")
print(f"Built {target.name}: {len(files)} component files, {target.stat().st_size} bytes")
