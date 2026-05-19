"""
EVR Raw Mesh Importer — primary file auto-discovery
Pure Python, no bpy dependency.
https://github.com/Dualgame/evr-mesh-importer
"""

import os


def _find_primary_path(gpu_filepath):
    """Try to find the matching Primary binary file path for the given GPU file.
    Supports hash-based directories (e.g. e7a8ab5ceaef49cb -> 37102e4b27955a14).
    """
    fname = os.path.basename(gpu_filepath)
    parent = os.path.dirname(gpu_filepath)
    grandparent = os.path.dirname(parent)

    gpu_to_primary = {
        # Hex
        "e7a8ab5ceaef49cb": "37102e4b27955a14",
        "e642bfb1abcf76df": "4e426f88c1b5d7ac",
        
        # Signed decimal
        "-1753733075210942005": "3967733470876129812",
        "-1854709326710606113": "5639148493132642220",
        
        # Unsigned decimal
        "16693010998498609611": "3967733470876129812",
        "16592034746998945503": "5639148493132642220",
        
        "CGMeshListResource": "CGMeshListResource",
        "CGInstancedModelResource": "CGInstancedModelResource",
    }
    
    parent_folder = os.path.basename(parent)
    primary_folder_base = gpu_to_primary.get(parent_folder, "37102e4b27955a14")
    
    # Generate all spelling variations for the target primary folder
    primary_folders = {primary_folder_base.lower()}
    if len(primary_folder_base) == 16:
        try:
            val = int(primary_folder_base, 16)
            primary_folders.add(str(val))
            primary_folders.add(str(val - 2**64 if val >= 2**63 else val))
        except ValueError:
            pass
            
    def is_same_file(p1, p2):
        return os.path.abspath(p1).lower() == os.path.abspath(p2).lower()
        
    # Try all candidate primary folders
    for pf in list(primary_folders):
        # 1. Flat hash sibling: parent/fname -> sibling/fname
        candidate1 = os.path.join(grandparent, pf, fname)
        if os.path.isfile(candidate1) and not is_same_file(candidate1, gpu_filepath):
            return candidate1

        # 2. Nested hash sibling: GPU/parent/fname -> Primary/sibling/fname
        ggparent = os.path.dirname(grandparent)
        candidate2 = os.path.join(ggparent, "Primary", pf, fname)
        if os.path.isfile(candidate2) and not is_same_file(candidate2, gpu_filepath):
            return candidate2

    # 3. Recursive fallback under grandparent
    if os.path.isdir(grandparent):
        for root, dirs, files in os.walk(grandparent):
            if fname in files:
                c = os.path.join(root, fname)
                if os.path.isfile(c) and not is_same_file(c, gpu_filepath):
                    lower_path = c.lower().replace(os.sep, "/")
                    if "primary" in lower_path or "37102e4b" in lower_path or "39677334" in lower_path:
                        return c

    # 4. Original conventions
    family = os.path.basename(parent)
    for gpu_dir, prim_dir in (("GPU", "Primary"),):
        if gpu_dir in parent:
            candidate = gpu_filepath.replace(
                os.path.join(gpu_dir, family),
                os.path.join(prim_dir, family),
                1,
            )
            if os.path.isfile(candidate) and not is_same_file(candidate, gpu_filepath):
                return candidate

    sibling = os.path.join(grandparent, "Primary", fname)
    if os.path.isfile(sibling) and not is_same_file(sibling, gpu_filepath):
        return sibling

    return None


def _find_primary_data(gpu_filepath):
    """Try to find and load a matching Primary binary for the given GPU file."""
    path = _find_primary_path(gpu_filepath)
    if path and os.path.isfile(path):
        with open(path, "rb") as fh:
            return fh.read()
    return None
