# Changelog

All notable changes to this project will be documented in this file.

## [1.2.5] - 2026-05-24
### Added
- **Dynamic Metadata Reallocation (The "1MB+ Model" Breakthrough)**: Fully reverse-engineered the `0x0B` rendering descriptors, `0xFFFFFF0C` blocks, and Stream/Index records in the Primary file. The exporter now correctly patches all layout offsets and vertex counts. This explicitly tells the RAD engine to allocate new memory for the custom mesh, meaning **custom models are no longer constrained by the original file size limit**.
- Added documentation outlining how to prevent "Bone LOD Culling" (custom meshes vanishing at a distance due to the engine disabling detail bones).

### Fixed
- **Medium Distance Vanishing Bug**: Fixed a critical issue where the outer shell of custom models would vanish at medium distances (LOD1 and LOD2). The fix correctly patches Stream Records unconditionally, ensuring the engine reads the full index buffer for all LOD levels instead of falling back to the original index counts.
- **Export Crash**: Restored a missing size comparison check (`orig_ioff < orig_gpu_size`) that was inadvertently overwriting the `0x03080000` submesh array flags in the `0x0B` blocks, which caused the game to crash on load.

## [1.2.4] - Previous Release
### Added
- Initial support for exporting Blender meshes to raw GPU binaries (`.bytes`) with heuristic and primary-assisted encoding paths.
- Support for auto-detecting and patching LOD0 bounding boxes.
- Automated texture resolution and material creation for imported models.

### Fixed
- Initial `0x0B` block parsing for basic vertex/index offset patching.
