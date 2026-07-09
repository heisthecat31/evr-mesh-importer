import bpy
"""
EVR Mesh Importer — texture replacement pipeline
Converts user-provided textures to Echo VR raw format and writes them
to the correct quality-tier folders.

Folder reference (all siblings under the extracted game root):
  c2434c5a99e139ce  = CGTextureStreamingResourceWin10 (model→texture mapping)
  ae49fad43254367a  = RawTexturePackfileWin10         (HIGH quality textures)
  4a4c32c49300b8a0  = cgtextureresourceWin10          (LOW quality textures)
"""

import json
import os
import struct
import subprocess
import tempfile
import shutil
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Folder hash constants
# ---------------------------------------------------------------------------
FOLDER_TEXTURE_MAPPING   = "c2434c5a99e139ce"
FOLDER_TEXTURE_HIGH      = "ae49fad43254367a"
FOLDER_TEXTURE_LOW       = "4a4c32c49300b8a0"
FOLDER_TEXTURE_MID       = "beac1969cb7b8861"

# DDS header sizes
DDS_HEADER_STANDARD = 128   # 'DDS ' (4) + DDS_HEADER (124)
DDS_HEADER_DX10     = 148   # standard + DDS_HEADER_DXT10 (20)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class TextureSlot:
    """One texture referenced by a model."""
    metadata_hash: str           # hash in the mapping file (c2434c5a99e139ce)
    payload_hash: str            # actual texture data hash (resolved via texture_mapping.json)
    slot_index: int              # 0=base color, 1=normal, 2=roughness/ORM, 3=emissive
    skin_id: int = 0
    high_path: str | None = None # resolved path in ae49fad43254367a
    low_path: str | None = None  # resolved path in 4a4c32c49300b8a0
    mid_path: str | None = None  # resolved path in beac1969cb7b8861
    original_high_size: int = -1
    original_low_size: int = -1
    original_mid_size: int = -1

    @property
    def role(self) -> str:
        roles = {0: "base_color", 1: "normal", 2: "orm", 3: "emissive"}
        return roles.get(self.slot_index % 4, "unknown")


# ---------------------------------------------------------------------------
# Hash name variations (mirrors textures.py logic)
# ---------------------------------------------------------------------------
def _all_name_variations(name: str) -> list[str]:
    """Returns all representation variations of a hash: padded hex, stripped hex,
    signed decimal, unsigned decimal."""
    variations = {name.lower().strip()}
    name_clean = name.strip().lower()

    val = None
    try:
        val = int(name_clean, 16)
    except ValueError:
        pass
        
    # Map specific known folder hashes to evrtools string names
    if name_clean == "c2434c5a99e139ce" or val == 0xc2434c5a99e139ce:
        variations.add("cgtexturestreamingresourcewin10")
    elif name_clean == "ae49fad43254367a" or val == 0xae49fad43254367a:
        variations.add("rawtexturepackfilewin10")
    elif name_clean == "4a4c32c49300b8a0" or val == 0x4a4c32c49300b8a0:
        variations.add("cgtextureresourcewin10")
    if val is None:
        try:
            temp_val = int(name_clean)
            if -2**63 <= temp_val < 2**64:
                val = temp_val + 2**64 if temp_val < 0 else temp_val
        except ValueError:
            pass

    if val is not None and 0 <= val < 2**64:
        variations.add(str(val))                         # unsigned decimal
        signed_val = val - 2**64 if val >= 2**63 else val
        variations.add(str(signed_val))                  # signed decimal
        variations.add(f"{val:016x}")                    # padded 16-char hex
        variations.add(f"{val:x}")                       # stripped hex

    return sorted(variations)


def _find_file_any_variation(directory: str, hash_name: str, extension: str = "") -> str | None:
    """Find a file in *directory* matching any name variation of *hash_name*,
    optionally with *extension* appended."""
    if not directory or not os.path.isdir(directory):
        return None
    for var in _all_name_variations(hash_name):
        p = os.path.join(directory, var + extension)
        if os.path.isfile(p):
            return p
    return None


