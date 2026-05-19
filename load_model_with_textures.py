"""
Blender Script: EVR Raw Mesh & PBR Texture Loader
=================================================
This script programmatically imports an Echo VR raw GPU mesh binary using the
evr-mesh-importer Blender addon, extracts the PBR texture mapping from the game's
materials mapping metadata (c2434c5a99e139ce), locates the high-res PNG textures
in the game's texture cache, and builds advanced PBR node materials in Blender.

How to use:
1. Open Blender and go to the "Scripting" tab.
2. Click "New" to create a new text block, and paste this script.
3. Edit the settings in the CONFIG section below if needed.
4. Run the script!
"""

import os
import json
import struct
import math
import bpy
import importlib
import sys

# Programmatically reload the importer addon submodules to ensure any disk edits are live in Blender
addon_submodules = [
    "evr_mesh_importer",
    "evr_mesh_importer.decode",
    "evr_mesh_importer.primary",
    "evr_mesh_importer.encode"
]
for sub in addon_submodules:
    if sub in sys.modules:
        try:
            importlib.reload(sys.modules[sub])
            print(f"[+] Programmatically reloaded cached module in Blender: {sub}")
        except Exception as e:
            print(f"[-] Failed to reload {sub}: {e}")

# ==============================================================================
# CONFIGURATION
# ==============================================================================
CONFIG = {
    # The hex hash of the model you want to import and texture.
    # Set to "auto" to automatically deduce the hash from the currently selected object's name!
    # e.g., "8b8acfe2c82f6ffb" (Robot Chassis), "8b8acfe2c82f6ffb" (Booster/Other)
    "MODEL_HASH": "8b8acfe2c82f6ffb",
    
    # Mode: 
    # 'IMPORT_AND_TEXTURE': Imports the model binary fresh and applies textures
    # 'TEXTURE_ACTIVE': Applies textures onto the currently selected object in Blender
    "MODE": "IMPORT_AND_TEXTURE",
    
    # Path to the raw GPU mesh binary file (only needed if MODE is 'IMPORT_AND_TEXTURE')
    # If set to None or empty, the script will try to auto-locate it in pcvr-extracted
    "GPU_BINARY_PATH": None,
    
    # Explicit override paths (if None, the script will auto-detect using config.json or fallbacks)
    "PCVR_EXTRACTED_DIR": None,
    "TEXTURE_CACHE_DIR": None,
}

# ==============================================================================
# PATH RESOLUTION HELPERS
# ==============================================================================
def hex_to_signed_decimal(hex_str):
    val = int(hex_str, 16)
    if val >= 2**63:
        val -= 2**64
    return str(val)

def signed_decimal_to_hex(dec_str):
    try:
        val = int(dec_str)
        if val < 0:
            val += 2**64
        return f"{val:016x}"
    except ValueError:
        return dec_str

def get_all_name_variations(name):
    """Returns a list of all representation variations of a hash (padded hex, stripped hex, signed decimal, unsigned decimal)."""
    variations = {name.lower()}
    name_clean = name.strip().lower()
    
    # Try parsing as hex
    val = None
    try:
        val = int(name_clean, 16)
    except ValueError:
        pass
        
    # Try parsing as decimal (signed or unsigned)
    if val is None:
        try:
            temp_val = int(name_clean)
            if -2**63 <= temp_val < 2**64:
                val = temp_val + 2**64 if temp_val < 0 else temp_val
        except ValueError:
            pass
            
    if val is not None and 0 <= val < 2**64:
        # 1. Unsigned decimal
        variations.add(str(val))
        # 2. Signed decimal
        signed_val = val - 2**64 if val >= 2**63 else val
        variations.add(str(signed_val))
        # 3. Padded 16-char hex
        variations.add(f"{val:016x}")
        # 4. Hex with leading zeros stripped
        variations.add(f"{val:x}")
        
    return sorted(list(variations))

