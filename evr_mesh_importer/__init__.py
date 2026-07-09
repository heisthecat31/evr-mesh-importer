"""
EVR Raw Mesh Importer/Exporter — Blender add-on
Imports and exports raw GPU mesh binaries from Echo VR (Echo Arena).
https://github.com/Dualgame/evr-mesh-importer
"""

bl_info = {
    "name": "EVR Raw Mesh Importer",
    "author": "Dualgame",
    "version": (2, 0, 0),
    "blender": (3, 0, 0),
    "location": "File > Import/Export > EVR Raw Mesh",
    "description": "Import/export raw GPU mesh binaries from Echo VR (Echo Arena)",
    "doc_url": "https://github.com/Dualgame/evr-mesh-importer",
    "tracker_url": "https://github.com/Dualgame/evr-mesh-importer/issues",
    "category": "Import-Export",
}

import bpy
import os
import sys
import importlib
import shutil

_submodules = ["decode", "primary", "encode", "textures", "collision_decode", "texture_replace"]
for _sub in _submodules:
    _full_name = f"{__package__}.{_sub}" if __package__ else _sub
    if _full_name in sys.modules:
        importlib.reload(sys.modules[_full_name])

from bpy_extras.io_utils import ImportHelper, ExportHelper
from bpy.props import (StringProperty, BoolProperty, FloatProperty, CollectionProperty, EnumProperty, IntProperty)
from bpy.types import Operator

from .decode import extract_mesh
from .primary import _find_primary_data
from .encode import (
    encode_heuristic_s16,
    encode_heuristic_s20,
    encode_heuristic_dual28,
    encode_cgml,
    mesh_from_blender_object,
)
from .textures import apply_textures_to_objects

def _build_blender_mesh(verts, faces, name):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)
    return obj

def _apply_flat_shading(obj):
    for poly in obj.data.polygons:
        poly.use_smooth = False

def _mesh_bbox(verts):
    mins = [min(v[i] for v in verts) for i in range(3)]
    maxs = [max(v[i] for v in verts) for i in range(3)]
    center = tuple((mins[i] + maxs[i]) * 0.5 for i in range(3))
    size = tuple(maxs[i] - mins[i] for i in range(3))
    return center, size

def _looks_like_lod_stack(submeshes):
    valid = [sub for sub in submeshes if len(sub) >= 2 and sub[0] and sub[1]]
    if len(valid) < 2:
        return False
    c0, s0 = _mesh_bbox(valid[0][0])
    diag0 = max((s0[0] * s0[0] + s0[1] * s0[1] + s0[2] * s0[2]) ** 0.5, 1e-6)
    for sub in valid[1:]:
        c, s = _mesh_bbox(sub[0])
        center_delta = ((c[0] - c0[0]) ** 2 + (c[1] - c0[1]) ** 2 + (c[2] - c0[2]) ** 2) ** 0.5
        diag = (s[0] * s[0] + s[1] * s[1] + s[2] * s[2]) ** 0.5
        if center_delta > diag0 * 0.15:
            return False
        if diag < diag0 * 0.65 or diag > diag0 * 1.35:
            return False
    return True