# ---------------------------------------------------------------------------
# Texture mapping parser
# ---------------------------------------------------------------------------
def parse_model_texture_mapping(extracted_dir: str, model_hash: str) -> dict | None:
    """Parse the CGTextureStreamingResourceWin10 mapping file for *model_hash*.

    Returns ``{"textures": [hash, ...], "bindings": [{slot_idx, skin_id, texture_idx}, ...]}``
    or ``None`` if the mapping file cannot be found.
    """
    mapping_dir = None
    for var in _all_name_variations(FOLDER_TEXTURE_MAPPING):
        candidate = os.path.join(extracted_dir, var)
        if os.path.isdir(candidate):
            mapping_dir = candidate
            break
    if mapping_dir is None:
        return None

    mapping_file = _find_file_any_variation(mapping_dir, model_hash)
    if mapping_file is None:
        return None

    with open(mapping_file, "rb") as fh:
        data = fh.read()

    if len(data) < 12:
        return None

    tex_count = struct.unpack_from("<I", data, 8)[0]
    if tex_count == 0 or tex_count > 1000:
        return None

    texture_hashes = []
    for i in range(tex_count):
        offset = 12 + i * 8
        if offset + 8 > len(data):
            break
        tex_hash = struct.unpack_from("<Q", data, offset)[0]
        texture_hashes.append(f"{tex_hash:016x}")

    offset = 12 + tex_count * 8
    if offset + 4 > len(data): return None
    layouts_count = struct.unpack_from("<I", data, offset)[0]
    offset += 4 + layouts_count * 192
    
    if offset + 4 > len(data): return None
    obj_count = struct.unpack_from("<I", data, offset)[0]
    
    bindings = []
    for i in range(obj_count):
        off = offset + 4 + i * 8
        if off + 8 > len(data): break
        tex_idx, max_texel_ratio = struct.unpack_from("<If", data, off)
        bindings.append({
            "slot_idx": i,
            "skin_id": 0, # Unused
            "texture_idx": tex_idx,
        })

    return {"textures": texture_hashes, "bindings": bindings}


def rewrite_model_texture_mapping(extracted_dir: str, export_dir: str, model_hash: str, custom_texture_meta_hash: str) -> list[str]:
    """Rewrite the model's mapping file so it links exclusively to 1 custom texture.
    Returns the paths of the written mapping files."""
    mapping_dir = None
    for var in _all_name_variations(FOLDER_TEXTURE_MAPPING):
        candidate = os.path.join(extracted_dir, var)
        if os.path.isdir(candidate):
            mapping_dir = candidate
            break
    if mapping_dir is None:
        return []

    # Handle b7ffa5a41d6141a7 fallback
    targets = [model_hash]
    if model_hash.lower() == "b7ffa5a41d6141a7":
        targets.extend(["b1d4c3494ca01a3f", "eb7461cc21be0753", "eb7461d221be0753"])
        
    written = []
    out_dir = os.path.join(export_dir, FOLDER_TEXTURE_MAPPING)
    os.makedirs(out_dir, exist_ok=True)
        
    for target in targets:
        mapping_file = _find_file_any_variation(mapping_dir, target)
        if mapping_file is None:
            continue
            
        with open(mapping_file, "rb") as fh:
            data = fh.read()
            
        if len(data) < 12: continue
        
        tex_count = struct.unpack_from("<I", data, 8)[0]
        offset = 12 + tex_count * 8
        
        if offset + 4 > len(data): continue
        layouts_count = struct.unpack_from("<I", data, offset)[0]
        offset += 4 + layouts_count * 192
        
        if offset + 4 > len(data): continue
        obj_count = struct.unpack_from("<I", data, offset)[0]
        objs = data[offset + 4 : offset + 4 + obj_count * 8]
        offset += 4 + obj_count * 8
        
        if offset + 4 > len(data): continue
        obb_count = struct.unpack_from("<I", data, offset)[0]
        obbs = data[offset + 4 : offset + 4 + obb_count * 40]
        offset += 4 + obb_count * 40
        
        remaining = data[offset:]
            
        out = bytearray()
        out.extend(data[:8]) # Keep original packfilename
        
        out.extend(struct.pack("<I", 1)) # tex_count = 1
        out.extend(struct.pack("<Q", int(custom_texture_meta_hash, 16)))
        
        out.extend(struct.pack("<I", 0)) # layouts_count = 0 (game defaults fine)
            
        out.extend(struct.pack("<I", obj_count))
        for i in range(obj_count):
            _, ratio = struct.unpack_from("<If", objs, i * 8)
            out.extend(struct.pack("<If", 0, ratio)) # Bind all slots to texture index 0
            
        out.extend(struct.pack("<I", obb_count))
        out.extend(obbs)
        
        out.extend(remaining)
            
        # Try to preserve original filename format (e.g. 0xB1D4C3494CA01A3F.bin or b1d4c3494ca01a3f)
        out_path = os.path.join(out_dir, os.path.basename(mapping_file))
        with open(out_path, "wb") as f:
            f.write(out)
        written.append(out_path)
        
    return written