def is_valid_extracted_dir(path):
    """Verifies if the path actually contains the materials mapping database directory."""
    if not path or not os.path.exists(path):
        return False
    hex_mapping = os.path.join(path, "c2434c5a99e139ce")
    dec_mapping = os.path.join(path, hex_to_signed_decimal("c2434c5a99e139ce"))
    unsigned_mapping = os.path.join(path, str(int("c2434c5a99e139ce", 16)))
    return os.path.exists(hex_mapping) or os.path.exists(dec_mapping) or os.path.exists(unsigned_mapping)

def is_valid_texture_cache(path):
    """Verifies if the path exists and contains cached PNG texture files."""
    if not path or not os.path.exists(path):
        return False
    try:
        for f in os.listdir(path):
            if f.lower().endswith(".png"):
                return True
    except Exception:
        pass
    return False

def get_model_hash_from_active():
    """Extracts a valid 16-character hex hash from the active selected Blender object's name."""
    active_obj = bpy.context.active_object
    if active_obj:
        # e.g., "8b8acfe2c82f6ffb" or "8b8acfe2c82f6ffb.001"
        name = active_obj.name.split('.')[0]
        if len(name) == 16:
            try:
                int(name, 16)
                return name
            except ValueError:
                pass
    return None

def discover_paths():
    """Auto-discovers the game extraction directories using config.json or default paths."""
    paths = {
        "pcvr_extracted": None,
        "texture_cache": None,
    }
    
    # 1. Try to load config.json from Tools/Settings
    potential_config_paths = [
        r"J:\EchoVR-Tools-Launcher\Tools\Settings\config.json",
        r"C:\Oculus\Games\Software\Software\ready-at-dawn-echo-arena\bin\win10\Tools\Tools\Settings\config.json"
    ]
    
    for cp in potential_config_paths:
        if os.path.exists(cp):
            try:
                with open(cp, "r") as f:
                    cfg = json.load(f)
                
                cand_extracted = cfg.get("extracted_folder") or cfg.get("output_folder")
                if is_valid_extracted_dir(cand_extracted):
                    paths["pcvr_extracted"] = cand_extracted
                
                # Check data folder and walk up parent directories to deduce game base path
                data_folder = cfg.get("data_folder")
                if data_folder:
                    curr = data_folder
                    for _ in range(6):
                        tc = os.path.join(curr, "bin", "win10", "Tools", "Tools", "Settings", "texture_cache")
                        if is_valid_texture_cache(tc):
                            paths["texture_cache"] = tc
                            break
                        tc = os.path.join(curr, "bin", "win10", "Tools", "Settings", "texture_cache")
                        if is_valid_texture_cache(tc):
                            paths["texture_cache"] = tc
                            break
                        curr = os.path.dirname(curr)
                
                if paths["pcvr_extracted"] or paths["texture_cache"]:
                    print(f"[+] Successfully read paths from config: {cp}")
                    break
            except Exception as e:
                print(f"[-] Error loading config {cp}: {e}")
                
    # 2. Fallbacks and Prioritization for extracted PCVR files
    # If G:\pcvr-extracted exists, we always prioritize it!
    if os.path.exists(r"G:\pcvr-extracted"):
        paths["pcvr_extracted"] = r"G:\pcvr-extracted"
    elif not paths["pcvr_extracted"]:
        fallbacks = [
            r"G:\pcvr-extracted",
            r"J:\EchoVR-Tools-Launcher\Tools\Settings\pcvr-extracted"
        ]
        for fb in fallbacks:
            if is_valid_extracted_dir(fb):
                paths["pcvr_extracted"] = fb
                break
                
    # 3. Fallbacks for texture cache
    if not paths["texture_cache"]:
        fallbacks = [
            r"C:\Oculus\Games\Software\Software\ready-at-dawn-echo-arena\bin\win10\Tools\Tools\Settings\texture_cache",
            r"C:\Oculus\Games\Software\Software\ready-at-dawn-echo-arena\bin\win10\Tools\Settings\texture_cache",
            r"J:\EchoVR-Tools-Launcher\EchoVR-Cosmetics-Editor\Settings\texture_cache",
            r"J:\EchoVR-Tools-Launcher\Tools\Settings\texture_cache"
        ]
        for fb in fallbacks:
            if is_valid_texture_cache(fb):
                paths["texture_cache"] = fb
                break
                
    # 4. User overrides
    if CONFIG["PCVR_EXTRACTED_DIR"]:
        paths["pcvr_extracted"] = CONFIG["PCVR_EXTRACTED_DIR"]
    if CONFIG["TEXTURE_CACHE_DIR"]:
        paths["texture_cache"] = CONFIG["TEXTURE_CACHE_DIR"]
        
    return paths