class EVR_OT_ImportMesh(Operator, ImportHelper):
    bl_idname = "import_mesh.evr_raw"
    bl_label = "EVR Raw Mesh"
    bl_options = {'REGISTER', 'UNDO'}
    filter_glob: StringProperty(default="*", options={'HIDDEN'}, maxlen=255)
    files: CollectionProperty(name="File Path", type=bpy.types.OperatorFileListElement)
    directory: StringProperty(subtype='DIR_PATH')
    primary_file: StringProperty(name="Primary File", default="", subtype='FILE_PATH')
    auto_find_primary: BoolProperty(name="Auto-find Primary", default=True)
    auto_apply_textures: BoolProperty(name="Auto-Apply Textures", default=True)
    extracted_game_dir: StringProperty(name="Extracted Game Dir", default="", subtype='DIR_PATH')
    flip_texture_v: BoolProperty(name="Flip Texture V", default=True)
    import_lod0_only: BoolProperty(name="Import LOD0 Only", default=True)
    import_collision: BoolProperty(name="Import Collision Data (Experimental)", default=False)
    texture_group_mode: EnumProperty(
        name="Texture Grouping",
        items=[
            ('sequential', "Sequential Texture List", "Use texture groups in raw mapping order, matching the verified Doug import"),
            ('binding', "Binding Table", "Use the mapping binding table to choose each group's texture slots"),
        ],
        default='binding',
    )
    material_assign_mode: EnumProperty(
        name="Material Assignment",
        items=[
            ('perfect_uv', "Base Color Only", "Applies ONLY the single best matching Base Color texture per mesh"),
            ('detailed_orm', "Detailed Map Only (ORM)", "Applies ONLY the detailed ORM map (scratches, shadows) as the main texture. Perfect for customizable models."),
            ('uv_scanner', "Dynamic UV Scanner", "Dynamically scan texture PNGs and assign materials based on UV layout"),
            ('auto', "Auto", "Automatically apply materials based on best guess (uses Skin 0 if available)"),
            ('first', "Force Skin 0", "Force the first skin (Skin 0) material onto all submeshes"),
            ('submesh', "Submesh Order", "Apply materials sequentially matching submesh index"),
            ('reverse', "Reverse Submesh Order", "Apply materials in reverse submesh order"),
        ],
        default='auto',
    )
    force_skin_index: IntProperty(
        name="Force Skin Index",
        description="When using 'Auto' or 'Force Skin' mode, forces a specific Skin index onto the entire model. Useful if the model defaults to the wrong variant. Set to -1 to disable.",
        default=-1,
        min=-1,
        max=50
    )
    use_smooth: BoolProperty(name="Smooth Shading", default=False)
    scale: FloatProperty(name="Scale", default=1.0, min=0.0001, max=10000.0)

    def _get_all_submeshes(self, context, is_cgml, obj):
        if not is_cgml:
            return [obj]
        if obj.parent and obj.parent.type == 'EMPTY':
            export_objs = [c for c in obj.parent.children if c.type == 'MESH']
        else:
            export_objs = [o for o in context.selected_objects if o.type == 'MESH']
        if not export_objs:
            export_objs = [obj]
        export_objs.sort(key=lambda x: x.get('evr_material_index', 9999))
        return export_objs

    def execute(self, context):
        paths = [os.path.join(self.directory, f.name) for f in self.files] if self.files else [self.filepath]
        primary_data = None
        if self.primary_file.strip():
            ppath = bpy.path.abspath(self.primary_file)
            if os.path.isfile(ppath):
                with open(ppath, "rb") as fh: primary_data = fh.read()
        
        all_roots = []
        for gpu_path in paths:
            root = self._import_single(context, gpu_path, primary_data)
            if root: all_roots.append(root)

        for obj in context.selected_objects: obj.select_set(False)
        for root in all_roots: root.select_set(True)
        if all_roots: context.view_layer.objects.active = all_roots[-1]
        return {'FINISHED'} if all_roots else {'CANCELLED'}

    def _import_single(self, context, gpu_path, primary_data=None):
        if not os.path.isfile(gpu_path): return None
        try:
            submeshes, path_label = extract_mesh(gpu_path, primary_data=primary_data, auto_find_primary=(primary_data is None and self.auto_find_primary))
        except Exception as exc:
            self.report({'ERROR'}, f"Decode failed: {exc}")
            return None
        if not submeshes: return None
        
        base_name = os.path.splitext(os.path.basename(gpu_path))[0]
        if self.import_lod0_only and _looks_like_lod_stack(submeshes):
            submeshes = submeshes[:1]
        valid = [(sub[0], sub[1]) for sub in submeshes if len(sub) >= 2 and sub[0] and sub[1]]
        parent_empty = None
        if len(valid) > 1:
            parent_empty = bpy.data.objects.new(base_name, None)
            bpy.context.collection.objects.link(parent_empty)

        created = []
        for idx, sub in enumerate(submeshes):
            verts = sub[0]
            faces = sub[1]
            uvs = sub[2] if len(sub) > 2 else None
            bone_data = sub[3] if len(sub) > 3 else None
            colors = sub[4] if len(sub) > 4 else None
            if not verts or not faces: continue
            if self.scale != 1.0: verts = [(x * self.scale, y * self.scale, z * self.scale) for x, y, z in verts]
            name = base_name if parent_empty is None else f"{base_name}.{idx:03d}"
            obj = _build_blender_mesh(verts, faces, name)
            if uvs and obj.data:
                uv_layer = obj.data.uv_layers.new(name="UVMap")
                for poly in obj.data.polygons:
                    for loop_idx in poly.loop_indices:
                        v_idx = obj.data.loops[loop_idx].vertex_index
                        if v_idx < len(uvs):
                            u, v = uvs[v_idx]
                            if self.flip_texture_v:
                                v = 1.0 - v
                            uv_layer.data[loop_idx].uv = (u, v)
            
            if bone_data and obj.data:
                vgs = {}
                for v_idx, (indices, weights) in enumerate(bone_data):
                    if v_idx >= len(verts): break
                    for b_idx, weight in zip(indices, weights):
                        if weight > 0:
                            if b_idx not in vgs:
                                vgs[b_idx] = obj.vertex_groups.new(name=f"Bone_{b_idx}")
                            vgs[b_idx].add([v_idx], weight / 255.0, 'REPLACE')
            if not self.use_smooth: _apply_flat_shading(obj)
            
            if colors and obj.data:
                if hasattr(obj.data, "color_attributes"):
                    color_layer0 = obj.data.color_attributes.new(name="word0", type='BYTE_COLOR', domain='CORNER')
                    color_layer1 = obj.data.color_attributes.new(name="word1", type='BYTE_COLOR', domain='CORNER')
                    for poly in obj.data.polygons:
                        for loop_idx in poly.loop_indices:
                            v_idx = obj.data.loops[loop_idx].vertex_index
                            if v_idx < len(colors):
                                color_layer0.data[loop_idx].color = colors[v_idx][0]
                                color_layer1.data[loop_idx].color = colors[v_idx][1]
            
            if hasattr(obj.data, "attributes"):
                attr = obj.data.attributes.new(name="cgml_submesh", type='INT', domain='FACE')
                for p in obj.data.polygons:
                    attr.data[p.index].value = idx
                    
            obj["evr_material_index"] = idx
            # Store import paths on each mesh so texture replacement can find them later
            if self.extracted_game_dir.strip():
                obj["evr_extracted_game_dir"] = bpy.path.abspath(self.extracted_game_dir)
            obj["evr_gpu_path"] = gpu_path
            if parent_empty: obj.parent = parent_empty
            created.append(obj)

        has_bones = any(len(obj.vertex_groups) > 0 for obj in created)
        arm_obj = None
        if has_bones:
            bone_names = set()
            for obj in created:
                for vg in obj.vertex_groups:
                    bone_names.add(vg.name)
            
            arm_data = bpy.data.armatures.new(name=f"{base_name}_Armature")
            arm_obj = bpy.data.objects.new(f"{base_name}_Armature", arm_data)
            bpy.context.collection.objects.link(arm_obj)
            
            # Deselect all and select armature to go to EDIT mode
            for o in bpy.context.selected_objects: o.select_set(False)
            arm_obj.select_set(True)
            bpy.context.view_layer.objects.active = arm_obj
            bpy.ops.object.mode_set(mode='EDIT')
            
            for b_name in sorted(list(bone_names)):
                bone = arm_data.edit_bones.new(b_name)
                bone.head = (0, 0, 0)
                bone.tail = (0, 0, 0.1)
                
            bpy.ops.object.mode_set(mode='OBJECT')
            
            for obj in created:
                mod = obj.modifiers.new(name="Armature", type='ARMATURE')
                mod.object = arm_obj
                
            if parent_empty:
                parent_empty.parent = arm_obj
            else:
                for obj in created:
                    obj.parent = arm_obj

        if self.auto_apply_textures and created:
            try:
                applied = apply_textures_to_objects(
                    base_name,
                    bpy.path.abspath(self.pcvr_extracted_dir) if self.pcvr_extracted_dir.strip() else "",
                    bpy.path.abspath(self.texture_cache_dir) if self.texture_cache_dir.strip() else "",
                    created,
                    texture_group_mode=self.texture_group_mode,
                    material_assign_mode=self.material_assign_mode,
                    force_skin_index=self.force_skin_index,
                )
                if applied:
                    self.report({'INFO'}, f"Applied {applied} texture material(s) to {base_name}")
                else:
                    self.report({'WARNING'}, f"Imported {base_name}, but no matching texture mapping/PNGs were found")
            except Exception as exc:
                self.report({'WARNING'}, f"Imported {base_name}, but texture auto-apply failed: {exc}")

        if self.import_collision:
            p_ext = bpy.path.abspath(self.pcvr_extracted_dir) if self.pcvr_extracted_dir.strip() else ""
            if not p_ext:
                from .textures import discover_paths
                disc = discover_paths()
                p_ext = disc.get("pcvr_extracted", "")
                
            if p_ext and os.path.exists(p_ext):
                col_folder = os.path.join(p_ext, "b7d338793fa37832")
                if os.path.exists(col_folder):
                    from .textures import get_all_name_variations
                    col_vars = get_all_name_variations(base_name)
                    col_file = None
                    for v in col_vars:
                        cand = os.path.join(col_folder, v)
                        if os.path.exists(cand):
                            col_file = cand
                            break
                    if col_file:
                        try:
                            from .collision_decode import extract_collision_heuristic
                            col_verts = extract_collision_heuristic(col_file)
                            if col_verts:
                                if self.scale != 1.0:
                                    col_verts = [(x * self.scale, y * self.scale, z * self.scale) for x, y, z in col_verts]
                                
                                # Create triangle soup to make wireframe visible
                                col_faces = []
                                for i in range(0, len(col_verts) - 2, 3):
                                    col_faces.append((i, i+1, i+2))
                                    
                                col_mesh = bpy.data.meshes.new(f"{base_name}_Collision")
                                col_mesh.from_pydata(col_verts, [], col_faces)
                                col_mesh.update()
                                col_obj = bpy.data.objects.new(f"{base_name}_Collision", col_mesh)
                                bpy.context.collection.objects.link(col_obj)
                                
                                # Setup wireframe display
                                col_obj.display_type = 'WIRE'
                                col_obj.show_in_front = True
                                
                                # Make it bright green
                                mat = bpy.data.materials.new(name="EVR_Collision_Mat")
                                mat.use_nodes = True
                                bsdf = mat.node_tree.nodes.get("Principled BSDF")
                                if bsdf:
                                    if 'Base Color' in bsdf.inputs: bsdf.inputs['Base Color'].default_value = (0, 1, 0, 1)
                                    if 'Emission Color' in bsdf.inputs: bsdf.inputs['Emission Color'].default_value = (0, 1, 0, 1)
                                    if 'Emission Strength' in bsdf.inputs: bsdf.inputs['Emission Strength'].default_value = 2.0
                                col_obj.data.materials.append(mat)
                                
                                if parent_empty:
                                    col_obj.parent = parent_empty
                                else:
                                    parent_empty = bpy.data.objects.new(base_name, None)
                                    bpy.context.collection.objects.link(parent_empty)
                                    if created: created[0].parent = parent_empty
                                    col_obj.parent = parent_empty
                                    
                                self.report({'INFO'}, f"Imported {len(col_verts)} collision points.")
                        except Exception as e:
                            self.report({'WARNING'}, f"Collision import failed: {e}")

        return arm_obj if arm_obj else parent_empty if parent_empty else created[0] if created else None

def menu_func_import(self, context): self.layout.operator(EVR_OT_ImportMesh.bl_idname, text="EVR Raw Mesh")

class EVR_ExportSettings(bpy.types.PropertyGroup):
    original_gpu_file: StringProperty(
        name="Original GPU File",
        description="Select the original GPU file you are replacing",
        subtype='FILE_PATH',
    )
    recalculate_normals: bpy.props.BoolProperty(
        name="Recalculate Normals",
        description="Compute smooth normals instead of zeroing them (experimental)",
        default=True
    )
    export_dir: StringProperty(
        name="Export Directory",
        description="Where the output folders and files will be saved",
        subtype='DIR_PATH',
        default=r"C:\Echovr\input-pcvr"
    )
    extracted_game_dir: StringProperty(
        name="Extracted Game Dir",
        description="Path to your pcvr-extracted directory containing model mappings and textures",
        subtype='DIR_PATH',
    )
    replace_textures: BoolProperty(name="Replace Textures", default=True)
    export_textures: BoolProperty(name="Export Associated Textures", default=True)
    export_bones: BoolProperty(name="Export Bone Data", default=True)
    encode_mode: EnumProperty(
        name="Encode Mode",
        items=[
            ('heuristic_s16', "Heuristic — stride-16", ""),
            ('heuristic_s20', "Heuristic — stride-20", ""),
            ('heuristic_dual28', "Heuristic — dual-28", ""),
            ('primary_described', "Primary Described (Full Patch)", "Patches the original Primary template to safely preserve game collision data"),
            ('cgml', "CGML — map geometry", ""),
        ],
        default='primary_described',
    )
    compute_normals: BoolProperty(name="Compute Normals", default=True)
    write_primary: BoolProperty(name="Write Primary File", default=True)
    auto_decimate: BoolProperty(
        name="Auto Decimate",
        description="Automatically decimate mesh if it exceeds vertex or file size limits",
        default=True,
    )

    stream0_stride: EnumProperty(name="Stream-0 Stride", items=[('auto', "Auto-Detect", ""), ('16', "16 bytes", ""), ('20', "20 bytes", "")], default='auto')
    scale: FloatProperty(name="Scale", default=1.0)