# ---------------------------------------------------------------------------
# Global texture_mapping.json loader
# ---------------------------------------------------------------------------
_global_mapping_cache = None

def _load_global_texture_mapping() -> dict:
    """Load the metadata→payload hash mapping from texture_mapping.json."""
    global _global_mapping_cache
    if _global_mapping_cache is not None:
        return _global_mapping_cache

    _global_mapping_cache = {}
    _dir = os.path.dirname(os.path.abspath(__file__))
    if os.path.basename(_dir) == "__pycache__":
        _dir = os.path.dirname(_dir)

    mapping_path = os.path.join(_dir, "texture_mapping.json")
    if os.path.exists(mapping_path):
        try:
            with open(mapping_path, "r") as f:
                _global_mapping_cache = json.load(f)
        except Exception as e:
            print(f"[EVR TextureReplace] Error loading texture_mapping.json: {e}")

    return _global_mapping_cache


# ---------------------------------------------------------------------------
# texconv wrapper
# ---------------------------------------------------------------------------
def _find_texconv() -> str | None:
    """Locate texconv.exe: bundled in addon/bin, then PATH, then common install."""
    # 1. Bundled
    addon_dir = os.path.dirname(os.path.abspath(__file__))
    if os.path.basename(addon_dir) == "__pycache__":
        addon_dir = os.path.dirname(addon_dir)
    bundled = os.path.join(addon_dir, "bin", "texconv.exe")
    if os.path.isfile(bundled):
        return bundled

    # 2. PATH
    found = shutil.which("texconv") or shutil.which("texconv.exe")
    if found:
        return found

    # 3. Common winget install location
    local_apps = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages")
    if os.path.isdir(local_apps):
        for root, dirs, files in os.walk(local_apps):
            if "texconv.exe" in files:
                return os.path.join(root, "texconv.exe")

    return None


def _run_texconv(
    input_path: str,
    output_dir: str,
    *,
    texconv_path: str | None = None,
) -> str | None:
    """Run custom texconv to encode *input_path* to a .dds in *output_dir*.
    Returns the path to the output .dds, or None on failure."""
    exe = texconv_path or _find_texconv()
    if exe is None:
        print("[EVR TextureReplace] texconv.exe not found.")
        return None

    os.makedirs(output_dir, exist_ok=True)

    basename = os.path.splitext(os.path.basename(input_path))[0]
    out_dds = os.path.join(output_dir, basename + ".dds")

    cmd = [exe, "encode", input_path, out_dds]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60,
            cwd=os.path.dirname(exe),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0:
            print(f"[EVR TextureReplace] texconv failed: {result.stderr.strip()}\n{result.stdout.strip()}")
            return None
    except FileNotFoundError:
        print(f"[EVR TextureReplace] texconv not found at: {exe}")
        return None
    except subprocess.TimeoutExpired:
        print("[EVR TextureReplace] texconv timed out.")
        return None

    if os.path.isfile(out_dds):
        return out_dds
    return None


