"""
EVR Raw Mesh Importer — Blender add-on
Imports raw GPU mesh binaries from Echo VR (Echo Arena).
https://github.com/Dualgame/evr-mesh-importer
"""

bl_info = {
    "name": "EVR Raw Mesh Importer",
    "author": "Dualgame",
    "version": (1, 0, 0),
    "blender": (3, 0, 0),
    "location": "File > Import > EVR Raw Mesh",
    "description": "Import raw GPU mesh binaries from Echo VR (Echo Arena)",
    "doc_url": "https://github.com/Dualgame/evr-mesh-importer",
    "tracker_url": "https://github.com/Dualgame/evr-mesh-importer/issues",
    "category": "Import-Export",
}

import bpy
import os
from bpy_extras.io_utils import ImportHelper
from bpy.props import StringProperty, BoolProperty, FloatProperty, CollectionProperty
from bpy.types import Operator

from .decode import extract_mesh
from .primary import _find_primary_data


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


class EVR_OT_ImportMesh(Operator, ImportHelper):
    """Import a raw EVR GPU mesh binary (CIMR, CGML, or heuristic)"""
    bl_idname = "import_mesh.evr_raw"
    bl_label = "EVR Raw Mesh"
    bl_options = {'REGISTER', 'UNDO'}

    filter_glob: StringProperty(
        default="*",
        options={'HIDDEN'},
        maxlen=255,
    )

    files: CollectionProperty(
        name="File Path",
        type=bpy.types.OperatorFileListElement,
    )

    directory: StringProperty(subtype='DIR_PATH')

    primary_file: StringProperty(
        name="Primary File (optional)",
        description=(
            "Path to the matching Primary binary for this GPU file. "
            "Leave blank to auto-discover or use GPU-only heuristics."
        ),
        default="",
        subtype='FILE_PATH',
    )

    auto_find_primary: BoolProperty(
        name="Auto-find Primary",
        description=(
            "Try to locate a matching Primary binary in the sibling Primary/ "
            "directory automatically (mirrors project directory layout)."
        ),
        default=True,
    )

    use_smooth: BoolProperty(
        name="Smooth Shading",
        description="Apply smooth shading to imported meshes",
        default=False,
    )

    scale: FloatProperty(
        name="Scale",
        description="Uniform scale factor applied to all vertices",
        default=1.0,
        min=0.0001,
        max=10000.0,
        soft_min=0.01,
        soft_max=100.0,
    )

    @classmethod
    def poll(cls, context):
        return context.area is not None

    def execute(self, context):
        if self.files:
            paths = [os.path.join(self.directory, f.name) for f in self.files]
        else:
            paths = [self.filepath]

        primary_data = None
        if self.primary_file.strip():
            ppath = bpy.path.abspath(self.primary_file)
            if os.path.isfile(ppath):
                with open(ppath, "rb") as fh:
                    primary_data = fh.read()
            else:
                self.report({'WARNING'}, f"Primary file not found: {ppath}")

        all_roots = []
        for gpu_path in paths:
            root = self._import_single(context, gpu_path, primary_data)
            if root:
                all_roots.append(root)

        for obj in context.selected_objects:
            obj.select_set(False)
        for root in all_roots:
            root.select_set(True)
        if all_roots:
            context.view_layer.objects.active = all_roots[-1]

        if not all_roots:
            return {'CANCELLED'}
        return {'FINISHED'}

    def _import_single(self, context, gpu_path, primary_data=None):
        if not os.path.isfile(gpu_path):
            self.report({'ERROR'}, f"File not found: {gpu_path}")
            return None

        try:
            submeshes, path_label = extract_mesh(
                gpu_path,
                primary_data=primary_data,
                auto_find_primary=(primary_data is None and self.auto_find_primary),
            )
        except Exception as exc:
            self.report({'ERROR'}, f"Decode failed: {exc}")
            return None

        if not submeshes:
            self.report({'WARNING'}, f"{os.path.basename(gpu_path)}: no geometry decoded.")
            return None

        base_name = os.path.splitext(os.path.basename(gpu_path))[0]
        valid = [(v, f) for v, f in submeshes if v and f]

        parent_empty = None
        if len(valid) > 1:
            parent_empty = bpy.data.objects.new(base_name, None)
            parent_empty.empty_display_type = 'PLAIN_AXES'
            parent_empty.empty_display_size = 0.01
            bpy.context.collection.objects.link(parent_empty)

        created = []
        for idx, (verts, faces) in enumerate(submeshes):
            if not verts or not faces:
                continue

            if self.scale != 1.0:
                s = self.scale
                verts = [(x * s, y * s, z * s) for x, y, z in verts]

            name = base_name if parent_empty is None else f"{base_name}.{idx:03d}"
            obj = _build_blender_mesh(verts, faces, name)
            if not self.use_smooth:
                _apply_flat_shading(obj)
            if parent_empty is not None:
                obj.parent = parent_empty
            created.append(obj)

        if not created:
            if parent_empty is not None:
                bpy.data.objects.remove(parent_empty, do_unlink=True)
            self.report({'WARNING'}, f"{base_name}: all submeshes were empty.")
            return None

        total_tris = sum(len(obj.data.polygons) for obj in created)
        self.report(
            {'INFO'},
            f"{base_name}: {len(created)} mesh(es) {total_tris}f [{path_label}]",
        )

        return parent_empty if parent_empty is not None else created[0]

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "auto_find_primary")
        layout.prop(self, "primary_file")
        layout.separator()
        layout.prop(self, "use_smooth")
        layout.prop(self, "scale")


def menu_func_import(self, context):
    self.layout.operator(EVR_OT_ImportMesh.bl_idname, text="EVR Raw Mesh")


def register():
    bpy.utils.register_class(EVR_OT_ImportMesh)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)


def unregister():
    bpy.utils.unregister_class(EVR_OT_ImportMesh)
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)


if __name__ == "__main__":
    register()