# ==============================================================================
# METADATA PARSING
# ==============================================================================
def parse_materials_mapping(pcvr_extracted_dir, model_hash):
    """Parses c2434c5a99e139ce materials mapping file for texture hashes and bindings."""
    meta_folder_hex = "c2434c5a99e139ce"
    
    meta_vars = get_all_name_variations(meta_folder_hex)
    model_vars = get_all_name_variations(model_hash)
    
    candidates = []
    for mf in meta_vars:
        for mv in model_vars:
            candidates.append(os.path.join(pcvr_extracted_dir, mf, mv))
            
    mapping_path = None
    for c in candidates:
        if os.path.exists(c):
            mapping_path = c
            break
            
    if not mapping_path:
        print(f"[-] Materials mapping file not found for model: {model_hash}")
        return None
        
    print(f"[+] Found Materials Mapping File: {mapping_path}")
    with open(mapping_path, "rb") as fh:
        data = fh.read()
        
    # Read texture count at offset 8
    tex_count = struct.unpack_from("<I", data, 8)[0]
    print(f"    Texture Count in Mapping: {tex_count}")
    
    texture_hashes = []
    for i in range(tex_count):
        offset = 12 + i * 8
        tex_hash = struct.unpack_from("<Q", data, offset)[0]
        texture_hashes.append(f"{tex_hash:016x}")
        
    # Read slot count at offset (12 + tex_count * 8 + 4)
    # Note: there is 4 bytes of padding before slot count to align to 8-byte boundary
    rem_offset = 12 + tex_count * 8
    # Align to 8-byte boundary
    if rem_offset % 8 != 0:
        rem_offset += 4
        
    slot_count = struct.unpack_from("<I", data, rem_offset)[0]
    print(f"    Material Bindings Count: {slot_count}")
    
    bindings = []
    for i in range(slot_count):
        off = rem_offset + 8 + i * 8
        val_float = struct.unpack_from("<f", data, off)[0]
        val_int = struct.unpack_from("<i", data, off + 4)[0]
        bindings.append({
            "slot_idx": i,
            "scale": val_float,
            "texture_idx": val_int
        })
        
    return {
        "textures": texture_hashes,
        "bindings": bindings
    }

# ==============================================================================
# TEXTURE CLASSIFIER (PIXEL ANALYSIS)
# ==============================================================================
def classify_texture_by_pixels(filepath):
    """Loads image and analyzes pixel channels to classify Albedo, Normal, Roughness, or Metallic."""
    try:
        img = bpy.data.images.load(filepath)
        # Downsample to 8x8 to make pixel analysis instantaneous and avoid freezing Blender on high-res textures
        img.scale(8, 8)
    except Exception as e:
        print(f"[-] Could not load {filepath} for analysis: {e}")
        return "albedo"
        
    pixels = img.pixels
    num_pixels = len(pixels) // 4
    if num_pixels == 0:
        bpy.data.images.remove(img)
        return "albedo"
        
    sum_r = sum_g = sum_b = 0
    for idx in range(num_pixels):
        p_idx = idx * 4
        sum_r += pixels[p_idx]
        sum_g += pixels[p_idx + 1]
        sum_b += pixels[p_idx + 2]
        
    avg_r = sum_r / num_pixels
    avg_g = sum_g / num_pixels
    avg_b = sum_b / num_pixels
    
    # Clean up loaded analysis image
    bpy.data.images.remove(img)
    
    # 1. Roughness/Metallic Map: ORM cyan layout (high green & blue, extremely low red)
    if avg_r < 0.04 and avg_g > 0.70 and avg_b > 0.70:
        return "roughness"
        
    # 2. Normal Maps: High blue, medium green, low red (BC5/RG custom normal packing)
    if avg_b > 0.62 and avg_g > 0.39 and avg_r < 0.39:
        return "normal"
        
    # 3. Emissive Map: pure yellow/mask on dark (high red & green, low blue)
    if avg_b < 0.08 and avg_r > 0.30 and avg_g > 0.30:
        return "emissive"
        
    # 4. Grayscale Map Classification (Metallic vs Roughness fallback)
    is_grayscale = abs(avg_r - avg_g) < 0.03 and abs(avg_g - avg_b) < 0.03
    if is_grayscale:
        # Metallic is mostly black (very low average)
        if avg_r < 0.18:
            return "metallic"
        else:
            return "roughness"
            
    # 5. Default fallback is Albedo
    return "albedo"