def build_texture_descriptor(width: int, height: int, mips: int,
                             dxgi: int, dds_total_size: int,
                             srgb: int = 0) -> bytes:
    """Build the 256-byte CGTexture descriptor (cgtextureresourceWin10) with the
    POPULATED tail the engine needs to upload the DDS sidecar.
    """
    BLOCK16 = {77, 78, 74, 75, 95, 96, 83, 84, 98, 99, 100, 101}  # BC2/3/5/6/7
    blockbytes = 16 if dxgi in BLOCK16 else 8
    bw = max(1, (width + 3) // 4)
    bh = max(1, (height + 3) // 4)
    first_mip_bytes = bw * bh * blockbytes
    desc = bytearray(b"\xFF" * 256)
    fields = [1, width, height, mips, 1, 0, dxgi, srgb & 1, 0, 0,
              width, height, mips, dds_total_size, first_mip_bytes, 0]
    struct.pack_into("<16I", desc, 192, *fields)
    return bytes(desc)


def parse_dds_header(dds_data: bytes) -> tuple[int, int, int, int]:
    """Parse DDS bytes and return (width, height, mips, dxgi_format)."""
    if len(dds_data) < 128 or dds_data[:4] != b'DDS ':
        return 0, 0, 0, 0
    
    h, w = struct.unpack_from('<II', dds_data, 12)
    mips = struct.unpack_from('<I', dds_data, 28)[0]
    
    dx10 = dds_data[84:88] == b'DX10'
    if dx10 and len(dds_data) >= 148:
        dxgi = struct.unpack_from('<I', dds_data, 128)[0]
    else:
        fourcc = dds_data[84:88]
        if fourcc == b'DXT1': dxgi = 71
        elif fourcc == b'DXT5': dxgi = 77
        elif fourcc == b'ATI2' or fourcc == b'BC5U': dxgi = 83
        else: dxgi = 87 # Fallback to B8G8R8A8_UNORM
        
    return w, h, max(1, mips), dxgi


def _strip_dds_header(dds_bytes: bytes) -> bytes:
    """Strip the DDS header to produce raw texture payload bytes."""
    if len(dds_bytes) < 4 or dds_bytes[:4] != b'DDS ':
        return dds_bytes  # not a DDS, return as-is

    # Check for DX10 extended header
    if len(dds_bytes) >= 88:
        fourcc = dds_bytes[84:88]
        if fourcc == b'DX10':
            return dds_bytes[DDS_HEADER_DX10:]

    return dds_bytes[DDS_HEADER_STANDARD:]


# ---------------------------------------------------------------------------
# TextureReplacer — main class
# ---------------------------------------------------------------------------
class TextureReplacer:
    """Replaces all texture tiers for an Echo VR model."""

    def __init__(self, extracted_dir: str, export_dir: str, texconv_path: str | None = None):
        self.extracted_dir = extracted_dir
        self.export_dir = export_dir
        self.texconv_path = texconv_path or _find_texconv()
        self._global_mapping = _load_global_texture_mapping()

    def _resolve_payload_hash(self, metadata_hash: str) -> str:
        """Resolve a metadata texture hash to its payload hash via texture_mapping.json."""
        return self._global_mapping.get(metadata_hash, metadata_hash)

    def _find_texture_file(self, payload_hash: str, folder_hash: str) -> str | None:
        """Find a raw texture file in a specific quality-tier folder."""
        folder = None
        for var in _all_name_variations(folder_hash):
            candidate = os.path.join(self.extracted_dir, var)
            if os.path.isdir(candidate):
                folder = candidate
                break
        if folder is None:
            return None
        return _find_file_any_variation(folder, payload_hash)

    def find_model_textures(self, model_hash: str) -> list[TextureSlot]:
        """Find all textures referenced by *model_hash* and resolve their paths."""
        mapping = parse_model_texture_mapping(self.extracted_dir, model_hash)
        if not mapping or not mapping.get("textures"):
            return []

        seen = set()
        slots: list[TextureSlot] = []

        if mapping.get("bindings"):
            for bind in mapping["bindings"]:
                tex_idx = bind["texture_idx"]
                if tex_idx < 0 or tex_idx >= len(mapping["textures"]):
                    continue
                meta_hash = mapping["textures"][tex_idx]
                if meta_hash in seen:
                    continue
                seen.add(meta_hash)

                payload_hash = self._resolve_payload_hash(meta_hash)
                slot = TextureSlot(
                    metadata_hash=meta_hash,
                    payload_hash=payload_hash,
                    slot_index=bind["slot_idx"] % 4,
                    skin_id=bind.get("skin_id", 0),
                )

                # Resolve paths in all quality tiers
                slot.high_path = self._find_texture_file(payload_hash, FOLDER_TEXTURE_HIGH)
                slot.low_path = self._find_texture_file(payload_hash, FOLDER_TEXTURE_LOW)
                slot.mid_path = self._find_texture_file(payload_hash, FOLDER_TEXTURE_MID)

                if slot.high_path:
                    slot.original_high_size = os.path.getsize(slot.high_path)
                if slot.low_path:
                    slot.original_low_size = os.path.getsize(slot.low_path)
                if slot.mid_path:
                    slot.original_mid_size = os.path.getsize(slot.mid_path)

                slots.append(slot)
        else:
            # Fallback: sequential groups of 4
            for i, meta_hash in enumerate(mapping["textures"]):
                if meta_hash in seen:
                    continue
                seen.add(meta_hash)

                payload_hash = self._resolve_payload_hash(meta_hash)
                slot = TextureSlot(
                    metadata_hash=meta_hash,
                    payload_hash=payload_hash,
                    slot_index=i % 4,
                    skin_id=i // 4,
                )
                slot.high_path = self._find_texture_file(payload_hash, FOLDER_TEXTURE_HIGH)
                slot.low_path = self._find_texture_file(payload_hash, FOLDER_TEXTURE_LOW)
                slot.mid_path = self._find_texture_file(payload_hash, FOLDER_TEXTURE_MID)
                if slot.high_path:
                    slot.original_high_size = os.path.getsize(slot.high_path)
                if slot.low_path:
                    slot.original_low_size = os.path.getsize(slot.low_path)
                if slot.mid_path:
                    slot.original_mid_size = os.path.getsize(slot.mid_path)
                slots.append(slot)

        return slots

    def _convert_to_dds(self, user_image_path: str, target_w: int, target_h: int, tmp_dir: str, force_bc3: bool = False) -> str | None:
        import subprocess
        
        temp_png = os.path.join(tmp_dir, "temp.png")
        
        try:
            img = bpy.data.images.load(user_image_path, check_existing=False)
            if img.size[0] != target_w or img.size[1] != target_h:
                img.scale(target_w, target_h)
            
            orig_fp = img.filepath_raw
            orig_fmt = img.file_format
            
            if force_bc3:
                scene = bpy.context.scene
                orig_scene_fmt = scene.render.image_settings.file_format
                orig_scene_mode = getattr(scene.render.image_settings, "color_mode", None)
                scene.render.image_settings.file_format = 'PNG'
                if orig_scene_mode is not None:
                    scene.render.image_settings.color_mode = 'RGBA'
                try:
                    img.save_render(temp_png)
                finally:
                    scene.render.image_settings.file_format = orig_scene_fmt
                    if orig_scene_mode is not None:
                        scene.render.image_settings.color_mode = orig_scene_mode
            else:
                try:
                    img.file_format = 'PNG'
                    img.filepath_raw = temp_png
                    img.save()
                except Exception:
                    img.save_render(temp_png)
                finally:
                    img.filepath_raw = orig_fp
                    img.file_format = orig_fmt
            bpy.data.images.remove(img)
        except Exception as e:
            print(f"[EVR TextureReplace] Failed to prepare PNG: {e}")
            raise RuntimeError(f"Failed to prepare PNG: {e}")
        
        if not os.path.isfile(temp_png):
            print(f"[EVR TextureReplace] temp PNG was not created at: {temp_png}")
            raise RuntimeError(f"temp PNG not created at {temp_png}")
        
        out_dds = os.path.join(tmp_dir, "output.dds")
        cmd = [self.texconv_path, "encode", temp_png, out_dds]
        
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=60,
                cwd=os.path.dirname(self.texconv_path),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode != 0:
                print(f"[EVR TextureReplace] texconv encode failed: {result.stderr.strip()}\n{result.stdout.strip()}")
                raise RuntimeError(f"texconv failed: {result.stderr.strip()} | {result.stdout.strip()}")
        except FileNotFoundError:
            print(f"[EVR TextureReplace] texconv not found at: {self.texconv_path}")
            raise RuntimeError(f"texconv not found: {self.texconv_path}")
        except subprocess.TimeoutExpired:
            print("[EVR TextureReplace] texconv timed out.")
            raise RuntimeError("texconv timed out")
        
        if os.path.isfile(out_dds):
            return out_dds
        return None

    def replace_texture(
        self,
        slot: TextureSlot,
        user_image_path: str,
        *,
        write_high: bool = True,  # Kept for signature compatibility, but ignored
        write_low: bool = True,   # Kept for signature compatibility, but ignored
        low_max_size: int = 256,
    ) -> dict:
        result = {"high_written": False, "low_written": False,
                   "high_path": None, "low_path": None, "errors": []}

        if not self.texconv_path:
            result["errors"].append("texconv.exe not found")
            return result

        if not os.path.isfile(user_image_path):
            result["errors"].append(f"User image not found: {user_image_path}")
            return result
            
        # Clean up any existing exported files to avoid corrupt mixing
        for q_folder in [FOLDER_TEXTURE_HIGH, FOLDER_TEXTURE_MID, FOLDER_TEXTURE_LOW]:
            h = slot.payload_hash if q_folder == FOLDER_TEXTURE_HIGH else slot.metadata_hash
            old_path = os.path.join(self.export_dir, q_folder, h)
            if os.path.isfile(old_path):
                try: os.remove(old_path)
                except: pass

        with tempfile.TemporaryDirectory(prefix="evr_tex_") as tmp_dir:
            # We don't read dimensions from the original file anymore, we just encode to max_size
            target_w = low_max_size
            target_h = low_max_size
            
            try:
                dds_path = self._convert_to_dds(user_image_path, target_w, target_h, tmp_dir, force_bc3=False)
            except Exception as e:
                result["errors"].append(f"DDS Convert Exception: {str(e)}")
                dds_path = None
                
            if dds_path and isinstance(dds_path, str) and os.path.isfile(dds_path):
                with open(dds_path, "rb") as f:
                    dds_data = f.read()
                    
                w, h, mips, dxgi = parse_dds_header(dds_data)
                if w == 0:
                    result["errors"].append("Failed to parse DDS header from converted texture.")
                    return result

                # 1. Write the synthetic descriptor to LOW
                desc = build_texture_descriptor(w, h, mips, dxgi, len(dds_data), srgb=1)
                
                out_dir = os.path.join(self.export_dir, FOLDER_TEXTURE_LOW)
                os.makedirs(out_dir, exist_ok=True)
                out_path = os.path.join(out_dir, slot.metadata_hash)
                with open(out_path, "wb") as f:
                    f.write(desc)
                result["low_written"] = True
                result["low_path"] = out_path
                    
                # 2. Write the full DDS to MID (0xBEAC1969CB7B8861)
                mid_dir = os.path.join(self.export_dir, FOLDER_TEXTURE_MID)
                os.makedirs(mid_dir, exist_ok=True)
                mid_path = os.path.join(mid_dir, slot.metadata_hash)
                with open(mid_path, "wb") as f:
                    f.write(dds_data)
                    
            else:
                if isinstance(dds_path, tuple):
                    result["errors"].append(f"Error: {dds_path[1]}")
                else:
                    result["errors"].append(f"texconv failed. Input={user_image_path}")

        return result
    def export_model_textures(self, model_hash: str, overwrite: bool = False) -> list[str]:
        """Copy ALL raw textures for *model_hash* from the extracted dir to export_dir.

        Returns a list of written file paths.
        """
        slots = self.find_model_textures(model_hash)
        written = []

        for slot in slots:
            # Copy high quality
            if slot.high_path and os.path.isfile(slot.high_path):
                out_dir = os.path.join(self.export_dir, FOLDER_TEXTURE_HIGH)
                os.makedirs(out_dir, exist_ok=True)
                dst = os.path.join(out_dir, slot.payload_hash)
                if overwrite or not os.path.exists(dst):
                    shutil.copy2(slot.high_path, dst)
                written.append(dst)

            # Copy low quality
            if slot.low_path and os.path.isfile(slot.low_path):
                out_dir = os.path.join(self.export_dir, FOLDER_TEXTURE_LOW)
                os.makedirs(out_dir, exist_ok=True)
                dst = os.path.join(out_dir, slot.metadata_hash)
                if overwrite or not os.path.exists(dst):
                    shutil.copy2(slot.low_path, dst)
                written.append(dst)

            # Copy mid quality
            if slot.mid_path and os.path.isfile(slot.mid_path):
                out_dir = os.path.join(self.export_dir, FOLDER_TEXTURE_MID)
                os.makedirs(out_dir, exist_ok=True)
                dst = os.path.join(out_dir, slot.metadata_hash)
                if overwrite or not os.path.exists(dst):
                    shutil.copy2(slot.mid_path, dst)
                written.append(dst)

        return written
