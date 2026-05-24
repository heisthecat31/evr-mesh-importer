"""
EVR Raw Mesh Importer/Exporter — Blender add-on
Imports and exports raw GPU mesh binaries from Echo VR (Echo Arena).
https://github.com/Dualgame/evr-mesh-importer
"""

bl_info = {
    "name": "EVR Raw Mesh Importer",
    "author": "Dualgame",
    "version": (1, 2, 5),
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

_submodules = ["decode", "primary", "encode", "textures"]
for _sub in _submodules:
    _full_name = f"{__package__}.{_sub}" if __package__ else _sub
    if _full_name in sys.modules:
        importlib.reload(sys.modules[_full_name])

from bpy_extras.io_utils import ImportHelper, ExportHelper
from bpy.props import (StringProperty, BoolProperty, FloatProperty, CollectionProperty, EnumProperty)
from bpy.types import Operator

from .decode import extract_mesh
from .primary import _find_primary_data
from .encode import (
    encode_heuristic_s16,
    encode_heuristic_s20,
    encode_heuristic_dual28,
    encode_cgml,
    mesh_from_blender_object,
    EncodeSizeError,
    VertexLimitError,
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
    pcvr_extracted_dir: StringProperty(name="pcvr-extracted Folder", default="", subtype='DIR_PATH')
    texture_cache_dir: StringProperty(name="Texture Cache Folder", default="", subtype='DIR_PATH')
    flip_texture_v: BoolProperty(name="Flip Texture V", default=True)
    import_lod0_only: BoolProperty(name="Import LOD0 Only", default=True)
    texture_group_mode: EnumProperty(
        name="Texture Grouping",
        items=[
            ('sequential', "Sequential Texture List", "Use texture groups in raw mapping order, matching the verified Doug import"),
            ('binding', "Binding Table", "Use the mapping binding table to choose each group's texture slots"),
        ],
        default='sequential',
    )
    material_assign_mode: EnumProperty(
        name="Material Assignment",
        items=[
            ('uv_scanner', "Dynamic UV Scanner", "Dynamically scan texture PNGs and assign materials based on UV layout"),
            ('auto', "Auto", "Skin 0 for LOD/single imports, submesh order for multi-part imports"),
            ('first', "Force Skin 0", "Apply the first texture group to every imported mesh"),
            ('submesh', "By Submesh Order", "Apply Skin 0 to mesh .000, Skin 1 to mesh .001, and so on"),
            ('reverse', "Reverse Submesh Order", "Apply texture groups in reverse mesh order"),
        ],
        default='uv_scanner',
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
            
            if hasattr(obj.data, "attributes"):
                attr = obj.data.attributes.new(name="cgml_submesh", type='INT', domain='FACE')
                for p in obj.data.polygons:
                    attr.data[p.index].value = idx
                    
            obj["evr_material_index"] = idx
            if parent_empty: obj.parent = parent_empty
            created.append(obj)

        if self.auto_apply_textures and created:
            try:
                applied = apply_textures_to_objects(
                    base_name,
                    bpy.path.abspath(self.pcvr_extracted_dir) if self.pcvr_extracted_dir.strip() else "",
                    bpy.path.abspath(self.texture_cache_dir) if self.texture_cache_dir.strip() else "",
                    created,
                    texture_group_mode=self.texture_group_mode,
                    material_assign_mode=self.material_assign_mode,
                )
                if applied:
                    self.report({'INFO'}, f"Applied {applied} texture material(s) to {base_name}")
                else:
                    self.report({'WARNING'}, f"Imported {base_name}, but no matching texture mapping/PNGs were found")
            except Exception as exc:
                self.report({'WARNING'}, f"Imported {base_name}, but texture auto-apply failed: {exc}")

        return parent_empty if parent_empty else created[0] if created else None

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
    bl_label = "EVR Mesh Exporter"
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

        layout.prop(settings, "original_gpu_file")
        layout.prop(settings, "export_dir")
        layout.separator()
        layout.prop(settings, "encode_mode")
        if settings.encode_mode == 'primary_described':
            layout.prop(settings, "write_primary")
            layout.prop(settings, "stream0_stride")
        layout.prop(settings, "compute_normals")
        layout.prop(settings, "auto_decimate")

        layout.prop(settings, "scale")
        layout.separator()
        layout.label(text="Utilities:")
        row = layout.row()
        row.operator("evr.transfer_weights", text="Transfer Weights", icon='MOD_DATA_TRANSFER')
        layout.separator()
        layout.operator("export_mesh.evr_raw", text="Export Mesh (Replace)", icon='EXPORT')


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
                            submeshes.extend(mesh_from_blender_object(eo, apply_transforms=True, split_by_material=True, decimate_ratio=ratio))
                        else:
                            submeshes.append(mesh_from_blender_object(eo, apply_transforms=True, split_by_material=False, decimate_ratio=ratio))
                else:
                    res = mesh_from_blender_object(obj, apply_transforms=True, split_by_material=False, decimate_ratio=ratio)
                    verts, faces, uvs = res[0], res[1], res[2]
                    bone_data = res[3] if len(res) > 3 else None

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
                                orig_gpu, orig_primary, verts, faces, uvs=uvs, bone_data=bone_data, stream0_stride=s0_stride, compute_normals=cn
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

def register():
    bpy.utils.register_class(EVR_OT_ImportMesh)
    bpy.utils.register_class(EVR_ExportSettings)
    bpy.utils.register_class(EVR_OT_TransferWeights)
    bpy.utils.register_class(EVR_PT_ExportPanel)
    bpy.utils.register_class(EVR_OT_ExportMesh)
    bpy.types.Scene.evr_export_settings = bpy.props.PointerProperty(type=EVR_ExportSettings)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)
    bpy.types.TOPBAR_MT_file_export.append(menu_func_export)

def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    bpy.types.TOPBAR_MT_file_export.remove(menu_func_export)
    del bpy.types.Scene.evr_export_settings
    bpy.utils.unregister_class(EVR_OT_ExportMesh)
    bpy.utils.unregister_class(EVR_OT_TransferWeights)
    bpy.utils.unregister_class(EVR_PT_ExportPanel)
    bpy.utils.unregister_class(EVR_ExportSettings)
    bpy.utils.unregister_class(EVR_OT_ImportMesh)

if __name__ == "__main__": register()
