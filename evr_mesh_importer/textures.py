import os
import json
import struct
import math
import bpy

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
    for folder_hex in ("23d48cecc462abe7", "c2434c5a99e139ce"):
        hex_mapping = os.path.join(path, folder_hex)
        dec_mapping = os.path.join(path, hex_to_signed_decimal(folder_hex))
        unsigned_mapping = os.path.join(path, str(int(folder_hex, 16)))
        if os.path.exists(hex_mapping) or os.path.exists(dec_mapping) or os.path.exists(unsigned_mapping):
            return True
    return False

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

def discover_paths():
    """Auto-discovers the game extraction directories using config.json or fallback paths.
    DEPRECATED: We now rely entirely on user UI input and the addon's local texture/ directory.
    """
    return {
        "pcvr_extracted": None,
        "texture_cache": None,
    }

# ==============================================================================
# METADATA PARSING
# ==============================================================================
def parse_materials_mapping(pcvr_extracted_dir, model_hash):
    """Parses Echo material mapping files for texture hashes and bindings."""
    meta_folder_hexes = (
        "23d48cecc462abe7",  # Summer build model-texture mappings
        "c2434c5a99e139ce",  # Live PCVR model-texture mappings
    )
    
    model_vars = get_all_name_variations(model_hash)
    
    candidates = []
    for meta_folder_hex in meta_folder_hexes:
        meta_vars = get_all_name_variations(meta_folder_hex)
        for mf in meta_vars:
            for mv in model_vars:
                candidates.append(os.path.join(pcvr_extracted_dir, mf, mv))
            
    mapping_path = None
    for c in candidates:
        if os.path.exists(c):
            mapping_path = c
            break
            
    # Heuristic fallback: Search all folders in pcvr_extracted_dir if not found in fast paths
    if not mapping_path:
        print(f"[*] Material mapping not found in known folders, running heuristic search in {pcvr_extracted_dir}...")
        try:
            for folder in os.listdir(pcvr_extracted_dir):
                folder_path = os.path.join(pcvr_extracted_dir, folder)
                if not os.path.isdir(folder_path):
                    continue
                for mv in model_vars:
                    candidate = os.path.join(folder_path, mv)
                    if os.path.exists(candidate):
                        # Verify structural integrity of the file
                        try:
                            with open(candidate, "rb") as fh:
                                data = fh.read()
                            if len(data) >= 12:
                                tex_count = struct.unpack_from("<I", data, 8)[0]
                                if 0 < tex_count <= 1000:
                                    expected_size = 12 + (tex_count * 8)
                                    if len(data) >= expected_size:
                                        rem_offset = expected_size
                                        if rem_offset % 8 != 0:
                                            rem_offset += 4
                                        if len(data) >= rem_offset + 4:
                                            slot_count = struct.unpack_from("<I", data, rem_offset)[0]
                                            if 0 <= slot_count <= 1000 and len(data) >= rem_offset + 4 + (slot_count * 8):
                                                mapping_path = candidate
                                                print(f"[+] Heuristically discovered mapping file: {mapping_path}")
                                                break
                        except Exception:
                            pass
                if mapping_path:
                    break
        except Exception as e:
            print(f"[-] Heuristic search failed: {e}")

    if not mapping_path:
        print(f"[-] Materials mapping file not found for model: {model_hash}")
        return None
        
    print(f"[+] Found Materials Mapping File: {mapping_path}")
    with open(mapping_path, "rb") as fh:
        data = fh.read()
        
    tex_count = struct.unpack_from("<I", data, 8)[0]
    
    texture_hashes = []
    for i in range(tex_count):
        offset = 12 + i * 8
        tex_hash = struct.unpack_from("<Q", data, offset)[0]
        texture_hashes.append(f"{tex_hash:016x}")
        
    rem_offset = 12 + tex_count * 8
    if rem_offset % 8 != 0:
        rem_offset += 4
        
    slot_count = struct.unpack_from("<I", data, rem_offset)[0]
    
    raw_bindings = []
    rem_offset += 4 # skip slot_count
    
    raw_bindings = []
    for i in range(slot_count):
        off = rem_offset + i * 8
        raw_bindings.append(data[off:off + 8])

    decoded_bindings = []
    for raw in raw_bindings:
        tex_idx, skin_id = struct.unpack_from("<II", raw)
        decoded_bindings.append((skin_id, tex_idx))

    bindings = []
    for i, (skin_id, tex_idx) in enumerate(decoded_bindings):
        bindings.append({
            "slot_idx": i,
            "skin_id": skin_id,
            "texture_idx": tex_idx
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
        
    num_pixels = len(img.pixels) // 4
    if num_pixels == 0:
        bpy.data.images.remove(img)
        return "albedo"
        
    # Sample up to ~4096 pixels evenly across the image for fast analysis
    # without using img.scale() which destroys colors via black background interpolation
    stride = max(1, num_pixels // 4096)
    
    import array
    # Very fast buffer copy using array to avoid multi-second tuple unpacking
    pixels = array.array('f', [0.0] * (num_pixels * 4))
    img.pixels.foreach_get(pixels)
    
    sum_r = sum_g = sum_b = 0
    max_r = max_g = max_b = 0
    valid_pixels = 0
    
    for i in range(0, num_pixels, stride):
        p_idx = i * 4
        r, g, b = pixels[p_idx], pixels[p_idx + 1], pixels[p_idx + 2]
        a = pixels[p_idx + 3]
        
        # Ignore fully transparent or pure black pixels for classification averages
        if a < 0.05 or (r < 0.05 and g < 0.05 and b < 0.05):
            continue
            
        sum_r += r
        sum_g += g
        sum_b += b
        valid_pixels += 1
        if r > max_r: max_r = r
        if g > max_g: max_g = g
        if b > max_b: max_b = b
        
    if valid_pixels == 0:
        bpy.data.images.remove(img)
        return "albedo"
        
    avg_r = sum_r / valid_pixels
    avg_g = sum_g / valid_pixels
    avg_b = sum_b / valid_pixels
    
    bpy.data.images.remove(img)
    
    # 1. 2-Channel RG Normal Map (BC5 / ATI2)
    # Average B is very low, R and G are centered around 0.5 (representing vector x/y centered at 0.5)
    if avg_b < 0.05 and 0.35 < avg_r < 0.65 and 0.35 < avg_g < 0.65:
        return "normal_rg"
        
    # 2. Standard 3-Channel Normal Map (Blue)
    if avg_b > 0.55 and 0.35 < avg_r < 0.65 and 0.35 < avg_g < 0.65:
        return "normal"
        
    # 3. ORM (Roughness/Metallic) Map: High Green & Blue, extremely low/0 Red
    if avg_r < 0.05 and avg_g > 0.40 and avg_b > 0.40:
        return "roughness"
        
    # 4. Emissive Map: Low average brightness across the texture, but has bright glowing spots
    avg_brightness = (avg_r + avg_g + avg_b) / 3.0
    if avg_brightness < 0.15 and (max_r > 0.15 or max_g > 0.15 or max_b > 0.15):
        return "emissive"
        
    # 5. Grayscale Map Classification
    is_grayscale = abs(avg_r - avg_g) < 0.03 and abs(avg_g - avg_b) < 0.03
    if is_grayscale:
        if avg_r < 0.18:
            return "metallic"
        else:
            return "roughness"
            
    return "albedo"

# ==============================================================================
# MATERIAL CREATION
# ==============================================================================
def create_true_evr_material(mat_name, group_bindings, mapping_textures, resolved_textures, classified_textures, scale=1.0):
    """Creates a material using Echo's binding slot order.

    The binding order is meaningful. Pixel classification is useful as a
    fallback, but using it as the primary role selector can put a normal/detail
    map into Base Color. Echo material groups are normally:
        slot 0 = base color
        slot 1 = normal
        slot 2 = packed roughness/material data
        slot 3 = emissive/detail
    """
    mat = bpy.data.materials.new(name=mat_name)
    mat.use_nodes = True
    
    mat.blend_method = 'OPAQUE'
    mat.use_transparent_shadow = False
    
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()
    
    # Create BSDF and Output
    bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')
    bsdf.location = (200, 100)
    
    bsdf.inputs['Alpha'].default_value = 1.0
        
    output = nodes.new(type='ShaderNodeOutputMaterial')
    output.location = (500, 100)
    links.new(bsdf.outputs['BSDF'], output.inputs['Surface'])
    
    # UV Map node
    uv_node = nodes.new(type='ShaderNodeUVMap')
    uv_node.uv_map = "UVMap"
    uv_node.location = (-600, 400)
    
    def path_for_slot(slot_in_group):
        bind = group_bindings.get(slot_in_group)
        if not bind:
            return None, None
        tex_idx = bind["texture_idx"]
        if tex_idx < 0 or tex_idx >= len(mapping_textures):
            return None, None
        tex_hash = mapping_textures[tex_idx]
        return resolved_textures.get(tex_hash), classified_textures.get(tex_hash, "albedo")

    p_base, _base_role = path_for_slot(0)
    p_normal, normal_role = path_for_slot(1)
    p_roughness, _roughness_role = path_for_slot(2)
    p_emissive, _emissive_role = path_for_slot(3)
    is_normal_rg = normal_role == "normal_rg"
    print(
        f"[EVR Material] {mat_name}: "
        f"base={os.path.basename(p_base) if p_base else 'None'}, "
        f"normal={os.path.basename(p_normal) if p_normal else 'None'}, "
        f"packed={os.path.basename(p_roughness) if p_roughness else 'None'}, "
        f"emissive={os.path.basename(p_emissive) if p_emissive else 'None'}"
    )

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
        
        if is_normal_rg:
            # Reconstruct Z = sqrt(max(0, 1 - x^2 - y^2)) where x = 2R-1, y = 2G-1.
            sep = nodes.new(type='ShaderNodeSeparateColor')
            sep.location = (-150, -50)
            
            map_r = nodes.new(type='ShaderNodeMapRange')
            map_r.location = (50, 100)
            map_r.inputs['From Min'].default_value = 0.0
            map_r.inputs['From Max'].default_value = 1.0
            map_r.inputs['To Min'].default_value = -1.0
            map_r.inputs['To Max'].default_value = 1.0
            
            map_g = nodes.new(type='ShaderNodeMapRange')
            map_g.location = (50, -100)
            map_g.inputs['From Min'].default_value = 0.0
            map_g.inputs['From Max'].default_value = 1.0
            map_g.inputs['To Min'].default_value = -1.0
            map_g.inputs['To Max'].default_value = 1.0
            
            pow_r = nodes.new(type='ShaderNodeMath')
            pow_r.operation = 'MULTIPLY'
            pow_r.location = (250, 100)
            
            pow_g = nodes.new(type='ShaderNodeMath')
            pow_g.operation = 'MULTIPLY'
            pow_g.location = (250, -100)
            
            add_sq = nodes.new(type='ShaderNodeMath')
            add_sq.operation = 'ADD'
            add_sq.location = (450, 0)
            
            sub_one = nodes.new(type='ShaderNodeMath')
            sub_one.operation = 'SUBTRACT'
            sub_one.location = (650, 0)
            sub_one.inputs[0].default_value = 1.0
            
            max_zero = nodes.new(type='ShaderNodeMath')
            max_zero.operation = 'MAXIMUM'
            max_zero.location = (850, 0)
            max_zero.inputs[1].default_value = 0.0
            
            sqrt_z = nodes.new(type='ShaderNodeMath')
            sqrt_z.operation = 'SQRT'
            sqrt_z.location = (1050, 0)
            
            map_z = nodes.new(type='ShaderNodeMapRange')
            map_z.location = (1250, 0)
            map_z.inputs['From Min'].default_value = -1.0
            map_z.inputs['From Max'].default_value = 1.0
            map_z.inputs['To Min'].default_value = 0.0
            map_z.inputs['To Max'].default_value = 1.0
            
            comb = nodes.new(type='ShaderNodeCombineColor')
            comb.location = (1450, -100)
            
            links.new(node_normal_img.outputs['Color'], sep.inputs['Color'])
            links.new(sep.outputs['Red'], map_r.inputs['Value'])
            links.new(sep.outputs['Green'], map_g.inputs['Value'])
            
            links.new(map_r.outputs['Result'], pow_r.inputs[0])
            links.new(map_r.outputs['Result'], pow_r.inputs[1])
            links.new(map_g.outputs['Result'], pow_g.inputs[0])
            links.new(map_g.outputs['Result'], pow_g.inputs[1])
            
            links.new(pow_r.outputs['Value'], add_sq.inputs[0])
            links.new(pow_g.outputs['Value'], add_sq.inputs[1])
            
            links.new(add_sq.outputs['Value'], sub_one.inputs[1])
            links.new(sub_one.outputs['Value'], max_zero.inputs[0])
            links.new(max_zero.outputs['Value'], sqrt_z.inputs[0])
            links.new(sqrt_z.outputs['Value'], map_z.inputs['Value'])
            
            links.new(sep.outputs['Red'], comb.inputs['Red'])
            links.new(sep.outputs['Green'], comb.inputs['Green'])
            links.new(map_z.outputs['Result'], comb.inputs['Blue'])
            
            links.new(comb.outputs['Color'], node_normal_map.inputs['Color'])
        else:
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


def create_ordered_evr_material(mat_name, texture_hashes, resolved_textures):
    """Create a material from sequential Echo texture order.

    This mirrors the known-good Doug script: each skin group is the next four
    texture hashes in order, where index 0 is the visible atlas/base color.
    """
    mat = bpy.data.materials.new(name=mat_name)
    mat.use_nodes = True
    mat.blend_method = 'OPAQUE'
    mat.use_transparent_shadow = False

    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    nodes.clear()

    bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')
    bsdf.location = (200, 100)
    bsdf.inputs['Alpha'].default_value = 1.0
    if 'Roughness' in bsdf.inputs:
        bsdf.inputs['Roughness'].default_value = 0.5

    output = nodes.new(type='ShaderNodeOutputMaterial')
    output.location = (500, 100)
    links.new(bsdf.outputs['BSDF'], output.inputs['Surface'])

    uv_node = nodes.new(type='ShaderNodeUVMap')
    uv_node.uv_map = "UVMap"
    uv_node.location = (-600, 300)

    def image_for(slot, colorspace):
        if slot >= len(texture_hashes):
            return None, None
        tex_hash = texture_hashes[slot]
        tex_path = resolved_textures.get(tex_hash)
        if not tex_path:
            return tex_hash, None
        image = bpy.data.images.load(tex_path)
        image.colorspace_settings.name = colorspace
        return tex_hash, image

    base_hash, base_image = image_for(0, 'sRGB')
    normal_hash, normal_image = image_for(1, 'Non-Color')
    packed_hash, packed_image = image_for(2, 'Non-Color')
    emit_hash, emit_image = image_for(3, 'sRGB')
    print(
        f"[EVR Material] {mat_name}: "
        f"base={base_hash or 'None'}, normal={normal_hash or 'None'}, "
        f"packed={packed_hash or 'None'}, emissive={emit_hash or 'None'}"
    )

    if base_image:
        node = nodes.new(type='ShaderNodeTexImage')
        node.location = (-300, 300)
        node.image = base_image
        node.interpolation = 'Cubic'
        links.new(uv_node.outputs['UV'], node.inputs['Vector'])
        links.new(node.outputs['Color'], bsdf.inputs['Base Color'])

    if normal_image:
        node = nodes.new(type='ShaderNodeTexImage')
        node.location = (-300, 20)
        node.image = normal_image
        node.interpolation = 'Cubic'
        normal_map = nodes.new(type='ShaderNodeNormalMap')
        normal_map.location = (-40, 20)
        normal_map.inputs['Strength'].default_value = 1.0
        links.new(uv_node.outputs['UV'], node.inputs['Vector'])
        links.new(node.outputs['Color'], normal_map.inputs['Color'])
        links.new(normal_map.outputs['Normal'], bsdf.inputs['Normal'])

    if packed_image:
        node = nodes.new(type='ShaderNodeTexImage')
        node.location = (-300, -240)
        node.image = packed_image
        node.interpolation = 'Cubic'
        separate = nodes.new(type='ShaderNodeSeparateColor')
        separate.location = (-40, -240)
        links.new(uv_node.outputs['UV'], node.inputs['Vector'])
        links.new(node.outputs['Color'], separate.inputs['Color'])
        if 'Metallic' in bsdf.inputs:
            links.new(separate.outputs['Red'], bsdf.inputs['Metallic'])
        if 'Roughness' in bsdf.inputs:
            links.new(separate.outputs['Green'], bsdf.inputs['Roughness'])

    if emit_image:
        node = nodes.new(type='ShaderNodeTexImage')
        node.location = (-300, -500)
        node.image = emit_image
        node.interpolation = 'Cubic'
        links.new(uv_node.outputs['UV'], node.inputs['Vector'])
        if 'Emission Color' in bsdf.inputs:
            links.new(node.outputs['Color'], bsdf.inputs['Emission Color'])
        if 'Emission Strength' in bsdf.inputs:
            bsdf.inputs['Emission Strength'].default_value = 0.25

    return mat

def create_classified_evr_material(mat_name, texture_hashes, resolved_textures):
    """Create a material by dynamically analyzing the pixel content of all textures in the group."""
    classified = {}
    for tex_hash in texture_hashes:
        tex_path = resolved_textures.get(tex_hash)
        if not tex_path:
            continue
        role = classify_texture_by_pixels(tex_path)
        # Keep the first found of each role
        if role not in classified:
            classified[role] = tex_hash

    group_bindings = {}
    mapping_textures = []
    idx = 0
    
    if "albedo" in classified:
        group_bindings[0] = {"texture_idx": idx}
        mapping_textures.append(classified["albedo"])
        idx += 1
        
    if "normal_rg" in classified:
        group_bindings[1] = {"texture_idx": idx}
        mapping_textures.append(classified["normal_rg"])
        idx += 1
    elif "normal" in classified:
        group_bindings[1] = {"texture_idx": idx}
        mapping_textures.append(classified["normal"])
        idx += 1
        
    if "roughness" in classified:
        group_bindings[2] = {"texture_idx": idx}
        mapping_textures.append(classified["roughness"])
        idx += 1
    elif "metallic" in classified:
        group_bindings[2] = {"texture_idx": idx}
        mapping_textures.append(classified["metallic"])
        idx += 1
        
    if "emissive" in classified:
        group_bindings[3] = {"texture_idx": idx}
        mapping_textures.append(classified["emissive"])
        idx += 1
        
    class_tex_dict = {h: role for role, h in classified.items()}
    return create_true_evr_material(mat_name, group_bindings, mapping_textures, resolved_textures, class_tex_dict)


# ==============================================================================
# MAIN APPLYING INTERFACE
# ==============================================================================
def _objects_look_like_lod_stack(objects):
    mesh_objects = [obj for obj in objects if getattr(obj, "type", None) == 'MESH' and obj.data and obj.data.vertices]
    if len(mesh_objects) < 2:
        return False

    def bbox(obj):
        coords = [obj.matrix_world @ v.co for v in obj.data.vertices]
        mins = [min(co[i] for co in coords) for i in range(3)]
        maxs = [max(co[i] for co in coords) for i in range(3)]
        center = [(mins[i] + maxs[i]) * 0.5 for i in range(3)]
        size = [maxs[i] - mins[i] for i in range(3)]
        diag = max(math.sqrt(sum(s * s for s in size)), 1e-6)
        return center, diag

    c0, d0 = bbox(mesh_objects[0])
    similar = 0
    for obj in mesh_objects[1:]:
        c, d = bbox(obj)
        center_delta = math.sqrt(sum((c[i] - c0[i]) ** 2 for i in range(3)))
        if center_delta <= d0 * 0.25 and 0.35 <= d / d0 <= 1.65:
            similar += 1
    return similar >= max(1, len(mesh_objects) - 2)


import random

def _score_texture_uv_match(img, uv_samples, pixel_cache=None):
    if not img or not getattr(img, "pixels", None):
        return -1.0
        
    width = img.size[0]
    height = img.size[1]
    if width == 0 or height == 0:
        return -1.0
        
    if len(uv_samples) > 200:
        uv_samples = random.sample(uv_samples, 200)
        
    # Fast caching to avoid Blender's horrific img.pixels index overhead
    if pixel_cache is not None:
        if img.name not in pixel_cache:
            import array
            num_pixels = width * height
            # Allocate contiguous float array (268MB for 4K texture)
            arr = array.array('f', [0.0] * (num_pixels * 4))
            img.pixels.foreach_get(arr)
            pixel_cache[img.name] = arr
        cache = pixel_cache[img.name]
    else:
        cache = img.pixels
        
    hits = 0
    total = len(uv_samples)
    
    # 1. Calculate Hit Rate (under UVs)
    for u, v in uv_samples:
        u_frac = u % 1.0
        v_frac = v % 1.0
        px = int(u_frac * width)
        py = int(v_frac * height)
        px = max(0, min(px, width - 1))
        py = max(0, min(py, height - 1))
        
        idx = (py * width + px) * 4 + 3
        a = cache[idx]
        r = cache[idx - 3]
        g = cache[idx - 2]
        b = cache[idx - 1]
        if a > 0.05 and (r > 0.05 or g > 0.05 or b > 0.05):
            hits += 1
            
    hit_rate = hits / total if total > 0 else 0.0
    
    # 2. Calculate Background Opacity (random sampling)
    bg_hits = 0
    bg_samples = 200
    for _ in range(bg_samples):
        px = random.randint(0, width - 1)
        py = random.randint(0, height - 1)
        idx = (py * width + px) * 4 + 3
        a = cache[idx]
        r = cache[idx - 3]
        g = cache[idx - 2]
        b = cache[idx - 1]
        if a > 0.05 and (r > 0.05 or g > 0.05 or b > 0.05):
            bg_hits += 1
            
    bg_rate = bg_hits / bg_samples
    
    return hit_rate - bg_rate

def apply_textures_to_objects(model_hash, pcvr_extracted_dir, texture_cache_dir, target_objects,
                              texture_group_mode="sequential", material_assign_mode="auto", force_skin_index=-1, is_lod_stack=False):
    """Parses textures for model_hash and procedurally applies PBR node materials onto target_objects."""
    # 1. Standardize and discover missing paths
    disc = discover_paths()
    
    p_ext = pcvr_extracted_dir.strip() if pcvr_extracted_dir else None
    if not p_ext:
        p_ext = disc["pcvr_extracted"]
        
    t_cache = texture_cache_dir.strip() if texture_cache_dir else None
    if not t_cache:
        t_cache = disc["texture_cache"]
        
    if not p_ext or not os.path.exists(p_ext):
        print(f"[-] Texture Auto-Apply: pcvr-extracted directory not resolved/provided.")
        return 0
        
    if not t_cache or not os.path.exists(t_cache):
        print(f"[-] Texture Auto-Apply: texture_cache directory not resolved/provided.")
        return 0
        
    # 2. Parse Materials Mapping
    mapping = parse_materials_mapping(p_ext, model_hash)
    if not mapping:
        return 0
        
    # Load global texture mapping
    global_mapping = {}
    _dir = os.path.dirname(os.path.abspath(__file__))
    if os.path.basename(_dir) == "__pycache__":
        _dir = os.path.dirname(_dir)
    mapping_path = os.path.join(_dir, "texture_mapping.json")
    if os.path.exists(mapping_path):
        try:
            import json
            with open(mapping_path, 'r') as f:
                global_mapping = json.load(f)
        except Exception:
            pass

    # 3. Resolve Unique Textures on Disk
    resolved_textures = {}

    for tex_hash in set(mapping["textures"]):
        payload_hash = global_mapping.get(tex_hash, tex_hash)
        vars_payload = get_all_name_variations(payload_hash)
        vars_meta = get_all_name_variations(tex_hash)
        
        found_path = None
        
        search_dirs = [t_cache] if t_cache and os.path.exists(t_cache) else []
        
        # ALSO search the addon's local texture/ directory (or the dev directory)
        for search_root in [os.path.dirname(_dir), _dir]:
            local_tex_dir = os.path.join(search_root, "texture")
            if os.path.exists(local_tex_dir) and local_tex_dir not in search_dirs:
                search_dirs.insert(0, local_tex_dir) # Prepend so it has priority
                
        # Also add hardcoded dev fallback
        alt = r"J:\EchoVR-Tools-Launcher\evr-mesh-importer\texture"
        if os.path.exists(alt) and alt not in search_dirs:
            search_dirs.insert(0, alt)

        for ext in [".dds", ".png"]:
            for d in search_dirs:
                # Search payload first, then metadata
                for v in vars_payload + vars_meta:
                    path = os.path.join(d, f"{v}{ext}")
                    if os.path.exists(path):
                        found_path = path
                        break
                if found_path: break
            if found_path: break
            
        if found_path:
            resolved_textures[tex_hash] = found_path
            
    if not resolved_textures:
        print("[-] Texture Auto-Apply: No texture cached PNG files resolved.")
        return 0
        
    if material_assign_mode in ["perfect_uv", "detailed_orm"]:
        albedo_hashes = set()
        target_slot = 0 if material_assign_mode == "perfect_uv" else 2
        
        if mapping.get("bindings"):
            for bind in mapping["bindings"]:
                if bind["slot_idx"] % 4 == target_slot:
                    tex_idx = bind["texture_idx"]
                    if 0 <= tex_idx < len(mapping["textures"]):
                        albedo_hashes.add(mapping["textures"][tex_idx])
        else:
            for i in range(target_slot, len(mapping["textures"]), 4):
                if i < len(mapping["textures"]):
                    albedo_hashes.add(mapping["textures"][i])

        # Find max_size_hash BEFORE loading images to prevent Blender from hanging on large files!
        max_size = -1
        max_size_hash = None
        if material_assign_mode in ["detailed_orm", "perfect_uv"]:
            for thash in albedo_hashes:
                path_tex = resolved_textures.get(thash)
                if path_tex and os.path.exists(path_tex):
                    sz = os.path.getsize(path_tex)
                    if sz > max_size:
                        max_size = sz
                        max_size_hash = thash

        albedo_images = {}
        for thash in albedo_hashes:
            if thash in resolved_textures:
                # If we are in detailed_orm or perfect_uv, only load the best match!
                if max_size_hash and thash != max_size_hash:
                    continue
                try:
                    albedo_images[thash] = bpy.data.images.load(resolved_textures[thash])
                except Exception:
                    pass
                    
        if not albedo_images:
            print("[-] Perfect UV: No albedo textures found.")
            return 0
            
        created_count = 0
                        
        for idx, obj in enumerate(target_objects):
            obj.data.materials.clear()
            uv_layer = obj.data.uv_layers.active
            if not uv_layer:
                continue
                
            best_hash = None
            if max_size_hash:
                best_hash = max_size_hash
            else:
                uv_samples = [uv_layer.data[l.index].uv[:] for l in obj.data.loops]
                best_score = -1.0
                for thash, img in albedo_images.items():
                    score = _score_texture_uv_match(img, uv_samples)
                    if score > best_score:
                        best_score = score
                        best_hash = thash
                    
            if best_hash:
                mat_name = f"{model_hash}_UVMatch_{idx}"
                mat = bpy.data.materials.get(mat_name)
                if mat: bpy.data.materials.remove(mat)
                
                mat = bpy.data.materials.new(name=mat_name)
                mat.use_nodes = True
                nodes = mat.node_tree.nodes
                links = mat.node_tree.links
                nodes.clear()
                
                bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')
                bsdf.location = (0, 0)
                out = nodes.new(type='ShaderNodeOutputMaterial')
                out.location = (300, 0)
                links.new(bsdf.outputs['BSDF'], out.inputs['Surface'])
                
                tex = nodes.new(type='ShaderNodeTexImage')
                tex.location = (-300, 0)
                tex.image = albedo_images[best_hash]
                links.new(tex.outputs['Color'], bsdf.inputs['Base Color'])
                
                obj.data.materials.append(mat)
                for p in obj.data.polygons:
                    p.material_index = 0
                created_count += 1
                
        # Switch all 3D viewports to Material Preview shading mode so textures are instantly visible
        for screen in bpy.data.screens:
            for area in screen.areas:
                if area.type == 'VIEW_3D':
                    for space in area.spaces:
                        if space.type == 'VIEW_3D':
                            space.shading.type = 'MATERIAL'
                            
        return created_count

    # 4. Group texture hashes. Sequential matches the verified Doug workflow;
    # binding mode is available for resources whose second table is authoritative.
    if texture_group_mode == "binding":
        grouped = {}
        for bind in mapping["bindings"]:
            tex_idx = bind["texture_idx"]
            skin_id = bind.get("skin_id", bind["slot_idx"] // 4)
            if 0 <= tex_idx < len(mapping["textures"]):
                if skin_id not in grouped:
                    grouped[skin_id] = []
                # Keep textures in the exact order they appear for this skin
                grouped[skin_id].append(mapping["textures"][tex_idx])
                
        texture_groups = []
        for skin_id, tex_list in grouped.items():
            texture_groups.append(tex_list)
    else:
        texture_groups = [
            mapping["textures"][i:i + 4]
            for i in range(0, len(mapping["textures"]), 4)
        ]
        
    # 5. Create PBR Materials
    created_materials = []
    for g_idx, texture_group in enumerate(texture_groups):
        mat_name = f"{model_hash}_Skin_{g_idx}"
        # Reuse existing material if present to avoid duplicating nodes
        mat = bpy.data.materials.get(mat_name)
        if mat:
            bpy.data.materials.remove(mat)

        if texture_group_mode == "binding":
            mat = create_classified_evr_material(mat_name, texture_group, resolved_textures)
        else:
            mat = create_ordered_evr_material(mat_name, texture_group, resolved_textures)
        created_materials.append(mat)
        
    if not created_materials:
        return 0
        
    # 6. Assign constructed materials
    # Clear existing slots on each mesh object and add all created materials to its slots.
    # Assign each face's material index using the game engine's cumulative vertex face ranges.
    lod_stack = _objects_look_like_lod_stack(target_objects) or len(target_objects) == 1
    for idx, obj in enumerate(target_objects):
        obj.data.materials.clear()
        for mat in created_materials:
            obj.data.materials.append(mat)
            
        if material_assign_mode == "uv_scanner":
            uv_layer = obj.data.uv_layers.active
            if not uv_layer:
                continue
                
            global_pixel_cache = {}
            
            # Group polygons by UV tile
            tile_to_polys = {}
            tile_to_uvs = {}
            
            for poly in obj.data.polygons:
                # Use the first vertex of the polygon to determine its UV tile
                u, v = uv_layer.data[poly.loop_indices[0]].uv
                tile = (math.floor(u), math.floor(v))
                
                if tile not in tile_to_polys:
                    tile_to_polys[tile] = []
                    tile_to_uvs[tile] = []
                    
                tile_to_polys[tile].append(poly.index)
                
                for loop_idx in poly.loop_indices:
                    tile_to_uvs[tile].append(uv_layer.data[loop_idx].uv[:])
                    
            for tile, polys in tile_to_polys.items():
                best_mat_idx = 0
                best_score = -1.0
                
                for m_idx, mat in enumerate(created_materials):
                    score = 0.0
                    nodes = mat.node_tree.nodes
                    tex_node = None
                    for node in nodes:
                        if node.type == 'TEX_IMAGE':
                            for out in node.outputs:
                                if out.is_linked:
                                    for link in out.links:
                                        if link.to_socket.name == 'Base Color':
                                            tex_node = node
                                            break
                                if tex_node: break
                        if tex_node: break
                            
                    if tex_node and tex_node.image:
                        score = _score_texture_uv_match(tex_node.image, tile_to_uvs[tile], global_pixel_cache)
                    
                    if score > best_score:
                        best_score = score
                        best_mat_idx = m_idx
                        
                for p_idx in polys:
                    obj.data.polygons[p_idx].material_index = best_mat_idx

            # Free pixel caches to instantly release RAM
            global_pixel_cache.clear()
            import gc
            gc.collect()

        elif material_assign_mode == "first":
            mat_idx = 0
        elif material_assign_mode == "submesh":
            mat_idx = obj.get("evr_material_index", idx)
        elif material_assign_mode == "reverse":
            mat_idx = len(target_objects) - 1 - idx
        else:
            # Auto: a single imported mesh is usually LOD0 after the importer
            # trims LOD stacks, so use Skin 0. True multi-part models use order.
            mat_idx = force_skin_index if force_skin_index != -1 else (0 if is_lod_stack else obj.get("evr_material_index", idx))
            
            if mat_idx >= len(created_materials):
                mat_idx = 0

            v_count = len(obj.data.vertices)
            if len(created_materials) >= 5 and v_count < 200:
                # Map face materials using exact cumulative vertex index ranges specifically for the Visor hack
                for poly in obj.data.polygons:
                    v_max = max(poly.vertices)
                    if v_max <= 4:
                        poly.material_index = 1
                    elif v_max <= 6:
                        poly.material_index = 2
                    elif v_max <= 12:
                        poly.material_index = 3
                    elif v_max <= 19:
                        poly.material_index = 4
                    else:
                        poly.material_index = 0
            else:
                # Assign the proper logical material index corresponding to this submesh
                for poly in obj.data.polygons:
                    poly.material_index = mat_idx
            
    # Switch all 3D viewports to Material Preview shading mode so textures are instantly visible
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type == 'VIEW_3D':
                for space in area.spaces:
                    if space.type == 'VIEW_3D':
                        space.shading.type = 'MATERIAL'
                        
    return len(created_materials)