class EVR_OT_TransferWeights(bpy.types.Operator):
    """Transfer weights from original EVR mesh to custom mesh"""
    bl_idname = "evr.transfer_weights"
    bl_label = "Transfer Weights"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return len(context.selected_objects) >= 2 and context.active_object and context.active_object.type == 'MESH'

    def execute(self, context):
        target_obj = context.active_object
        source_objs = [obj for obj in context.selected_objects if obj != target_obj and obj.type == 'MESH']
        
        if not source_objs:
            self.report({'ERROR'}, 'You must select at least one source mesh.')
            return {'CANCELLED'}
            
        temp_obj = None
        if len(source_objs) > 1:
            # Duplicate and join all source objects into a temporary object
            bpy.ops.object.select_all(action='DESELECT')
            for obj in source_objs:
                obj.select_set(True)
            context.view_layer.objects.active = source_objs[0]
            bpy.ops.object.duplicate()
            bpy.ops.object.join()
            temp_obj = context.active_object
            source_obj = temp_obj
        else:
            source_obj = source_objs[0]
            
        # Create Data Transfer modifier
        mod = target_obj.modifiers.new(name='EVR_Weight_Transfer', type='DATA_TRANSFER')
        mod.object = source_obj
        mod.use_vert_data = True
        mod.data_types_verts = {'VGROUP_WEIGHTS'}
        mod.vert_mapping = 'POLYINTERP_NEAREST'
        
        # Generate data layers so vertex groups are actually created
        # We need target_obj to be active again to apply the modifier
        bpy.ops.object.select_all(action='DESELECT')
        target_obj.select_set(True)
        context.view_layer.objects.active = target_obj
        
        bpy.ops.object.datalayout_transfer(modifier=mod.name)
        
        # Apply the modifier
        bpy.ops.object.modifier_apply(modifier=mod.name)
        
        # Clean up weights (limit total and normalize)
        if len(target_obj.vertex_groups) > 0:
            bpy.ops.object.mode_set(mode='WEIGHT_PAINT')
            bpy.ops.object.vertex_group_limit_total(limit=4)
            bpy.ops.object.vertex_group_normalize_all()
            bpy.ops.object.mode_set(mode='OBJECT')
            
        if temp_obj:
            bpy.data.objects.remove(temp_obj, do_unlink=True)
            
        # Restore original selection
        for obj in source_objs:
            obj.select_set(True)
        
        self.report({'INFO'}, f'Transferred weights from {len(source_objs)} objects to {target_obj.name}')
        return {'FINISHED'}

class EVR_PT_ExportPanel(bpy.types.Panel):
    bl_label = "EVR Mesh Tools"
    bl_idname = "EVR_PT_export_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "EVR Tools"

    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.type == 'MESH'

    def draw(self, context):
        layout = self.layout
        settings = context.scene.evr_export_settings

        layout.label(text="Project Settings:")
        layout.prop(settings, "original_gpu_file")
        layout.prop(settings, "extracted_game_dir")
        layout.separator()
        
        layout.label(text="Import Custom Model (Replace):")
        layout.prop(settings, "auto_decimate")
        layout.prop(settings, "replace_textures")
        layout.operator("export_mesh.evr_import_replace", text="Import & Replace", icon='IMPORT')
        layout.separator()

        layout.label(text="Export Options:")
        layout.prop(settings, "export_dir")
        layout.prop(settings, "encode_mode")
        if settings.encode_mode == 'primary_described':
            layout.prop(settings, "write_primary")
            layout.prop(settings, "stream0_stride")
        layout.prop(settings, "compute_normals")
        layout.prop(settings, "export_textures")
        layout.prop(settings, "export_bones")
        layout.prop(settings, "scale")
        layout.separator()
        
        layout.label(text="Utilities:")
        row = layout.row()
        row.operator("evr.transfer_weights", text="Transfer Weights", icon='MOD_DATA_TRANSFER')
        layout.separator()
        layout.operator("export_mesh.evr_raw", text="Export EVR Mesh (Replace)", icon='EXPORT')
        layout.operator("export_mesh.evr_replace_textures", text="Replace Textures Manually", icon='TEXTURE')
        layout.operator("evr.dump_model_data", text="Dump Textures & .blend", icon='PACKAGE')


