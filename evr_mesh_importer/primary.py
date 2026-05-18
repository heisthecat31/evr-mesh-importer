"""
EVR Raw Mesh Importer — primary file auto-discovery
Pure Python, no bpy dependency.
https://github.com/Dualgame/evr-mesh-importer
"""

import os


def _find_primary_data(gpu_filepath):
    """Try to find and load a matching Primary binary for the given GPU file.
    Supports hash-based directories (e.g. e7a8ab5ceaef49cb -> 37102e4b27955a14).
    """
    fname = os.path.basename(gpu_filepath)
    parent = os.path.dirname(gpu_filepath)
    grandparent = os.path.dirname(parent)

    # 1. Flat hash sibling: e7a8ab5ceaef49cb/fname -> 37102e4b27955a14/fname
    candidate1 = os.path.join(grandparent, "37102e4b27955a14", fname)
    if os.path.isfile(candidate1):
        with open(candidate1, "rb") as fh:
            return fh.read()

    # 2. Nested hash sibling: GPU/e7a8ab5ceaef49cb/fname -> Primary/37102e4b27955a14/fname
    ggparent = os.path.dirname(grandparent)
    candidate2 = os.path.join(ggparent, "Primary", "37102e4b27955a14", fname)
    if os.path.isfile(candidate2):
        with open(candidate2, "rb") as fh:
            return fh.read()

    # 3. Recursive fallback under grandparent
    if os.path.isdir(grandparent):
        for root, dirs, files in os.walk(grandparent):
            if fname in files:
                c = os.path.join(root, fname)
                if c != gpu_filepath and os.path.isfile(c):
                    lower_path = c.lower().replace(os.sep, "/")
                    if "primary" in lower_path or "37102e4b" in lower_path:
                        with open(c, "rb") as fh:
                            return fh.read()

    # 4. Original conventions
    family = os.path.basename(parent)
    for gpu_dir, prim_dir in (("GPU", "Primary"),):
        if gpu_dir in parent:
            candidate = gpu_filepath.replace(
                os.path.join(gpu_dir, family),
                os.path.join(prim_dir, family),
                1,
            )
            if candidate != gpu_filepath and os.path.isfile(candidate):
                with open(candidate, "rb") as fh:
                    return fh.read()

    sibling = os.path.join(grandparent, "Primary", fname)
    if os.path.isfile(sibling):
        with open(sibling, "rb") as fh:
            return fh.read()

    return None
