"""
EVR Raw Mesh Importer — primary file auto-discovery
Pure Python, no bpy dependency.
https://github.com/Dualgame/evr-mesh-importer
"""

import os


def _find_primary_data(gpu_filepath):
    """Try to find and load a matching Primary binary for the given GPU file.

    Checks two conventions:
    1. The project tree convention:  GPU/<family>/<name>  ->  Primary/<family>/<name>
    2. A sibling directory:          any/.../GPU/<name>   ->  any/.../Primary/<name>

    Returns bytes or None.
    """
    fname = os.path.basename(gpu_filepath)
    parent = os.path.dirname(gpu_filepath)
    grandparent = os.path.dirname(parent)

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