class EVR_OT_ImportAndReplace(Operator):
    bl_idname = "export_mesh.evr_import_replace"
    bl_label = "Import & Replace"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.type == 'MESH'

    def execute(self, context):
        obj = context.active_object
        settings = context.scene.evr_export_settings
        
        orig_gpu_path = bpy.path.abspath(settings.original_gpu_file)
        if not orig_gpu_path or not os.path.isfile(orig_gpu_path):
            self.report({'ERROR'}, "Please select the Original GPU File to replace.")
            return {'CANCELLED'}
            
        ext_dir = bpy.path.abspath(settings.extracted_game_dir)
        if not ext_dir or not os.path.isdir(ext_dir):
            self.report({'ERROR'}, "Please set the Extracted Game Dir in Project Settings.")
            return {'CANCELLED'}
            
        export_dir = bpy.path.abspath(settings.export_dir)
        if not export_dir:
            self.report({'ERROR'}, "Please set Export Directory.")
            return {'CANCELLED'}
            
        model_hash = os.path.splitext(os.path.basename(orig_gpu_path))[0]
        
        # 1. Texture Replacement
        if settings.replace_textures:
            from .texture_replace import TextureReplacer
            from .texture_replace import FOLDER_TEXTURE_HIGH, FOLDER_TEXTURE_LOW, FOLDER_TEXTURE_MID, parse_dds_header, build_texture_descriptor, rewrite_model_texture_mapping
            
            replacer = TextureReplacer(ext_dir, export_dir)
            
            # Get all submeshes for this model to find all materials
            export_objs = [obj]
            if obj.parent and obj.parent.type == 'EMPTY':
                export_objs = [c for c in obj.parent.children if c.type == 'MESH']
            elif len(context.selected_objects) > 1:
                export_objs = [o for o in context.selected_objects if o.type == 'MESH']

            # Extract image paths from all active object materials
            images_by_material = {}
            for eo in export_objs:
                for slot_mat in eo.material_slots:
                    mat = slot_mat.material
                    if mat and mat.use_nodes:
                        images_by_role = {}
                        for node in mat.node_tree.nodes:
                            if node.type == 'TEX_IMAGE' and node.image:
                                img = node.image
                                img_path = bpy.path.abspath(img.filepath) if getattr(img, 'filepath', None) else ""
                            
                                # If image is packed from a GLB or doesn't exist on disk, save it to temp!
                                if img.packed_file or not os.path.isfile(img_path):
                                    import tempfile
                                    temp_img_path = os.path.join(tempfile.gettempdir(), f"evr_temp_{hash(img.name)}.png")
                                    # Save to temp file safely
                                    orig_fp = img.filepath_raw
                                    try:
                                        img.filepath_raw = temp_img_path
                                        img.save()
                                        img_path = temp_img_path
                                    except Exception:
                                        # Fallback to save_render
                                        try:
                                            img.save_render(temp_img_path)
                                            img_path = temp_img_path
                                        except Exception:
                                            pass
                                    finally:
                                        img.filepath_raw = orig_fp
                                    
                                if img_path and os.path.isfile(img_path):
                                    def get_node_role(current_node, visited=None):
                                        if visited is None: visited = set()
                                        if current_node in visited: return "unknown"
                                        visited.add(current_node)
                                    
                                        for out in current_node.outputs:
                                            for link in out.links:
                                                to_node = link.to_node
                                                if to_node.type == 'BSDF_PRINCIPLED':
                                                    socket_name = link.to_socket.name
                                                    if socket_name in ('Base Color', 'Color'): return 'base_color'
                                                    if socket_name in ('Normal', 'Normal Map'): return 'normal'
                                                    if socket_name in ('Roughness', 'Metallic', 'Specular'): return 'orm'
                                                    if socket_name in ('Emission Color', 'Emission', 'Emission Strength'): return 'emissive'
                                                elif to_node.type == 'NORMAL_MAP':
                                                    return 'normal'
                                                elif to_node.type in ('SEPARATE_COLOR', 'SEPARATE_RGB'):
                                                    return 'orm'
                                                else:
                                                    r = get_node_role(to_node, visited)
                                                    if r != "unknown": return r
                                        return "unknown"
                                    
                                    role = get_node_role(node)
                                    if role != "unknown" and role not in images_by_role:
                                        images_by_role[role] = img_path
                    if images_by_role:
                        images_by_material[mat.name] = images_by_role

            if not images_by_material:
                self.report({'WARNING'}, "No texture images found on the selected model's materials.")
            else:
                replaced_count = 0
                from .texture_replace import TextureSlot
                dynamic_slots = []
                
                # Create dynamic slots based on ALL textures assigned across ALL materials
                for mat_name, roles in images_by_material.items():
                    for role, img_path in roles.items():
                        base_name = os.path.splitext(os.path.basename(img_path))[0]
                        import hashlib
                        
                        if len(base_name) == 16 and all(c in '0123456789abcdefABCDEF' for c in base_name):
                            metadata_hash = base_name.lower()
                            payload_hash = replacer._resolve_payload_hash(metadata_hash) or hashlib.md5((metadata_hash+"_payload").encode()).hexdigest()[:16]
                        else:
                            metadata_hash = hashlib.md5(base_name.encode()).hexdigest()[:16]
                            payload_hash = hashlib.md5((base_name+"_payload").encode()).hexdigest()[:16]

                        slot = TextureSlot(
                            metadata_hash=metadata_hash,
                            payload_hash=payload_hash,
                            slot_index=list(roles.keys()).index(role),
                            skin_id=0,
                        )
                        slot.low_path = replacer._find_texture_file(metadata_hash, FOLDER_TEXTURE_LOW)
                        slot.mid_path = replacer._find_texture_file(metadata_hash, FOLDER_TEXTURE_MID)
                        slot.high_path = replacer._find_texture_file(payload_hash, FOLDER_TEXTURE_HIGH)

                        dynamic_slots.append((slot, img_path))
                
                for slot, img_path in dynamic_slots:
                    self.report({'INFO'}, f"Replacing dynamically found texture {slot.metadata_hash}...")
                    res = replacer.replace_texture(slot, img_path)
                    if res.get("errors"):
                        for err in res["errors"]:
                            self.report({'ERROR'}, f"Texture error: {err}")
                    if res.get("high_written") or res.get("low_written"):
                        replaced_count += 1
                
                # BLANKET REPLACE LOGIC:
                # The user wants to override ALL original textures of the model with the new custom textures.
                if dynamic_slots:
                    first_custom_slot = dynamic_slots[0][0]
                    first_custom_hash = first_custom_slot.metadata_hash
                    
                    # Read the custom texture's MID DDS data
                    custom_mid_path = os.path.join(export_dir, FOLDER_TEXTURE_MID, first_custom_hash)
                    if os.path.exists(custom_mid_path):
                        with open(custom_mid_path, 'rb') as f:
                            custom_dds_data_original = f.read()
                        
                        original_slots = replacer.find_model_textures(model_hash)
                        dynamic_meta_hashes = {s[0].metadata_hash for s in dynamic_slots}
                        blanket_count = 0
                            
                        if original_slots:
                            for o_slot in original_slots:
                                if o_slot.metadata_hash in dynamic_meta_hashes:
                                    continue  # Already replaced
                                    
                                # Read original DXGI format to prevent shiny/smoothed out artifacts
                                orig_dxgi = 0
                                orig_low_path = replacer._find_texture_file(o_slot.metadata_hash, "BCE9C410B354B078")
                                if not orig_low_path:
                                    orig_low_path = replacer._find_texture_file(o_slot.metadata_hash, FOLDER_TEXTURE_LOW)
                                
                                if orig_low_path and os.path.exists(orig_low_path):
                                    with open(orig_low_path, 'rb') as f:
                                        orig_data = f.read(256)
                                    if len(orig_data) >= 256:
                                        orig_dxgi = struct.unpack_from('<I', orig_data, 192+24)[0]

                                if orig_dxgi == 83: # BC5_UNORM (Normal Map)
                                    custom_dds_data = bytes.fromhex('444453207c00000007100a000400000004000000100000000100000001000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000020000000040000004458313000000000000000000000000000000000000000000010000000000000000000000000000000000000530000000300000000000000010000000000000080800000000000008080000000000000') # 4x4 BC5
                                    synthetic_desc = build_texture_descriptor(4, 4, 1, 83, len(custom_dds_data), srgb=0)
                                elif orig_dxgi in (71, 98, 77, 95): # BC1/BC7 UNORM (ARM / Roughness / Emissive)
                                    custom_dds_data = bytes.fromhex('444453207c00000007100a0004000000040000000800000001000000010000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000200000000400000044583130000000000000000000000000000000000000000000100000000000000000000000000000000000004700000003000000000000000100000000000000ffffffff00000000') # 4x4 BC1
                                    synthetic_desc = build_texture_descriptor(4, 4, 1, 71, len(custom_dds_data), srgb=0)
                                else: # SRGB formats (Albedo) or Unknown
                                    w, h, mips, dxgi = parse_dds_header(custom_dds_data_original)
                                    if w == 0:
                                        self.report({'WARNING'}, f"Failed to parse custom DDS for blanket replace.")
                                        continue
                                    synthetic_desc = build_texture_descriptor(w, h, mips, dxgi, len(custom_dds_data_original), srgb=1)
                                    custom_dds_data = custom_dds_data_original

                                # 3. Clean up old HIGH bucket file if it exists, since synthetic descriptors don't use it
                                out_high_dir = os.path.join(export_dir, FOLDER_TEXTURE_HIGH)
                                out_high_path = os.path.join(out_high_dir, o_slot.payload_hash)
                                if os.path.exists(out_high_path):
                                    try: os.remove(out_high_path)
                                    except: pass

                                # 4. Overwrite LOW file with synthetic descriptor
                                out_low_dir = os.path.join(export_dir, FOLDER_TEXTURE_LOW)
                                os.makedirs(out_low_dir, exist_ok=True)
                                out_low_path = os.path.join(out_low_dir, o_slot.metadata_hash)
                                try:
                                    with open(out_low_path, 'wb') as f:
                                        f.write(synthetic_desc)
                                except Exception as e:
                                    self.report({'WARNING'}, f"Failed to write LOW descriptor for {o_slot.metadata_hash}: {e}")

                                # 5. Overwrite MID file with full raw DDS
                                out_mid_dir = os.path.join(export_dir, FOLDER_TEXTURE_MID)
                                os.makedirs(out_mid_dir, exist_ok=True)
                                out_mid_path = os.path.join(out_mid_dir, o_slot.metadata_hash)
                                try:
                                    with open(out_mid_path, 'wb') as f:
                                        f.write(custom_dds_data)
                                    blanket_count += 1
                                except Exception as e:
                                    self.report({'WARNING'}, f"Failed to write MID DDS for {o_slot.metadata_hash}: {e}")

                            self.report({'INFO'}, f"Replaced {replaced_count} assigned textures, and blanket-patched {blanket_count} original textures to point to the custom texture!")
                else:
                    self.report({'INFO'}, f"Replaced {replaced_count} textures dynamically based on Blender materials.")
                    
        # 2. Mesh replacement (calls export_mesh.evr_raw)
        bpy.ops.export_mesh.evr_raw()
        return {'FINISHED'}