# ==============================================================================
# MATERIAL CREATION
# ==============================================================================
def create_true_evr_material(mat_name, group_bindings, mapping_textures, resolved_textures, classified_textures):
    """Creates a material using the exact PBR bindings from game metadata, resolved dynamically by texture classification."""
    mat = bpy.data.materials.new(name=mat_name)
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()
    
    # Create BSDF and Output
    bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')
    bsdf.location = (200, 100)
    
    output = nodes.new(type='ShaderNodeOutputMaterial')
    output.location = (500, 100)
    links.new(bsdf.outputs['BSDF'], output.inputs['Surface'])
    
    # UV Map node
    uv_node = nodes.new(type='ShaderNodeUVMap')
    uv_node.uv_map = "UVMap"
    uv_node.location = (-600, 400)
    
    # Dynamically classify and resolve texture roles in this slot group
    p_base = None
    p_normal = None
    p_roughness = None
    p_emissive = None

    for slot_in_group, bind in group_bindings.items():
        tex_idx = bind["texture_idx"]
        if tex_idx < 0 or tex_idx >= len(mapping_textures):
            continue
        tex_hash = mapping_textures[tex_idx]
        tex_path = resolved_textures.get(tex_hash)
        if not tex_path:
            continue
            
        role = classified_textures.get(tex_hash, "albedo")
        if role == "albedo":
            p_base = tex_path
        elif role == "normal":
            p_normal = tex_path
        elif role in ("roughness", "metallic"):
            p_roughness = tex_path
        elif role == "emissive":
            p_emissive = tex_path

    # 1. Base Color
    node_albedo = None
    if p_base:
        node_albedo = nodes.new(type='ShaderNodeTexImage')
        node_albedo.location = (-300, 400)
        node_albedo.image = bpy.data.images.load(p_base)
        node_albedo.image.colorspace_settings.name = 'sRGB'
        links.new(uv_node.outputs['UV'], node_albedo.inputs['Vector'])
        links.new(node_albedo.outputs['Color'], bsdf.inputs['Base Color'])
    
    # 2. Normal Map
    node_normal_img = None
    if p_normal:
        node_normal_img = nodes.new(type='ShaderNodeTexImage')
        node_normal_img.location = (-300, 100)
        node_normal_img.image = bpy.data.images.load(p_normal)
        node_normal_img.image.colorspace_settings.name = 'Non-Color'
        node_normal_map = nodes.new(type='ShaderNodeNormalMap')
        node_normal_map.location = (0, -100)
        links.new(uv_node.outputs['UV'], node_normal_img.inputs['Vector'])
        links.new(node_normal_img.outputs['Color'], node_normal_map.inputs['Color'])
        links.new(node_normal_map.outputs['Normal'], bsdf.inputs['Normal'])
        
    # 3. Roughness Map
    if p_roughness:
        node_rough = nodes.new(type='ShaderNodeTexImage')
        node_rough.location = (-300, -200)
        node_rough.image = bpy.data.images.load(p_roughness)
        node_rough.image.colorspace_settings.name = 'Non-Color'
        links.new(uv_node.outputs['UV'], node_rough.inputs['Vector'])
        links.new(node_rough.outputs['Color'], bsdf.inputs['Roughness'])
    else:
        bsdf.inputs['Roughness'].default_value = 0.5
        
    # 4. Emissive Map
    if p_emissive:
        node_emissive = nodes.new(type='ShaderNodeTexImage')
        node_emissive.location = (-300, -500)
        node_emissive.image = bpy.data.images.load(p_emissive)
        node_emissive.image.colorspace_settings.name = 'sRGB'
        links.new(uv_node.outputs['UV'], node_emissive.inputs['Vector'])
        links.new(node_emissive.outputs['Color'], bsdf.inputs['Emission Color'])
        
        # Emissive Mask (using the inverse of the Normal Map's Alpha channel if present)
        if p_normal and node_normal_img:
            node_map = nodes.new(type='ShaderNodeMapRange')
            node_map.location = (0, 100)
            node_map.inputs['From Min'].default_value = 0.0
            node_map.inputs['From Max'].default_value = 0.9
            node_map.inputs['To Min'].default_value = 5.0
            node_map.inputs['To Max'].default_value = 0.0
            links.new(node_normal_img.outputs['Alpha'], node_map.inputs['Value'])
            links.new(node_map.outputs['Result'], bsdf.inputs['Emission Strength'])
        else:
            bsdf.inputs['Emission Strength'].default_value = 5.0
    else:
        bsdf.inputs['Emission Strength'].default_value = 0.0
        
    return mat

