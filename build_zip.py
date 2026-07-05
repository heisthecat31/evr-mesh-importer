#!/usr/bin/env python3
"""
Creates the installable Blender add-on zip.

Usage:
    python3 build_zip.py [version]

    version defaults to the version tuple in evr_mesh_importer/__init__.py.

Output:
    release/evr_mesh_importer_v<version>.zip
"""
import zipfile
import pathlib
import re
import sys

src = pathlib.Path("evr_mesh_importer")
init = (src / "__init__.py").read_text()
m = re.search(r'"version":\s*\((\d+),\s*(\d+),\s*(\d+)\)', init)
version = ".".join(m.groups()) if m else sys.argv[1] if len(sys.argv) > 1 else "dev"

out_dir = pathlib.Path("release")
out_dir.mkdir(exist_ok=True)
out = out_dir / f"evr_mesh_importer_v{version}.zip"

with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
    for ext in ("*.py", "*.json"):
        for f in sorted(src.rglob(ext)):
            zf.write(f)

print(f"Built {out}  ({out.stat().st_size // 1024} KB)")