class EVR_OT_ExportMesh(Operator):
    bl_idname = "export_mesh.evr_raw"
    bl_label = "EVR Raw Mesh (Replace)"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context): return context.active_object and context.active_object.type == 'MESH'

    def _get_all_submeshes(self, context, is_cgml, obj):
        # Auto-collect all related meshes in the model hierarchy
        if not is_cgml:
            return [obj]
        if obj.parent and obj.parent.type == 'EMPTY':
            export_objs = [c for c in obj.parent.children if c.type == 'MESH']
        else:
            export_objs = [o for o in context.selected_objects if o.type == 'MESH']
        if not export_objs:
            export_objs = [obj]
        export_objs.sort(key=lambda x: x.get('evr_material_index', 9999))
        return export_objs

    def execute(self, context):
        obj = context.active_object
        settings = context.scene.evr_export_settings
        
        orig_gpu_path = bpy.path.abspath(settings.original_gpu_file)
        export_dir = bpy.path.abspath(settings.export_dir)

        if not os.path.isfile(orig_gpu_path):
            self.report({'ERROR'}, "Selected original GPU file does not exist.")
            return {'CANCELLED'}

        if not export_dir or not os.path.isdir(export_dir):
            try:
                os.makedirs(export_dir, exist_ok=True)
            except Exception as e:
                self.report({'ERROR'}, f"Invalid export directory: {e}")
                return {'CANCELLED'}

        is_cgml = 'CGMeshListResource' in orig_gpu_path or 'e642bfb1abcf76df' in orig_gpu_path
        hash_name = os.path.basename(orig_gpu_path)

        if is_cgml:
            out_gpu_dir = os.path.join(export_dir, 'e642bfb1abcf76df')
            out_pri_dir = os.path.join(export_dir, '4e426f88c1b5d7ac')
        else:
            out_gpu_dir = os.path.join(export_dir, 'e7a8ab5ceaef49cb')
            out_pri_dir = os.path.join(export_dir, '37102e4b27955a14')

        out_gpu_path = os.path.join(out_gpu_dir, hash_name)
        out_pri_path = os.path.join(out_pri_dir, hash_name)

        ratio = 1.0
        max_attempts = 10

        for attempt in range(max_attempts):
            try:
                if settings.encode_mode == 'cgml' or (settings.encode_mode == 'primary_described' and is_cgml):
                    export_objs = self._get_all_submeshes(context, is_cgml, obj)
                    submeshes = []
                    do_split = (len(export_objs) == 1)
                    for eo in export_objs:
                        if do_split:
                            submeshes.extend(mesh_from_blender_object(eo, apply_transforms=True, split_by_material=True))
                        else:
                            submeshes.append(mesh_from_blender_object(eo, apply_transforms=True, split_by_material=False))
                else:
                    res = mesh_from_blender_object(obj, apply_transforms=True, split_by_material=False)
                    verts, faces, uvs = res[0], res[1], res[2]
                    bone_data = res[3] if len(res) > 3 else None
                    normals = res[4] if len(res) > 4 else None
                    tangents = res[5] if len(res) > 5 else None
                    colors = res[6] if len(res) > 6 else None

                if settings.scale != 1.0:
                    s = settings.scale
                    if settings.encode_mode == 'cgml' or (settings.encode_mode == 'primary_described' and is_cgml):
                        submeshes = [([(x*s, y*s, z*s) for x,y,z in sub[0]], *sub[1:]) for sub in submeshes]
                    else: 
                        verts = [(x*s, y*s, z*s) for x,y,z in verts]

                cn = settings.compute_normals
                os.makedirs(out_gpu_dir, exist_ok=True)

                if settings.encode_mode == 'heuristic_s16':
                    self._write_gpu(out_gpu_path, encode_heuristic_s16(verts, faces, uvs=uvs, bone_data=bone_data, compute_normals=cn))
                    self.report({'INFO'}, f"Saved GPU to {out_gpu_path}")
                elif settings.encode_mode == 'heuristic_s20':
                    self._write_gpu(out_gpu_path, encode_heuristic_s20(verts, faces, uvs=uvs, bone_data=bone_data, compute_normals=cn))
                    self.report({'INFO'}, f"Saved GPU to {out_gpu_path}")
                elif settings.encode_mode == 'heuristic_dual28':
                    self._write_gpu(out_gpu_path, encode_heuristic_dual28(verts, faces, bone_data=bone_data, compute_normals=cn))
                    self.report({'INFO'}, f"Saved GPU to {out_gpu_path}")
                elif settings.encode_mode == 'cgml':
                    self._write_gpu(out_gpu_path, encode_cgml(submeshes, compute_normals=cn))
                    self.report({'INFO'}, f"Saved GPU to {out_gpu_path}")
                elif settings.encode_mode == 'primary_described':
                    s0_stride = None if settings.stream0_stride == 'auto' else int(settings.stream0_stride)
                    from .primary import _find_primary_path
                    orig_primary_path = _find_primary_path(orig_gpu_path)
                    
                    if orig_primary_path and os.path.exists(orig_primary_path):
                        with open(orig_gpu_path, 'rb') as f: orig_gpu = f.read()
                        with open(orig_primary_path, 'rb') as f: orig_primary = f.read()
                        
                        if is_cgml:
                            from .encode import encode_cgml_primary_replace
                            gpu_data, primary_data = encode_cgml_primary_replace(
                                orig_gpu, orig_primary, submeshes, stream0_stride=s0_stride, compute_normals=cn
                            )
                        else:
                            from .encode import encode_primary_described_full_replace
                            gpu_data, primary_data = encode_primary_described_full_replace(
                                orig_gpu, orig_primary, verts, faces, uvs=uvs, bone_data=bone_data, normals=normals, tangents=tangents, stream0_stride=s0_stride, compute_normals=cn
                            )

                        self._write_gpu(out_gpu_path, gpu_data)
                        if settings.write_primary:
                            os.makedirs(out_pri_dir, exist_ok=True)
                            with open(out_pri_path, 'wb') as f: f.write(primary_data)
                            self.report({'INFO'}, f"Saved GPU to {out_gpu_path} and Primary to {out_pri_path}")
                        else:
                            self.report({'INFO'}, f"Saved GPU to {out_gpu_path}")
                    else:
                        self.report({'ERROR'}, f"Could not find original Primary file for {orig_gpu_path}")
                        return {'CANCELLED'}
                        
                if settings.export_textures:
                    ext_dir = bpy.path.abspath(settings.extracted_game_dir)
                    if ext_dir and os.path.isdir(ext_dir):
                        from .texture_replace import TextureReplacer
                        replacer = TextureReplacer(ext_dir, export_dir)
                        written_tex = replacer.export_model_textures(hash_name, overwrite=False)
                        if written_tex:
                            self.report({'INFO'}, f"Exported {len(written_tex)} associated textures.")
                        else:
                            self.report({'INFO'}, "No associated textures found to export.")
                    else:
                        self.report({'WARNING'}, "Cannot export textures: Extracted Game Dir not set or invalid.")
                        
                break # Successfully saved, exit retry loop

            except VertexLimitError as e:
                if settings.auto_decimate and attempt < max_attempts - 1:
                    target_ratio = (e.max_verts / e.current_verts) * 0.85
                    ratio = max(ratio * target_ratio, 0.01)
                    self.report({'INFO'}, f"Vertex limit exceeded ({e.current_verts} > {e.max_verts}). Auto-decimating (Ratio: {ratio:.3f})")
                    continue
                else:
                    self.report({'ERROR'}, str(e))
                    return {'CANCELLED'}

            except ValueError as e:
                self.report({'ERROR'}, str(e))
                return {'CANCELLED'}
            except Exception as e:
                self.report({'ERROR'}, f"Encode failed: {e}")
                return {'CANCELLED'}

        return {'FINISHED'}

    def _write_gpu(self, path, data):
        with open(path, 'wb') as f: f.write(data)

def menu_func_export(self, context): self.layout.operator(EVR_OT_ExportMesh.bl_idname, text="EVR Raw Mesh (Replace)")

class EVR_AssetSwapperSettings(bpy.types.PropertyGroup):
    target_hash: StringProperty(
        name="Target Hash",
        description="The hash of the model you are replacing (e.g. your custom prop)",
        default=""
    )
    source_hash: StringProperty(
        name="Source Hash",
        description="The hash of the game object whose properties/collisions you want to steal",
        default=""
    )
    swap_collision: BoolProperty(
        name="Swap Collision Mesh",
        description="Transfers the Havok collision geometry from the source to the target",
        default=True
    )
    swap_def: BoolProperty(
        name="Swap Model Definition",
        description="Transfers the physics weight, mass, and surface properties from the source to the target",
        default=True
    )
    extracted_dir: StringProperty(
        name="PCVR Extracted Dir",
        description="Directory containing the extracted game folders (e.g. G:\\pcvr-extracted)",
        subtype='DIR_PATH',
        default=r"G:\pcvr-extracted"
    )

class EVR_OT_SwapAssets(Operator):
    """Swap collisions and physics definitions using Hash Spoofing"""
    bl_idname = "evr.swap_assets"
    bl_label = "Spoof Hashes (Apply Swap)"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        settings = context.scene.evr_swapper_settings
        extracted_dir = bpy.path.abspath(settings.extracted_dir)
        
        target = settings.target_hash.strip()
        source = settings.source_hash.strip()
        
        if not target or not source:
            self.report({'ERROR'}, "Both Target Hash and Source Hash must be provided.")
            return {'CANCELLED'}
            
        if not os.path.exists(extracted_dir):
            self.report({'ERROR'}, f"Extracted directory not found: {extracted_dir}")
            return {'CANCELLED'}

        from .textures import get_all_name_variations
        
        target_vars = get_all_name_variations(target)
        source_vars = get_all_name_variations(source)
        
        folders_to_swap = []
        if settings.swap_collision:
            folders_to_swap.append(('b7d338793fa37832', "Collision Mesh"))
        if settings.swap_def:
            folders_to_swap.append(('46adff5980245670', "Model Definition"))
            
        success_count = 0
        
        for folder, name in folders_to_swap:
            folder_path = os.path.join(extracted_dir, folder)
            if not os.path.exists(folder_path):
                self.report({'WARNING'}, f"{name} folder not found in extracted dir.")
                continue
                
            # Find the source file
            source_file = None
            for var in source_vars:
                cand = os.path.join(folder_path, var)
                if os.path.exists(cand):
                    source_file = cand
                    break
                    
            if not source_file:
                self.report({'WARNING'}, f"Could not find {name} for Source Hash in {folder}.")
                continue
                
            target_name = None
            for var in target_vars:
                if len(var) == len(os.path.basename(source_file)):
                    target_name = var
                    break
            if not target_name: target_name = target
            
            target_file = os.path.join(folder_path, target_name)
            
            try:
                shutil.copy2(source_file, target_file)
                self.report({'INFO'}, f"Copied {name} from {os.path.basename(source_file)} to {target_name}")
                success_count += 1
            except Exception as e:
                self.report({'ERROR'}, f"Failed to copy {name}: {e}")
                
        if success_count > 0:
            self.report({'INFO'}, f"Successfully swapped {success_count} asset(s)!")
            return {'FINISHED'}
        else:
            self.report({'WARNING'}, "No assets were swapped.")
            return {'CANCELLED'}

