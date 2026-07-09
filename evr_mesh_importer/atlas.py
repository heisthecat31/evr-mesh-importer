import bpy
import bmesh
import math
import os

def _trace_socket_to_image(socket, depth=0):
    if depth > 10: return None
    if not socket.is_linked: return None
    for link in socket.links:
        node = link.from_node
        if node.type == 'TEX_IMAGE' and node.image:
            return node.image
        for in_sock in node.inputs:
            img = _trace_socket_to_image(in_sock, depth+1)
            if img: return img
    return None

def _get_material_image(mat, roles):
    if not mat or not mat.use_nodes: return None
    bsdf = next((n for n in mat.node_tree.nodes if n.type == 'BSDF_PRINCIPLED'), None)
    if not bsdf: return None
    for role in roles:
        if role in bsdf.inputs:
            img = _trace_socket_to_image(bsdf.inputs[role])
            if img: return img
    return None

def auto_atlas_objects(context, objects, atlas_res=2048):
    if not objects: return False, "No objects selected"
    
    # 1. Collect unique materials
    materials = set()
    for obj in objects:
        if obj.type != 'MESH': continue
        for slot in obj.material_slots:
            if slot.material:
                materials.add(slot.material)
                
    materials = list(materials)
    if not materials:
        return False, "No materials found on selected objects."
        
    n_mats = len(materials)
    grid_cols = math.ceil(math.sqrt(n_mats))
    grid_rows = math.ceil(n_mats / grid_cols)
    
    cell_res = atlas_res // grid_cols
    # Adjust atlas res to exact multiple
    atlas_w = grid_cols * cell_res
    atlas_h = grid_rows * cell_res
    
    mat_to_cell = {}
    for i, mat in enumerate(materials):
        row = i // grid_cols
        col = i % grid_cols
        mat_to_cell[mat.name] = (col, row)
        
    # 2. Collect images by type
    roles = {
        'albedo': ('Base Color', 'Color'),
        'normal': ('Normal',),
        'orm': ('Roughness', 'Metallic', 'Specular'),
        'emissive': ('Emission Color', 'Emission', 'Emission Strength')
    }
    
    built_atlases = {}
    
    for role_name, socket_names in roles.items():
        mat_to_img = {}
        for mat in materials:
            img = _get_material_image(mat, socket_names)
            if img:
                mat_to_img[mat.name] = img
                
        if not mat_to_img:
            continue
            
        print(f"Building atlas for {role_name}...")
        
        atlas_img = bpy.data.images.new(f"Atlas_{role_name.capitalize()}", width=atlas_w, height=atlas_h, alpha=True)
        pixels = [1.0 if role_name == 'albedo' else 0.5] * (atlas_w * atlas_h * 4)
        
        for mat_name, img in mat_to_img.items():
            col, row = mat_to_cell[mat_name]
            
            src = img.copy()
            try:
                src.scale(cell_res, cell_res)
                src_pixels = list(src.pixels)
                
                for y in range(cell_res):
                    atlas_y = row * cell_res + y
                    atlas_x_start = col * cell_res
                    
                    src_start = y * cell_res * 4
                    src_end = src_start + cell_res * 4
                    
                    atlas_start = (atlas_y * atlas_w + atlas_x_start) * 4
                    atlas_end = atlas_start + cell_res * 4
                    
                    pixels[atlas_start:atlas_end] = src_pixels[src_start:src_end]
            except Exception as e:
                print(f"Failed to copy image {img.name}: {e}")
            finally:
                bpy.data.images.remove(src)
                
        atlas_img.pixels = pixels
        built_atlases[role_name] = atlas_img

    # 3. Scale and Translate UVs
    for obj in objects:
        if obj.type != 'MESH': continue
        mesh = obj.data
        if not mesh.uv_layers:
            mesh.uv_layers.new(name="UVMap")
            
        uv_layer = mesh.uv_layers.active.data
        
        su = 1.0 / grid_cols
        sv = 1.0 / grid_rows
        
        for poly in mesh.polygons:
            if poly.material_index < len(obj.material_slots):
                mat = obj.material_slots[poly.material_index].material
                if mat and mat.name in mat_to_cell:
                    col, row = mat_to_cell[mat.name]
                    
                    for loop_idx in poly.loop_indices:
                        uv = uv_layer[loop_idx].uv
                        uv[0] = (uv[0] % 1.0) * su + (col * su)
                        uv[1] = (uv[1] % 1.0) * sv + (row * sv)
                        
    # 4. Create master material
    master_mat = bpy.data.materials.new(name="Atlas_Master")
    master_mat.use_nodes = True
    nodes = master_mat.node_tree.nodes
    links = master_mat.node_tree.links
    
    bsdf = None
    for n in nodes:
        if n.type == 'BSDF_PRINCIPLED':
            bsdf = n
            break
            
    if not bsdf:
        bsdf = nodes.new('ShaderNodeBsdfPrincipled')
        
    y_offset = 300
    for role_name, atlas_img in built_atlases.items():
        tex_node = nodes.new('ShaderNodeTexImage')
        tex_node.image = atlas_img
        tex_node.location = (-300, y_offset)
        y_offset -= 300
        
        if role_name == 'albedo':
            links.new(tex_node.outputs[0], bsdf.inputs.get('Base Color', bsdf.inputs[0]))
        elif role_name == 'normal':
            norm_node = nodes.new('ShaderNodeNormalMap')
            norm_node.location = (-150, y_offset + 300)
            links.new(tex_node.outputs[0], norm_node.inputs['Color'])
            links.new(norm_node.outputs[0], bsdf.inputs.get('Normal'))
            atlas_img.colorspace_settings.name = 'Non-Color'
        elif role_name == 'orm':
            atlas_img.colorspace_settings.name = 'Non-Color'
            links.new(tex_node.outputs[0], bsdf.inputs.get('Roughness'))
        elif role_name == 'emissive':
            links.new(tex_node.outputs[0], bsdf.inputs.get('Emission Color', bsdf.inputs.get('Emission')))
            
    # 5. Apply master material and remove old slots
    for obj in objects:
        if obj.type != 'MESH': continue
        bpy.context.view_layer.objects.active = obj
        for _ in range(len(obj.material_slots)):
            bpy.ops.object.material_slot_remove()
        obj.data.materials.append(master_mat)
        
    # 6. Join objects
    bpy.ops.object.select_all(action='DESELECT')
    for obj in objects:
        obj.select_set(True)
    if objects:
        context.view_layer.objects.active = objects[0]
        if len(objects) > 1:
            bpy.ops.object.join()
            
    return True, "Successfully atlased materials and joined meshes!"