# ==============================================================================
# UI NOTIFICATION POPUP
# ==============================================================================
def show_message_box(message="", title="EVR Texture Loader", icon='INFO'):
    """Native Blender popup message box for immediate visual user feedback."""
    if bpy.app.background:
        return
    def draw(self, context):
        self.layout.label(text=message)
    bpy.context.window_manager.popup_menu(draw, title=title, icon=icon)

# ==============================================================================
# MAIN EXECUTION LOOP
# ==============================================================================
def main():
    print("\n" + "="*80)
    print("STARTING ECHO VR RAW MESH & PBR TEXTURE LOADER...")
    print("="*80)
    
    active_obj = bpy.context.active_object
    
    # 1. Resolve Model Hash
    model_hash = CONFIG["MODEL_HASH"]
    if not model_hash or model_hash.lower() == "auto":
        detected = get_model_hash_from_active()
        if detected:
            model_hash = detected
            print(f"[+] Auto-detected model hash from selected object: {model_hash}")
        elif active_obj and len(active_obj.name.split('.')[0]) == 16:
            model_hash = active_obj.name.split('.')[0]
            print(f"[+] Auto-detected model hash from selection prefix: {model_hash}")
        else:
            model_hash = "8b8acfe2c82f6ffb" # Fallback to chassis
            print(f"[!] No active selected object with a valid hex name. Falling back to chassis hash: {model_hash}")
            
    # 2. Discover Paths
    paths = discover_paths()
    if not paths["pcvr_extracted"]:
        msg = "Error: Could not locate pcvr-extracted directory."
        print(f"[-] {msg}")
        show_message_box(msg, "Path Error", 'ERROR')
        return
    if not paths["texture_cache"]:
        msg = "Error: Could not locate texture_cache directory."
        print(f"[-] {msg}")
        show_message_box(msg, "Path Error", 'ERROR')
        return
        
    print(f"[+] pcvr-extracted path: {paths['pcvr_extracted']}")
    print(f"[+] texture_cache path:   {paths['texture_cache']}")
    
    # 3. Parse Materials Mapping
    mapping = parse_materials_mapping(paths["pcvr_extracted"], model_hash)
    if not mapping:
        msg = f"Error: Failed to parse materials mapping for {model_hash}."
        print(f"[-] {msg}")
        show_message_box(msg, "Mapping Error", 'ERROR')
        return
        
    # 4. Classify and Resolve Unique Textures on Disk
    resolved_textures = {} # hex_hash -> file_path
    classified_textures = {} # hex_hash -> type ("albedo", "normal", "roughness", "metallic")
    
    for tex_hash in set(mapping["textures"]):
        vars = get_all_name_variations(tex_hash)
        found_path = None
        for v in vars:
            png_path = os.path.join(paths["texture_cache"], f"{v}.png")
            if os.path.exists(png_path):
                if os.path.getsize(png_path) >= 500:
                    found_path = png_path
                    break
        if found_path:
            resolved_textures[tex_hash] = found_path
            # Analyze pixels to get correct PBR type
            tex_type = classify_texture_by_pixels(found_path)
            classified_textures[tex_hash] = tex_type
        else:
            print(f"  [!] Warning: Texture {tex_hash} not found in cache folder (tried variations: {vars}).")
            
    print(f"[+] Resolved {len(resolved_textures)} unique high-res textures from game cache:")
    for h, p in resolved_textures.items():
        print(f"  - {h}: {classified_textures[h].upper()} ({os.path.basename(p)})")
        
    # 5. Import Geometry / Select Target Objects
    target_objects = []
    
    if CONFIG["MODE"] == "IMPORT_AND_TEXTURE":
        # Resolve GPU filepath
        gpu_path = CONFIG["GPU_BINARY_PATH"]
        if not gpu_path:
            # Auto-locate in pcvr-extracted Cosmetics or Prop database folder, or evr-mesh-importer-main models
            gpu_folders = ["e7a8ab5ceaef49cb", "e642bfb1abcf76df", "CGInstancedModelResource", "CGMeshListResource"]
            candidates = []
            
            # Generate all spelling variations of folder and model hash to handle hex/signed/unsigned mix
            model_vars = get_all_name_variations(model_hash)
            
            # Collect all candidate base search directories in prioritized order
            search_dirs = []
            models_gpu_base = r"J:\EchoVR-Tools-Launcher\evr-mesh-importer-main\models\GPU"
            if os.path.exists(models_gpu_base):
                search_dirs.append(models_gpu_base)
                
            search_dirs.append(r"G:\pcvr-extracted")
            
            if paths["pcvr_extracted"]:
                search_dirs.append(paths["pcvr_extracted"])
                
            search_dirs.append(r"J:\EchoVR-Tools-Launcher\Tools\Settings\pcvr-extracted")
            
            for sd in search_dirs:
                if not os.path.exists(sd):
                    continue
                for gf in gpu_folders:
                    folder_vars = get_all_name_variations(gf)
                    for fv in folder_vars:
                        # Also check the folder name directly (e.g. "CGMeshListResource")
                        for f_name in {fv, gf}:
                            for mv in model_vars:
                                candidates.append(os.path.join(sd, f_name, mv))
                                
            for c in candidates:
                if os.path.exists(c):
                    gpu_path = c
                    break
                    
        if not gpu_path or not os.path.exists(gpu_path):
            msg = f"Error: Could not locate GPU binary for model {model_hash}."
            print(f"[-] {msg}")
            show_message_box(msg, "Import Error", 'ERROR')
            return
            
        print(f"[+] Programmatically importing EVR Mesh: {gpu_path}")
        
        # Call the importer addon operator
        try:
            # CRITICAL FIX: Pass both directory and files to override any stale window manager selection buffers!
            bpy.ops.import_mesh.evr_raw(
                directory=os.path.dirname(gpu_path),
                files=[{"name": os.path.basename(gpu_path)}]
            )
            # Find imported mesh objects
            base_name = os.path.splitext(os.path.basename(gpu_path))[0]
            vars = get_all_name_variations(base_name)
            target_objects = []
            for obj in bpy.context.scene.objects:
                if obj.type == 'MESH':
                    obj_base = obj.name.split('.')[0]
                    if obj_base.lower() in vars:
                        target_objects.append(obj)
            # Sort by name index to match material slots sequentially
            target_objects.sort(key=lambda o: o.name)
            print(f"[+] Imported {len(target_objects)} mesh objects for model.")
        except Exception as e:
            msg = f"Importer execution failed: {e}"
            print(f"[-] {msg}")
            show_message_box(msg, "Import Error", 'ERROR')
            return
            
    else: # MODE == 'TEXTURE_ACTIVE'
        if not active_obj:
            msg = "Error: No active object selected in Blender. Select your mesh or empty axis first!"
            print(f"[-] {msg}")
            show_message_box(msg, "Selection Error", 'ERROR')
            return
        
        base_name = active_obj.name.split('.')[0]
        
        # Smart Selection: support selecting parent Empty or any submesh LOD
        vars = get_all_name_variations(base_name)
        if active_obj.type == 'EMPTY':
            # If selecting parent empty, gather all its child meshes
            target_objects = [child for child in active_obj.children if child.type == 'MESH']
            # Fallback if empty parent has no child meshes directly: find meshes matching variations
            if not target_objects:
                for obj in bpy.context.scene.objects:
                    if obj.type == 'MESH':
                        obj_base = obj.name.split('.')[0]
                        if obj_base.lower() in vars:
                            target_objects.append(obj)
            print(f"[+] Active selection is parent Empty. Found {len(target_objects)} child submeshes.")
        else:
            # Active is one of the submeshes, gather its siblings
            target_objects = []
            for obj in bpy.context.scene.objects:
                if obj.type == 'MESH':
                    obj_base = obj.name.split('.')[0]
                    if obj_base.lower() in vars:
                        target_objects.append(obj)
            # If submesh belongs to parent Empty, ensure we gather all siblings
            if active_obj.parent and active_obj.parent.type == 'EMPTY':
                for child in active_obj.parent.children:
                    if child.type == 'MESH' and child not in target_objects:
                        target_objects.append(child)
                        
        target_objects.sort(key=lambda o: o.name)
        
        # UV coordinates are already correctly flipped by the evr_mesh_importer addon during geometry creation
        pass
                        
        print(f"[+] Applying PBR materials to {len(target_objects)} target mesh submeshes under '{base_name}'")
        
    if not target_objects:
        msg = f"Error: No target submeshes found for base name '{base_name}'."
        print(f"[-] {msg}")
        show_message_box(msg, "Geometry Error", 'ERROR')
        return
        
    # 6. Create Material Presets from database bindings
    groups = {}
    for bind in mapping["bindings"]:
        g_idx = bind["slot_idx"] // 4
        slot_in_group = bind["slot_idx"] % 4
        if g_idx not in groups:
            groups[g_idx] = {}
        groups[g_idx][slot_in_group] = bind
        
    created_materials = []
    print(f"[+] Creating {len(groups)} material skins/presets from database bindings...")
    for g_idx in sorted(groups.keys()):
        mat_name = f"{model_hash}_Skin_{g_idx}"
        mat = create_true_evr_material(mat_name, groups[g_idx], mapping["textures"], resolved_textures, classified_textures)
        created_materials.append(mat)
        print(f"  - Created {mat_name}")
    
    # 7. Assign constructed materials to target submesh/LOD objects
    print("[+] Assigning material presets to mesh objects...")
    if len(target_objects) == 1:
        obj = target_objects[0]
        obj.data.materials.clear()
        for mat in created_materials:
            obj.data.materials.append(mat)
        print(f"  - Appended all {len(created_materials)} materials to combined object {obj.name}")
    else:
        for idx, obj in enumerate(target_objects):
            obj.data.materials.clear()
            for mat in created_materials:
                obj.data.materials.append(mat)
            
            # Use logical material index stored on object
            mat_idx = obj.get("evr_material_index", idx)
            if mat_idx >= len(created_materials):
                mat_idx = 0
            
            mat = created_materials[mat_idx]
            obj.active_material = mat
            
            # Map all faces to the correct material index slot
            for poly in obj.data.polygons:
                poly.material_index = mat_idx
                
            print(f"  - Assigned corresponding material skin {mat_idx} ({mat.name}) to submesh: {obj.name}")
            
    # 8. Switch 3D viewport shading mode to Material Preview to display textures instantly across all screens/workspaces
    shading_switched = False
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type == 'VIEW_3D':
                for space in area.spaces:
                    if space.type == 'VIEW_3D':
                        space.shading.type = 'MATERIAL'
                        shading_switched = True
                    
    if shading_switched:
        print("[+] Switched all 3D viewports across all workspaces to Material Preview shading mode.")
        
    success_msg = f"Success! Created {len(created_materials)} blend order presets. Select them in the Material panel to test!"
    print(f"\n[+] {success_msg}")
    show_message_box(success_msg, "Success!", 'CHECKMARK')
if __name__ == "__main__":
    main()