class EVR_OT_DumpModelData(Operator):
    bl_idname = "evr.dump_model_data"
    bl_label = "Dump Textures & .blend"
    bl_options = {'REGISTER', 'UNDO'}

    directory: StringProperty(subtype='DIR_PATH')

    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.type in {'MESH', 'ARMATURE', 'EMPTY'}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        obj = context.active_object
        dump_dir = self.directory
        if not dump_dir:
            return {'CANCELLED'}
            
        if not os.path.isdir(dump_dir):
            os.makedirs(dump_dir, exist_ok=True)
            
        objs_to_export = []
        def collect_children(o):
            if o not in objs_to_export:
                objs_to_export.append(o)
                for child in o.children:
                    collect_children(child)
                    
        # Find root of the selected object hierarchy
        root = obj
        while root.parent:
            root = root.parent
        collect_children(root)
        
        copied_images = {}
        for o in objs_to_export:
            if o.type == 'MESH':
                for mat_slot in o.material_slots:
                    if mat_slot.material and mat_slot.material.use_nodes:
                        for node in mat_slot.material.node_tree.nodes:
                            if node.type == 'TEX_IMAGE' and node.image:
                                img = node.image
                                if img.name not in copied_images:
                                    tex_dir = os.path.join(dump_dir, "textures")
                                    os.makedirs(tex_dir, exist_ok=True)
                                    
                                    # Clean up the name to ensure it has a valid extension
                                    basename = img.name
                                    if not basename.lower().endswith(('.png', '.jpg', '.jpeg', '.tga')):
                                        basename += '.png'
                                        
                                    dst = os.path.join(tex_dir, basename)
                                    src = bpy.path.abspath(img.filepath) if getattr(img, 'filepath', None) else ""
                                    
                                    try:
                                        if img.packed_file or not os.path.exists(src):
                                            # Image is packed or doesn't exist on disk, save it from memory
                                            old_fp = img.filepath_raw
                                            img.filepath_raw = dst
                                            if dst.lower().endswith('.png'): img.file_format = 'PNG'
                                            img.save()
                                            img.filepath_raw = old_fp
                                            copied_images[img.name] = dst
                                        else:
                                            # Exists on disk, just copy it directly
                                            import shutil
                                            shutil.copy2(src, dst)
                                            copied_images[img.name] = dst
                                    except Exception as e:
                                        print(f"Failed to copy texture: {e}")

        # Fallback: if no textures were found in Blender's material nodes, dump the raw game textures directly
        if not copied_images:
            model_hash = root.name.split('_')[0]
            
            possible_ext_dirs = []
            if hasattr(context.scene, "evr_swapper_settings") and getattr(context.scene.evr_swapper_settings, "pcvr_extracted_dir", ""):
                possible_ext_dirs.append(context.scene.evr_swapper_settings.pcvr_extracted_dir)
            if getattr(context.scene.evr_export_settings, "extracted_game_dir", ""):
                possible_ext_dirs.append(context.scene.evr_export_settings.extracted_game_dir)
            
            from .textures import discover_paths
            disc = discover_paths()
            if disc.get("pcvr_extracted"):
                possible_ext_dirs.append(disc.get("pcvr_extracted"))
                
            mapping = None
            ext_dir = ""
            for pd in possible_ext_dirs:
                if pd and os.path.exists(pd):
                    try:
                        from .textures import parse_materials_mapping
                        m = parse_materials_mapping(pd, model_hash)
                        if m and "textures" in m:
                            mapping = m
                            ext_dir = pd
                            break
                    except: pass
            
            if mapping:
                tex_dir = os.path.join(dump_dir, "textures")
                os.makedirs(tex_dir, exist_ok=True)
                import shutil
                import tempfile
                
                texconv_path = os.path.join(os.path.dirname(__file__), "bin", "texconv.exe")
                from .texture_decoder import decode_texture
                
                fallback_errors = []
                
                for tex_hash in set(mapping["textures"]):
                    # Generate a temporary PNG path
                    temp_png = os.path.join(tempfile.gettempdir(), f"dump_{tex_hash}.png")
                    dst = os.path.join(tex_dir, f"{tex_hash}.png")
                    
                    # Use decode_texture to compile high/low and convert to PNG
                    if os.path.exists(texconv_path):
                        success = decode_texture(tex_hash, ext_dir, temp_png, texconv_path)
                        if success and os.path.exists(temp_png):
                            try:
                                shutil.copy2(temp_png, dst)
                                copied_images[tex_hash] = dst
                                os.remove(temp_png)
                            except Exception as e:
                                fallback_errors.append(f"Copy {tex_hash}: {e}")
                        else:
                            fallback_errors.append(f"Decode failed {tex_hash}")
                    else:
                        fallback_errors.append("texconv.exe missing")
                        break
                        
                if not copied_images and fallback_errors:
                    self.report({'WARNING'}, f"Found mapping, but decoding failed: {fallback_errors[0]}")
            else:
                self.report({'WARNING'}, f"Could not find materials mapping for {model_hash} in any extraction folder!")

        original_paths = {}
        for img_name, dst in copied_images.items():
            img = bpy.data.images.get(img_name)
            if img:
                original_paths[img.name] = img.filepath
                img.filepath = "//textures/" + os.path.basename(dst)
                
        blend_path = os.path.join(dump_dir, f"{root.name}_dump.blend")
        try:
            bpy.ops.wm.save_as_mainfile(filepath=blend_path, copy=True)
            if len(copied_images) > 0:
                self.report({'INFO'}, f"Dumped to {dump_dir} with {len(copied_images)} textures")
            else:
                self.report({'WARNING'}, f"Dumped to {dump_dir} BUT NO TEXTURES WERE FOUND ON THE MESH!")
        except Exception as e:
            self.report({'ERROR'}, f"Failed to dump blend: {e}")
            
        for img_name, orig_path in original_paths.items():
            img = bpy.data.images.get(img_name)
            if img:
                img.filepath = orig_path

        return {'FINISHED'}

class EVR_PT_AssetSwapperPanel(bpy.types.Panel):
    bl_label = "Asset Swapper (Collisions)"
    bl_idname = "EVR_PT_asset_swapper_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "EVR Tools"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.evr_swapper_settings

        layout.label(text="Implicit Hash Spoofing", icon='FILE_REFRESH')
        
        box = layout.box()
        box.prop(settings, "target_hash")
        box.prop(settings, "source_hash")
        
        layout.separator()
        layout.prop(settings, "extracted_dir")
        
        layout.separator()
        layout.label(text="Properties to Steal:")
        row = layout.row()
        row.prop(settings, "swap_collision")
        row.prop(settings, "swap_def")
        
        layout.separator()
        layout.operator("evr.swap_assets", icon='UV_SYNC_SELECT')


# --- TEXTURE REPLACEMENT UI ---
import json
import random
import struct

_texture_items_cache = {}
_global_texture_mapping = None

def _get_global_texture_mapping():
    global _global_texture_mapping
    if _global_texture_mapping is not None:
        return _global_texture_mapping
        
    _global_texture_mapping = {}
    _dir = os.path.dirname(os.path.abspath(__file__))
    if os.path.basename(_dir) == "__pycache__":
        _dir = os.path.dirname(_dir)
        
    mapping_path = os.path.join(_dir, "texture_mapping.json")
    if os.path.exists(mapping_path):
        try:
            with open(mapping_path, 'r') as f:
                _global_texture_mapping = json.load(f)
        except Exception as e:
            print(f"[EVR Mesh Importer] Error loading texture_mapping.json: {e}")
            
    return _global_texture_mapping

def _resolve_pcvr_dir(context, orig_path):
    import os
    from .textures import discover_paths, get_all_name_variations
    
    def _is_valid(p):
        if not p or not os.path.exists(p): return False
        for h in ("c2434c5a99e139ce", "23d48cecc462abe7"):
            for v in get_all_name_variations(h):
                if os.path.exists(os.path.join(p, v)): return True
        return False

    # 1. Use user's explicit setting from the export panel
    if hasattr(context, "scene") and hasattr(context.scene, "evr_export_settings"):
        p = context.scene.evr_export_settings.pcvr_extracted_dir
        if _is_valid(p): return p

    # 2. Check if mesh has the pcvr extracted dir stored from import
    if hasattr(context, "active_object") and context.active_object and context.active_object.get("evr_pcvr_extracted"):
        p = context.active_object["evr_pcvr_extracted"]
        if _is_valid(p): return p

    # 3. Derive from original GPU file path
    if orig_path:
        p = os.path.dirname(os.path.dirname(orig_path))
        if _is_valid(p): return p

    # 4. Fallback to discover_paths
    disc = discover_paths()
    p = disc.get('pcvr_extracted')
    if _is_valid(p): return p
    
    # Best effort fallback (even if invalid)
    if orig_path:
        return os.path.dirname(os.path.dirname(orig_path))
    return None

