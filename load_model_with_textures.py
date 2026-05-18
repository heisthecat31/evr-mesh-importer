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

# ==============================================================================
# CONFIGURATION
# ==============================================================================
CONFIG = {
    # The hex hash of the model you want to import and texture
    # e.g., "8b8acfe2c82f6ffb" (Robot Chassis) or "7c26ec719a14a2a8" (Booster/Other)
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
    
    config_loaded = False
    for cp in potential_config_paths:
        if os.path.exists(cp):
            try:
                with open(cp, "r") as f:
                    cfg = json.load(f)
                paths["pcvr_extracted"] = cfg.get("extracted_folder") or cfg.get("output_folder")
                # Deduce texture cache from oculus folder or tools directory
                data_folder = cfg.get("data_folder")
                if data_folder:
                    # e.g., C:\Oculus\Games\Software\...\ready-at-dawn-echo-arena\_data\...\win10
                    # texture_cache is typically in ready-at-dawn-echo-arena\bin\win10\Tools\Tools\Settings\texture_cache
                    oculus_base = os.path.dirname(os.path.dirname(data_folder))
                    tc = os.path.join(oculus_base, "bin", "win10", "Tools", "Tools", "Settings", "texture_cache")
                    if os.path.exists(tc):
                        paths["texture_cache"] = tc
                config_loaded = True
                print(f"[+] Loaded config from: {cp}")
                break
            except Exception as e:
                print(f"[-] Error loading config {cp}: {e}")
                
    # 2. Fallbacks
    if not paths["pcvr_extracted"] or not os.path.exists(paths["pcvr_extracted"]):
        fallbacks = [
            r"G:\pcvr-extracted",
            r"J:\EchoVR-Tools-Launcher\Tools\Settings\pcvr-extracted"
        ]
        for fb in fallbacks:
            if os.path.exists(fb):
                paths["pcvr_extracted"] = fb
                break
                
    if not paths["texture_cache"] or not os.path.exists(paths["texture_cache"]):
        fallbacks = [
            r"C:\Oculus\Games\Software\Software\ready-at-dawn-echo-arena\bin\win10\Tools\Tools\Settings\texture_cache",
            r"C:\Oculus\Games\Software\Software\ready-at-dawn-echo-arena\bin\win10\Tools\Settings\texture_cache",
            r"J:\EchoVR-Tools-Launcher\Tools\Settings\texture_cache"
        ]
        for fb in fallbacks:
            if os.path.exists(fb):
                paths["texture_cache"] = fb
                break
                
    # 3. User overrides
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
    # Find mapping file: can be hex folder or decimal folder
    meta_folder_hex = "c2434c5a99e139ce"
    meta_folder_dec = hex_to_signed_decimal(meta_folder_hex)
    model_dec = hex_to_signed_decimal(model_hash)
    
    candidates = [
        os.path.join(pcvr_extracted_dir, meta_folder_hex, model_hash),
        os.path.join(pcvr_extracted_dir, meta_folder_dec, model_dec),
    ]
    
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
    except Exception as e:
        print(f"[-] Could not load {filepath} for analysis: {e}")
        return "albedo"
        
    pixels = img.pixels
    num_pixels = len(pixels) // 4
    if num_pixels == 0:
        bpy.data.images.remove(img)
        return "albedo"
        
    # Sample every Nth pixel for fast execution
    step = max(1, num_pixels // 500)
    sum_r = sum_g = sum_b = 0
    samples = 0
    for idx in range(0, num_pixels, step):
        p_idx = idx * 4
        sum_r += pixels[p_idx]
        sum_g += pixels[p_idx + 1]
        sum_b += pixels[p_idx + 2]
        samples += 1
        
    avg_r = sum_r / samples
    avg_g = sum_g / samples
    avg_b = sum_b / samples
    
    # Clean up loaded analysis image
    bpy.data.images.remove(img)
    
    # 1. Normal Maps: High blue, medium red & green (R ~ 0.5, G ~ 0.5, B ~ 1.0)
    if avg_b > 0.75 and 0.4 < avg_r < 0.65 and 0.4 < avg_g < 0.65:
        return "normal"
        
    # 2. Grayscale Map Classification (Metallic vs Roughness)
    is_grayscale = abs(avg_r - avg_g) < 0.03 and abs(avg_g - avg_b) < 0.03
    if is_grayscale:
        # Metallic is mostly black (very low average)
        if avg_r < 0.18:
            return "metallic"
        else:
            return "roughness"
            
    # 3. Default fallback is Albedo
    return "albedo"

# ==============================================================================
# MATERIAL CREATION
# ==============================================================================
def create_pbr_material(mat_name, texture_paths):
    """Creates a beautiful Principled BSDF node material with texture connections."""
    mat = bpy.data.materials.new(name=mat_name)
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()
    
    # 1. Create BSDF and Output nodes
    bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')
    bsdf.location = (200, 100)
    
    output = nodes.new(type='ShaderNodeOutputMaterial')
    output.location = (500, 100)
    links.new(bsdf.outputs['BSDF'], output.inputs['Surface'])
    
    # 2. Add Albedo Texture
    if "albedo" in texture_paths:
        tex_node = nodes.new(type='ShaderNodeTexImage')
        tex_node.location = (-200, 300)
        tex_node.label = "Albedo (Base Color)"
        img = bpy.data.images.load(texture_paths["albedo"])
        img.colorspace_settings.name = 'sRGB'
        tex_node.image = img
        links.new(tex_node.outputs['Color'], bsdf.inputs['Base Color'])
        
    # 3. Add Metallic Texture
    if "metallic" in texture_paths:
        tex_node = nodes.new(type='ShaderNodeTexImage')
        tex_node.location = (-200, 50)
        tex_node.label = "Metallic"
        img = bpy.data.images.load(texture_paths["metallic"])
        img.colorspace_settings.name = 'Non-Color'
        tex_node.image = img
        links.new(tex_node.outputs['Color'], bsdf.inputs['Metallic'])
        
    # 4. Add Roughness Texture
    if "roughness" in texture_paths:
        tex_node = nodes.new(type='ShaderNodeTexImage')
        tex_node.location = (-200, -200)
        tex_node.label = "Roughness"
        img = bpy.data.images.load(texture_paths["roughness"])
        img.colorspace_settings.name = 'Non-Color'
        tex_node.image = img
        links.new(tex_node.outputs['Color'], bsdf.inputs['Roughness'])
        
    # 5. Add Normal Map Texture
    if "normal" in texture_paths:
        tex_node = nodes.new(type='ShaderNodeTexImage')
        tex_node.location = (-400, -450)
        tex_node.label = "Normal Image"
        img = bpy.data.images.load(texture_paths["normal"])
        img.colorspace_settings.name = 'Non-Color'
        tex_node.image = img
        
        normal_map = nodes.new(type='ShaderNodeNormalMap')
        normal_map.location = (-100, -450)
        
        links.new(tex_node.outputs['Color'], normal_map.inputs['Color'])
        links.new(normal_map.outputs['Normal'], bsdf.inputs['Normal'])
        
    return mat

# ==============================================================================
# MAIN EXECUTION LOOP
# ==============================================================================
def main():
    print("\n" + "="*80)
    print("STARTING ECHO VR RAW MESH & PBR TEXTURE LOADER...")
    print("="*80)
    
    # 1. Discover Paths
    paths = discover_paths()
    if not paths["pcvr_extracted"]:
        print("[-] Error: Could not locate pcvr-extracted directory.")
        return
    if not paths["texture_cache"]:
        print("[-] Error: Could not locate texture_cache directory.")
        return
        
    print(f"[+] pcvr-extracted path: {paths['pcvr_extracted']}")
    print(f"[+] texture_cache path:   {paths['texture_cache']}")
    
    # 2. Parse Materials Mapping
    mapping = parse_materials_mapping(paths["pcvr_extracted"], CONFIG["MODEL_HASH"])
    if not mapping:
        print("[-] Error: Failed to parse materials mapping.")
        return
        
    # 3. Classify and Resolve Unique Textures on Disk
    resolved_textures = {} # hex_hash -> file_path
    classified_textures = {} # hex_hash -> type ("albedo", "normal", "roughness", "metallic")
    
    for tex_hash in set(mapping["textures"]):
        png_path = os.path.join(paths["texture_cache"], f"{tex_hash}.png")
        if os.path.exists(png_path):
            resolved_textures[tex_hash] = png_path
            # Analyze pixels to get correct PBR type
            tex_type = classify_texture_by_pixels(png_path)
            classified_textures[tex_hash] = tex_type
        else:
            print(f"  [!] Warning: Texture {tex_hash}.png not found in cache folder.")
            
    print(f"[+] Resolved {len(resolved_textures)} unique textures from game cache:")
    for h, p in resolved_textures.items():
        print(f"  - {h}: {classified_textures[h].upper()} ({os.path.basename(p)})")
        
    # 4. Import Geometry (if in import mode)
    target_objects = []
    
    if CONFIG["MODE"] == "IMPORT_AND_TEXTURE":
        # Resolve GPU filepath
        gpu_path = CONFIG["GPU_BINARY_PATH"]
        if not gpu_path:
            # Auto-locate in pcvr-extracted GPU folder
            gpu_folder_hex = "e7a8ab5ceaef49cb"
            gpu_folder_dec = hex_to_signed_decimal(gpu_folder_hex)
            candidates = [
                os.path.join(paths["pcvr_extracted"], gpu_folder_hex, CONFIG["MODEL_HASH"]),
                os.path.join(paths["pcvr_extracted"], gpu_folder_dec, hex_to_signed_decimal(CONFIG["MODEL_HASH"])),
            ]
            for c in candidates:
                if os.path.exists(c):
                    gpu_path = c
                    break
                    
        if not gpu_path or not os.path.exists(gpu_path):
            print(f"[-] Error: Could not locate GPU binary file for model {CONFIG['MODEL_HASH']}.")
            return
            
        print(f"[+] Programmatically importing EVR Mesh: {gpu_path}")
        
        # Call the importer addon operator
        try:
            bpy.ops.import_mesh.evr_raw(filepath=gpu_path)
            # Find imported mesh objects
            # When imported, objects are named <model_hash> or <model_hash>.<idx>
            base_name = os.path.splitext(os.path.basename(gpu_path))[0]
            target_objects = [
                obj for obj in bpy.context.scene.objects 
                if obj.type == 'MESH' and (obj.name == base_name or obj.name.startswith(base_name + "."))
            ]
            # Sort by name index to match material slots sequentially
            target_objects.sort(key=lambda o: o.name)
            print(f"[+] Imported {len(target_objects)} mesh objects for model.")
        except Exception as e:
            print(f"[-] Importer execution failed: {e}")
            print("[!] Make sure the 'EVR Raw Mesh Importer' addon is installed and enabled in Blender.")
            return
            
    else: # MODE == 'TEXTURE_ACTIVE'
        active_obj = bpy.context.active_object
        if not active_obj or active_obj.type != 'MESH':
            print("[-] Error: No active mesh object selected in Blender.")
            return
        target_objects = [active_obj]
        print(f"[+] Applying textures to active selected object: {active_obj.name}")
        
    if not target_objects:
        print("[-] Error: No target objects found to apply materials.")
        return
        
    # 5. Group bindings into Materials (4 bindings per material slot)
    # Typically, slot 0-3 = Mat 0, 4-7 = Mat 1, 8-11 = Mat 2, etc.
    num_materials = len(mapping["bindings"]) // 4
    print(f"[+] Creating {num_materials} PBR materials...")
    
    created_materials = []
    for mat_idx in range(num_materials):
        # Gather the 4 texture slots for this material
        mat_tex_paths = {}
        for sub_slot in range(4):
            binding_idx = mat_idx * 4 + sub_slot
            binding = mapping["bindings"][binding_idx]
            tex_idx = binding["texture_idx"]
            
            # Map texture index to our resolved PNG
            if 0 <= tex_idx < len(mapping["textures"]):
                tex_hash = mapping["textures"][tex_idx]
                if tex_hash in resolved_textures:
                    tex_type = classified_textures[tex_hash]
                    mat_tex_paths[tex_type] = resolved_textures[tex_hash]
                    
        # Construct Blender material
        mat_name = f"{CONFIG['MODEL_HASH']}_Material_{mat_idx:02d}"
        mat = create_pbr_material(mat_name, mat_tex_paths)
        created_materials.append(mat)
        print(f"  - Material {mat_idx:2d} ({mat_name}) configured with {list(mat_tex_paths.keys())}")
        
    # 6. Assign Materials to target submesh objects
    print("[+] Assigning materials to mesh objects...")
    for idx, obj in enumerate(target_objects):
        # Clear existing materials
        obj.data.materials.clear()
        
        # Determine material to assign
        # If there are fewer mesh objects than materials, match 1-to-1.
        # If there is only one combined object, append all materials to slots.
        if len(target_objects) == 1:
            for mat in created_materials:
                obj.data.materials.append(mat)
            print(f"  - Appended all {len(created_materials)} materials to combined object {obj.name}")
        else:
            mat_idx = idx % len(created_materials)
            mat = created_materials[mat_idx]
            obj.data.materials.append(mat)
            print(f"  - Assigned {mat.name} to submesh {obj.name}")
            
    print("\n" + "="*80)
    print("SUCCESS: Model import and material texturing complete!")
    print("="*80)

if __name__ == "__main__":
    main()
