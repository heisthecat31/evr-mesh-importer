"""
EVR Raw Mesh Importer — primary file auto-discovery
Pure Python, no bpy dependency.
https://github.com/Dualgame/evr-mesh-importer
"""

import os


def _is_same_file(path_a, path_b):
    """Case-insensitive, normpath comparison (Windows safe)."""
    return os.path.normcase(os.path.abspath(path_a)) == os.path.normcase(os.path.abspath(path_b))


def _find_primary_path(gpu_filepath):
    """Try to find the matching Primary binary file path for the given GPU file.
    Supports hash-based directories (e.g. e7a8ab5ceaef49cb -> 37102e4b27955a14)
    and named directories (e.g. GPU/CGMeshListResource -> Primary/CGMeshListResource).
    """
    raw_fname = os.path.basename(gpu_filepath)
    # Strip any " GPU", " Primary" etc. suffix so bare hash name is used for lookup
    fname = raw_fname.split(" ")[0] if " " in raw_fname else raw_fname
    parent = os.path.dirname(gpu_filepath)
    grandparent = os.path.dirname(parent)

    # GPU-folder -> Primary-folder mapping (all known pairs)
    gpu_to_primary = {
        "e7a8ab5ceaef49cb": "37102e4b27955a14",
        "e642bfb1abcf76df": "4e426f88c1b5d7ac",
        "CGMeshListResource": "CGMeshListResource",
        "CGInstancedModelResource": "CGInstancedModelResource",
    }
    # All known primary folders, searched in order when GPU folder is unknown
    all_primary_folders = [
        "4e426f88c1b5d7ac",
        "37102e4b27955a14",
        "CGMeshListResource",
        "CGInstancedModelResource",
    ]

    parent_folder = os.path.basename(parent)
    primary_folder = gpu_to_primary.get(parent_folder)

    # --- Step 0: GPU/Primary convention (most reliable for models/ layout) ---
    # e.g. models/GPU/CGMeshListResource/hash -> models/Primary/CGMeshListResource/hash
    family = os.path.basename(parent)
    for gpu_dir, prim_dir in (("GPU", "Primary"),):
        if gpu_dir in parent:
            candidate = gpu_filepath.replace(
                os.path.join(gpu_dir, family),
                os.path.join(prim_dir, family),
                1,
            )
            if not _is_same_file(candidate, gpu_filepath) and os.path.isfile(candidate):
                return candidate
            # Also try with the stripped fname in case the raw filename has a suffix
            if fname != raw_fname:
                candidate2 = os.path.join(
                    parent.replace(os.path.join(gpu_dir, family), os.path.join(prim_dir, family), 1),
                    fname,
                )
                if not _is_same_file(candidate2, gpu_filepath) and os.path.isfile(candidate2):
                    return candidate2

    if primary_folder:
        # --- Step 1: Flat hash sibling: parent/fname -> sibling/fname ---
        candidate1 = os.path.join(grandparent, primary_folder, fname)
        if not _is_same_file(candidate1, gpu_filepath) and os.path.isfile(candidate1):
            return candidate1

        # --- Step 2: Nested hash sibling: ggparent/Primary/sibling/fname ---
        ggparent = os.path.dirname(grandparent)
        candidate2 = os.path.join(ggparent, "Primary", primary_folder, fname)
        if not _is_same_file(candidate2, gpu_filepath) and os.path.isfile(candidate2):
            return candidate2

    # --- Step 3: Recursive fallback under grandparent ---
    if os.path.isdir(grandparent):
        for root, dirs, files in os.walk(grandparent):
            if fname in files:
                c = os.path.join(root, fname)
                if not _is_same_file(c, gpu_filepath) and os.path.isfile(c):
                    lower_path = c.lower().replace(os.sep, "/")
                    if "primary" in lower_path or "37102e4b" in lower_path or "4e426f88" in lower_path:
                        return c

    # --- Step 4: Sibling Primary folder ---
    sibling = os.path.join(grandparent, "Primary", fname)
    if not _is_same_file(sibling, gpu_filepath) and os.path.isfile(sibling):
        return sibling

    # --- Step 5: Search known pcvr-extracted roots ---
    known_pcvr_roots = [
        r"G:\pcvr-extracted",
        r"J:\EchoVR-Tools-Launcher\Tools\Settings\pcvr-extracted",
    ]
    # Try to infer pcvr-extracted root from the GPU file's path
    probe = grandparent
    for _ in range(4):
        if any(os.path.isdir(os.path.join(probe, gf)) for gf in gpu_to_primary):
            if probe not in known_pcvr_roots:
                known_pcvr_roots.insert(0, probe)
            break
        probe = os.path.dirname(probe)

    search_primary_folders = [primary_folder] if primary_folder else all_primary_folders
    for pcvr_root in known_pcvr_roots:
        if not os.path.isdir(pcvr_root):
            continue
        for pf in search_primary_folders:
            candidate = os.path.join(pcvr_root, pf, fname)
            if not _is_same_file(candidate, gpu_filepath) and os.path.isfile(candidate):
                return candidate

    return None


def _find_primary_data(gpu_filepath):
    """Try to find and load a matching Primary binary for the given GPU file."""
    path = _find_primary_path(gpu_filepath)
    if path and os.path.isfile(path):
        with open(path, "rb") as fh:
            return fh.read()
    return None
