# EVR Raw Mesh Importer

A Blender add-on that imports raw GPU mesh binaries extracted from Echo VR (Echo Arena). It supports both instanced prop meshes (CIMR) and map geometry meshes (CGML), using a multi-stage decode chain that falls back from primary-file-assisted decode to GPU-only heuristics.

> **Vibe coded:** This add-on was built entirely with [Claude](https://claude.ai) reverse engineering the Echo VR binary formats from scratch. No official documentation or SDK was used.

> **Tested on:** Echo VR version **34.4.631547.1** (Echo Arena) only. The add-on works with already-extracted game files — it does not unpack or decrypt game archives.

---

## Requirements

- Blender 3.0 or newer (tested up to 5.x)
- Python 3.10+ (bundled with Blender — no separate install needed)
- Pre-extracted Echo VR game files (GPU and Primary binary directories)

---

## Installation

1. Download `evr_mesh_importer_v1.0.0.zip` from the [Releases](https://github.com/Dualgame/evr-mesh-importer/releases) page.
2. In Blender, open **Edit > Preferences > Add-ons**.
3. Click **Install…** and select the downloaded zip file.
4. Enable the add-on by checking the box next to **EVR Raw Mesh Importer**.

---

## Usage

### Importing a single file

Go to **File > Import > EVR Raw Mesh**, navigate to a GPU binary (e.g. inside `GPU/CGInstancedModelResource/`), and click **Import EVR Raw Mesh**.

### Importing multiple files at once

In the file browser, select multiple files (Shift-click or Ctrl-click) before clicking Import. Each file is imported as its own root object. All imported roots are selected when the dialog closes, with the last one set as the active object.

### Exporting custom meshes (Bypassing File Size Limits)

Go to **File > Export > EVR Raw Mesh** and select the original GPU file you want to overwrite. 

> **Tip:** You can also export directly from the 3D Viewport by pressing **N** to open the sidebar and selecting the **EVR Mesh Exporter** tab.

For the most reliable export (especially for props and chassis models), ensure the **Encode Mode** is set to **Primary Described (Full Replace)** and point it to the matching original Primary metadata file.

**Massive Breakthrough:** The exporter now fully reverse-engineers and patches the `0x0B` rendering descriptors, Stream Records, and Index Records inside the Primary file. This explicitly tells the RAD Engine to dynamically allocate new memory for your custom mesh. **You are no longer constrained by the original GPU file's size!** You can safely overwrite a 180 KB original file with a 1 MB+ high-poly custom mesh, and it will load flawlessly in-game (up to the engine's hard limit of 65,535 vertices per submesh).

#### Preventing "Bone LOD Culling" (Mesh Vanishing)
If you are overwriting a "Chassis" model (which contains a complex skeleton), the engine will automatically disable "detail" bones (like antennas or small fins) at a distance to save performance. If your custom mesh is weighted to one of these detail bones, parts of it will vanish when you move far away.
**To fix this:** In Blender, delete all Vertex Groups on your custom mesh, create a single new vertex group named exactly **`Bone.000`** (or your absolute root bone), and assign all vertices to it with a weight of `1.0`. The root bone is never culled, ensuring your model stays visible at any distance.

### Primary file auto-discovery

For the best decode quality, the importer needs a matching Primary binary alongside the GPU binary. If **Auto-find Primary** is enabled (the default), the importer looks for the Primary file automatically using the standard extracted directory layout:

```
<root>/
  GPU/
    CGInstancedModelResource/
      0xabcdef1234567890      ← GPU binary you import
  Primary/
    CGInstancedModelResource/
      0xabcdef1234567890      ← matched automatically
```

If your layout differs, uncheck **Auto-find Primary** and point the **Primary File** field to the correct file manually.

### Options

| Option | Default | Description |
|---|---|---|
| Auto-find Primary | On | Locate the matching Primary binary automatically |
| Primary File | *(blank)* | Override path to the Primary binary |
| Smooth Shading | Off | Apply smooth shading to all imported meshes |
| Scale | 1.0 | Uniform scale factor applied to all vertices |

---

## Supported formats

| Format | Family folder | Description |
|---|---|---|
| CIMR | `CGInstancedModelResource` | Instanced props and actors |
| CGML | `CGMeshListResource` | Map base geometry (multi-submesh) |

### Decode chain

The importer tries decode paths in order, using the first that succeeds:

| Priority | Path | Requires Primary |
|---|---|---|
| 1 | `primary_described` | Yes |
| 2 | `crossref_ib` | Yes |
| 3 | `hero` | Yes |
| 4 | `cgml` | Yes |
| 5 | `heuristic` | No |

The decode path used for each file is shown in the Blender Info bar after import (e.g. `0xabcd…: 1 mesh(es) 512f [primary_described]`).

---

## Scene organisation

**Single-submesh files** import as a plain mesh object named after the file hash.

**Multi-submesh files** (typically CGML map geometry) import under a named Empty object, with each submesh as a child named `<hash>.000`, `<hash>.001`, etc. This keeps the Outliner clean — one entry per file.

---

## Known limitations

### One unsupported file (hash prefix `0x5343dd53eb81f3b`)

This GPU binary contains two independent mesh regions. The correct mesh is a 3-D stepped ledge (64 triangles). The importer instead outputs a flat slab (124 triangles) because the ledge and the slab share the same binary layout as several other files that are correctly decoded via the heuristic path. A generic fix is not feasible without hard-coding the specific hash, which is out of scope for this release.

---

## Building from source

```bash
python3 build_zip.py
```

Output: `release/evr_mesh_importer_v1.0.0.zip`

---

## License

MIT — see [LICENSE](LICENSE).