def get_texture_items(self, context):
    orig_path = context.scene.evr_export_settings.original_gpu_file
    if not orig_path or not os.path.isfile(orig_path): return [("NONE", "No Original GPU File set", "")]
    model_hash = os.path.splitext(os.path.basename(orig_path))[0]
    if model_hash in _texture_items_cache:
        return _texture_items_cache[model_hash]
    
    from .textures import parse_materials_mapping, get_all_name_variations, discover_paths
    
    pcvr_dir = _resolve_pcvr_dir(context, orig_path)
    
    if not pcvr_dir or not os.path.exists(pcvr_dir):
        return [("NONE", "PCVR Extracted Dir not found", "")]
        
    mapping = parse_materials_mapping(pcvr_dir, model_hash)
    
    items = []
    if mapping and mapping.get('textures'):
        # Build search dirs: combine texture caches, local texture/ folder, AND pcvr-extracted subfolders
        user_cache = context.scene.evr_export_settings.texture_cache_dir
        obj_cache = None
        if hasattr(context, "active_object") and context.active_object and context.active_object.get("evr_texture_cache"):
            obj_cache = context.active_object["evr_texture_cache"]
        
        t_cache = bpy.path.abspath(user_cache).strip() if user_cache else None
        
        search_dirs = []
        if obj_cache and os.path.exists(obj_cache) and obj_cache not in search_dirs:
            search_dirs.append(obj_cache)
        if t_cache and os.path.exists(t_cache) and t_cache not in search_dirs:
            search_dirs.append(t_cache)
        # ALSO search the addon's local texture/ directory for converted DDS files
        # This works both when running from source checkout AND when installed in Blender
        local_tex_found = False
        for search_root in [os.path.dirname(os.path.dirname(os.path.abspath(__file__))), os.path.dirname(os.path.abspath(__file__))]:
            local_tex_dir = os.path.join(search_root, "texture")
            if os.path.exists(local_tex_dir) and local_tex_dir not in search_dirs:
                search_dirs.append(local_tex_dir)
                local_tex_found = True
                break
        # Also check the common dev checkout path in case Blender's __file__ resolves differently
        if not local_tex_found:
            for alt in [r"J:\EchoVR-Tools-Launcher\evr-mesh-importer\texture"]:
                if os.path.exists(alt) and alt not in search_dirs:
                    search_dirs.append(alt)
        # ALSO search pcvr-extracted texture subfolders (raw game textures, no DDS header)
        if pcvr_dir and os.path.exists(pcvr_dir):
            for sub in ["ae49fad43254367a", "4a4c32c49300b8a0"]:
                sub_path = os.path.join(pcvr_dir, sub)
                if os.path.isdir(sub_path) and sub_path not in search_dirs:
                    search_dirs.append(sub_path)
        
        # Helper to find which textures are actually used on the active object's materials
        assigned_vars = set()
        try:
            obj = getattr(context, "active_object", None)
            if obj and obj.type == 'MESH':
                for mat in obj.data.materials:
                    if mat and mat.use_nodes:
                        for node in mat.node_tree.nodes:
                            if node.type == 'TEX_IMAGE' and node.image and node.image.filepath:
                                name = os.path.splitext(os.path.basename(node.image.filepath))[0]
                                assigned_vars.add(name.lower())
        except Exception:
            pass

        # Load the global texture mapping to resolve metadata -> payload hashes
        global_mapping = _get_global_texture_mapping()

        found_hashes = []
        for thash in set(mapping['textures']):
            payload_hash = global_mapping.get(thash, thash)
            
            # variations for finding the file on disk
            vars = get_all_name_variations(payload_hash)
            meta_vars = get_all_name_variations(thash)

            # We no longer filter by assigned_vars so that ALL textures for the model show up in the dropdown
            # even if the material was deleted or the model hasn't been fully imported.
            
            # Priority order: first check user's explicitly-set cache dir for DDS,
            # then check all other dirs
            dds_path = None
            raw_path = None
            dds_size = -1
            raw_size = -1
            
            # Phase 1: Only search user's explicit texture_cache_dir for DDS files
            if t_cache and os.path.exists(t_cache):
                for v in vars:
                    p = os.path.join(t_cache, f"{v}.dds")
                    if os.path.exists(p):
                        sz = os.path.getsize(p)
                        if sz > dds_size:
                            dds_size = sz
                            dds_path = p
            
            # Phase 2: Search all other dirs for any format
            for d in search_dirs:
                if d == t_cache: continue  # already searched
                if not os.path.exists(d): continue
                # Try .dds file
                for v in vars:
                    p = os.path.join(d, f"{v}.dds")
                    if os.path.exists(p):
                        sz = os.path.getsize(p)
                        if sz > dds_size:
                            dds_size = sz
                            dds_path = p
                # Try raw binary file (pcvr-extracted raw texture, no extension)
                for v in vars:
                    p = os.path.join(d, v)
                    if os.path.exists(p) and os.path.isfile(p) and not v.endswith('.dds'):
                        sz = os.path.getsize(p)
                        if sz > raw_size:
                            raw_size = sz
                            raw_path = p
            
            # Always prefer DDS over raw for preview
            if dds_size >= 0:
                found_hashes.append((thash, payload_hash, dds_size, "DDS"))
            elif raw_size >= 0:
                found_hashes.append((thash, payload_hash, raw_size, "RAW"))
        
        # Sort by size descending so high quality textures are at the top, DDS preferred
        found_hashes.sort(key=lambda x: (0 if x[3] == "DDS" else 1, -x[2]))
        for thash, payload_hash, size, ftype in found_hashes:
            
            hash_display = f"{thash} -> {payload_hash}" if thash != payload_hash else thash
            
            if ftype == "RAW":
                label = f"{hash_display} ({size/1024:.0f} KB - raw)"
            else:
                mb = size / (1024*1024)
                if mb >= 1.0:
                    label = f"{hash_display} ({mb:.1f} MB)"
                else:
                    label = f"{hash_display} ({size/1024:.0f} KB)"
            items.append((thash, label, ""))
            
    if not items:
        items = [("NONE", "No cached textures found", "")]
    
    _texture_items_cache[model_hash] = items
    return items

def update_texture_preview(self, context):
    target_hash = self.texture_to_replace
    if target_hash == "NONE":
        context.scene.evr_preview_image = None
        return
        
    import os
    from .textures import get_all_name_variations, discover_paths
    
    # Build search dirs: combine texture caches, local texture/ folder, AND pcvr-extracted subfolders
    user_cache = context.scene.evr_export_settings.texture_cache_dir
    obj_cache = None
    if hasattr(context, "active_object") and context.active_object and context.active_object.get("evr_texture_cache"):
        obj_cache = context.active_object["evr_texture_cache"]

    t_cache = bpy.path.abspath(user_cache).strip() if user_cache else None

    search_dirs = []
    if obj_cache and os.path.exists(obj_cache) and obj_cache not in search_dirs:
        search_dirs.append(obj_cache)
    if t_cache and os.path.exists(t_cache) and t_cache not in search_dirs:
        search_dirs.append(t_cache)
    # ALSO search the addon's local texture/ directory for converted DDS files
    # This works both when running from source checkout AND when installed in Blender
    local_tex_found = False
    for search_root in [os.path.dirname(os.path.dirname(os.path.abspath(__file__))), os.path.dirname(os.path.abspath(__file__))]:
        local_tex_dir = os.path.join(search_root, "texture")
        if os.path.exists(local_tex_dir) and local_tex_dir not in search_dirs:
            search_dirs.append(local_tex_dir)
            local_tex_found = True
            break
    # Also check the common dev checkout path in case Blender's __file__ resolves differently
    if not local_tex_found:
        for alt in [r"J:\EchoVR-Tools-Launcher\evr-mesh-importer\texture"]:
            if os.path.exists(alt) and alt not in search_dirs:
                search_dirs.append(alt)
    # ALSO search pcvr-extracted texture subfolders
    pcvr_dir = _resolve_pcvr_dir(context, orig_path=context.scene.evr_export_settings.original_gpu_file)
    if pcvr_dir and os.path.exists(pcvr_dir):
        for sub in ["ae49fad43254367a", "4a4c32c49300b8a0"]:
            sub_path = os.path.join(pcvr_dir, sub)
            if os.path.isdir(sub_path) and sub_path not in search_dirs:
                search_dirs.append(sub_path)
    
    preview_img = None
    max_size = -1
    
    global_mapping = _get_global_texture_mapping()
    payload_hash = global_mapping.get(target_hash, target_hash)
    
    vars_payload = get_all_name_variations(payload_hash)
    vars_meta = get_all_name_variations(target_hash)
    
    for d in search_dirs:
        if not os.path.exists(d): continue
        for h_var in vars_payload + vars_meta:
            # Try .dds first (cached DDS files)
            p = os.path.join(d, h_var + ".dds")
            if os.path.exists(p):
                sz = os.path.getsize(p)
                if sz > max_size:
                    max_size = sz
                    preview_img = p
            # Also try raw binary (pcvr-extracted texture has no extension)
            p_raw = os.path.join(d, h_var)
            if os.path.exists(p_raw) and not p_raw.endswith(".dds"):
                sz = os.path.getsize(p_raw)
                if sz > max_size:
                    max_size = sz
                    preview_img = p_raw
        
        if preview_img:
            try:
                # Only load .dds for preview — raw Echo textures can't be loaded directly
                if preview_img.lower().endswith('.dds'):
                    img = bpy.data.images.load(preview_img, check_existing=True)
                    img.preview_ensure()
                    context.scene.evr_preview_image = img
                else:
                    # Raw texture in pcvr-extracted — try to find a .dds version in cache instead
                    fallback = None
                    for d in search_dirs:
                        if not os.path.exists(d): continue
                        for h_var in get_all_name_variations(target_hash):
                            p_dds = os.path.join(d, h_var + ".dds")
                            if os.path.exists(p_dds):
                                fallback = p_dds
                                break
                        if fallback: break
                    if fallback:
                        img = bpy.data.images.load(fallback, check_existing=True)
                        img.preview_ensure()
                        context.scene.evr_preview_image = img
                    else:
                        context.scene.evr_preview_image = None
            except Exception:
                context.scene.evr_preview_image = None
        else:
            context.scene.evr_preview_image = None

class EVR_OT_ReplaceTextures(bpy.types.Operator):
    bl_idname = "export_mesh.evr_replace_textures"
    bl_label = "Replace Textures for this Model"
    bl_options = {'REGISTER', 'UNDO'}

    texture_to_replace: bpy.props.EnumProperty(
        name="Texture to Replace",
        description="Select which texture to replace",
        items=get_texture_items,
        update=update_texture_preview
    )
    
    replacement_dds: bpy.props.StringProperty(
        name="Replacement DDS",
        description="Path to the new .dds file you want to use",
        subtype='FILE_PATH'
    )
    
    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.type == 'MESH'

    def invoke(self, context, event):
        orig_path = context.scene.evr_export_settings.original_gpu_file
        if not orig_path or not os.path.isfile(orig_path):
            self.report({'ERROR'}, "Please set the 'Original GPU File' in the Export Panel first.")
            return {'CANCELLED'}
            
        model_hash = os.path.splitext(os.path.basename(orig_path))[0]
        
        if model_hash in _texture_items_cache:
            del _texture_items_cache[model_hash]
            
        items = get_texture_items(self, context)
        if not items or items[0][0] == "NONE":
            self.report({'ERROR'}, f"No textures found for model {model_hash}")
            return {'CANCELLED'}
            
        # Force the initial preview update
        if items and items[0][0] != "NONE":
            self.texture_to_replace = items[0][0]
            update_texture_preview(self, context)
        else:
            context.scene.evr_preview_image = None
            
        return context.window_manager.invoke_props_dialog(self, width=500)

    def draw(self, context):
        layout = self.layout
        layout.label(text="Texture Preview:")
        if context.scene.evr_preview_image:
            img = context.scene.evr_preview_image
            if img.preview:
                layout.template_icon(icon_value=img.preview.icon_id, scale=10.0)
            else:
                layout.label(text="Preview generating...", icon='INFO')
        else:
            layout.label(text="No .dds preview found in cache.", icon='ERROR')
            
        layout.separator()
        layout.prop(self, "texture_to_replace")
        layout.prop(self, "replacement_dds")

    def execute(self, context):
        if not self.replacement_dds or not os.path.exists(self.replacement_dds):
            self.report({'ERROR'}, "Please select a valid replacement DDS file.")
            return {'CANCELLED'}
            
        target_hash = self.texture_to_replace
        if target_hash == "NONE":
            self.report({'ERROR'}, "No valid texture selected.")
            return {'CANCELLED'}
            
        with open(self.replacement_dds, 'rb') as f:
            magic = f.read(4)
            if magic != b'DDS ':
                self.report({'ERROR'}, "Selected file is not a valid DDS.")
                return {'CANCELLED'}
            f.seek(84)
            fourcc = f.read(4)
            header_size = 148 if fourcc == b'DX10' else 128
            f.seek(header_size)
            raw_data = f.read()
            
        from .textures import get_all_name_variations, discover_paths
        orig_path = context.scene.evr_export_settings.original_gpu_file
        pcvr_dir = _resolve_pcvr_dir(context, orig_path)
            
        if not pcvr_dir or not os.path.exists(pcvr_dir):
            self.report({'ERROR'}, "Cannot determine PCVR dir from Original GPU File path.")
            return {'CANCELLED'}
            
        global_mapping = _get_global_texture_mapping()
        payload_hash = global_mapping.get(target_hash, target_hash)

        # Find original texture size by checking all subfolders in pcvr_dir (textures can be in ae49fad43254367a, 4a4c32c49300b8a0, etc.)
        original_size = -1
        try:
            for sub in os.listdir(pcvr_dir):
                sub_path = os.path.join(pcvr_dir, sub)
                if not os.path.isdir(sub_path): continue
                for v in get_all_name_variations(payload_hash):
                    p = os.path.join(sub_path, v)
                    if os.path.exists(p):
                        original_size = os.path.getsize(p)
                        break
                if original_size != -1: break
        except Exception:
            pass
        
        if original_size == -1:
            # Fallback: find .dds in user's UI settings or mesh object settings
            user_cache = context.scene.evr_export_settings.texture_cache_dir
            obj_cache = None
            if hasattr(context, "active_object") and context.active_object and context.active_object.get("evr_texture_cache"):
                obj_cache = context.active_object["evr_texture_cache"]
                
            t_cache = user_cache.strip() if user_cache else None
            
            search_dirs = []
            if obj_cache and os.path.exists(obj_cache) and obj_cache not in search_dirs:
                search_dirs.append(obj_cache)
            if t_cache and os.path.exists(t_cache) and t_cache not in search_dirs:
                search_dirs.append(t_cache)
            for d in search_dirs:
                for v in get_all_name_variations(target_hash):
                    for ext in [".dds", ".png"]:
                        p = os.path.join(d, v + ext)
                        if os.path.exists(p):
                            sz = os.path.getsize(p)
                            if sz > original_size:
                                original_size = sz - 148  # Estimate raw size minus DDS header

        if original_size < 0:
            self.report({'WARNING'}, f"Could not find original texture {target_hash} to check size. Skipping padding.")
            original_size = len(raw_data)
            
        if len(raw_data) < original_size:
            raw_data = raw_data + b'\x00' * (original_size - len(raw_data))
        elif len(raw_data) > original_size:
            self.report({'WARNING'}, f"New texture is larger than original! Truncating to {original_size} bytes.")
            raw_data = raw_data[:original_size]
            
        # Write to the pcvr-extracted texture folder where the game reads it from
        # This ensures the in-game model actually sees the replaced texture
        export_dir = context.scene.evr_export_settings.export_dir
        pcvr_dir = _resolve_pcvr_dir(context, orig_path)
        
        # Strategy 1: Write directly to pcvr_extracted texture folder (for in-place replacement)
        written = False
        if pcvr_dir and os.path.exists(pcvr_dir):
            for sub in os.listdir(pcvr_dir):
                sub_path = os.path.join(pcvr_dir, sub)
                if not os.path.isdir(sub_path): continue
                for v in get_all_name_variations(payload_hash):
                    p = os.path.join(sub_path, v)
                    if os.path.exists(p):
                        # Found original texture location — overwrite it in-place
                        with open(p, 'wb') as f:
                            f.write(raw_data)
                        self.report({'INFO'}, f"Replaced texture written IN-PLACE to {p} ({len(raw_data)} bytes).")
                        written = True
                        break
                if written: break
        
        # Strategy 2: Also write to export_dir (for mod distribution)
        if export_dir:
            out_dir = os.path.join(export_dir, "ae49fad43254367a")
            os.makedirs(out_dir, exist_ok=True)
            out_path = os.path.join(out_dir, payload_hash)
            with open(out_path, 'wb') as f:
                f.write(raw_data)
            self.report({'INFO'}, f"Also saved to export dir: {out_path}" if written else f"Texture saved to export dir: {out_path} ({len(raw_data)} bytes).")
        elif not written:
            self.report({'ERROR'}, "Could not find original texture location to overwrite, and no Export Directory set.")
            return {'CANCELLED'}
            
        return {'FINISHED'}


def register():
    bpy.utils.register_class(EVR_OT_ImportMesh)
    bpy.utils.register_class(EVR_ExportSettings)
    bpy.utils.register_class(EVR_OT_TransferWeights)
    bpy.utils.register_class(EVR_PT_ExportPanel)
    bpy.utils.register_class(EVR_OT_ImportAndReplace)
    bpy.utils.register_class(EVR_OT_ExportMesh)
    bpy.utils.register_class(EVR_OT_ReplaceTextures)
    bpy.utils.register_class(EVR_OT_DumpModelData)
    bpy.types.Scene.evr_export_settings = bpy.props.PointerProperty(type=EVR_ExportSettings)
    bpy.types.Scene.evr_preview_image = bpy.props.PointerProperty(type=bpy.types.Image)
    bpy.utils.register_class(EVR_AssetSwapperSettings)
    bpy.utils.register_class(EVR_OT_SwapAssets)
    bpy.utils.register_class(EVR_PT_AssetSwapperPanel)
    bpy.types.Scene.evr_swapper_settings = bpy.props.PointerProperty(type=EVR_AssetSwapperSettings)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)
    bpy.types.TOPBAR_MT_file_export.append(menu_func_export)

def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    bpy.types.TOPBAR_MT_file_export.remove(menu_func_export)
    del bpy.types.Scene.evr_export_settings
    del bpy.types.Scene.evr_preview_image
    bpy.utils.unregister_class(EVR_OT_ImportAndReplace)
    bpy.utils.unregister_class(EVR_OT_ExportMesh)
    bpy.utils.unregister_class(EVR_OT_ReplaceTextures)
    bpy.utils.unregister_class(EVR_OT_DumpModelData)
    bpy.utils.unregister_class(EVR_OT_TransferWeights)
    bpy.utils.unregister_class(EVR_PT_ExportPanel)
    del bpy.types.Scene.evr_swapper_settings
    bpy.utils.unregister_class(EVR_PT_AssetSwapperPanel)
    bpy.utils.unregister_class(EVR_OT_SwapAssets)
    bpy.utils.unregister_class(EVR_AssetSwapperSettings)
    bpy.utils.unregister_class(EVR_ExportSettings)
    bpy.utils.unregister_class(EVR_OT_ImportMesh)

if __name__ == "__main__": register()
