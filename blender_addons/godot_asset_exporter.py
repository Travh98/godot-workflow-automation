bl_info = {
    "name": "Godot Asset Exporter",
    "author": "Travh98",
    "version": (1, 0, 0),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > Godot  |  File > Export > Godot Assets (.glb)",
    "description": "Export the active collection's meshes as individual GLBs with a material map",
    "category": "Import-Export",
}

import bpy
import os
import json
from bpy.props import StringProperty
from bpy.types import Operator, Panel

# res:// path to the post-import script stamped into every exported mesh's .import stub.
MATERIAL_APPLIER_SCRIPT_PATH: str = "res://game-assets/godot-workflow-automation/material_applier_post_import.gd"


class GODOT_OT_ExportCollection(Operator):
    bl_idname = "godot.export_collection"
    bl_label = "Export Active Collection"
    bl_description = "Export all meshes in the active collection to the Godot assets folder"
    bl_options = {'REGISTER', 'UNDO'}

    _conflict_paths: list = []
    _conflict_collection_name: str = ""
    _conflict_target_dir: str = ""

    def invoke(self, context, event):
        self._conflict_paths = []

        export_root: str = bpy.path.abspath(context.scene.godot_export_path.strip())
        if export_root and os.path.isdir(export_root):
            collection = context.view_layer.active_layer_collection.collection
            if (
                collection != context.scene.collection
                and collection.name.strip().lower() not in ('reference', 'ref')
            ):
                target_dir: str = os.path.join(export_root, collection.name)
                self._conflict_paths = [
                    p for p in self._find_conflicting_folders(
                        export_root, collection.name, target_dir,
                    )
                    if 'models' in p.lower()
                ]
                self._conflict_collection_name = collection.name
                self._conflict_target_dir = target_dir

        if self._conflict_paths:
            return context.window_manager.invoke_props_dialog(self, width=500)
        return self.execute(context)

    def draw(self, context) -> None:
        layout = self.layout
        layout.label(
            text=f"'{self._conflict_collection_name}' already exists elsewhere in the project:",
            icon='ERROR',
        )
        for p in self._conflict_paths:
            layout.label(text=f"  {p}")
        layout.separator()
        layout.label(text=f"OK will export to: {self._conflict_target_dir}")

    def _find_project_root(self, start_dir: str):
        current: str = os.path.abspath(start_dir)
        while True:
            if os.path.isfile(os.path.join(current, "project.godot")):
                return current
            parent: str = os.path.dirname(current)
            if parent == current:
                return None
            current = parent

    def _find_conflicting_folders(self, export_root: str, collection_name: str, target_dir: str) -> list:
        project_root = self._find_project_root(export_root) or export_root
        target_dir_norm: str = os.path.normcase(os.path.normpath(target_dir))
        name_lower: str = collection_name.strip().lower()
        skip_dirs: set = {'.git', '.godot', '.import'}

        conflicts: list = []
        for dirpath, dirnames, _filenames in os.walk(project_root):
            dirnames[:] = [d for d in dirnames if d not in skip_dirs and not d.startswith('.')]
            for d in dirnames:
                if d.lower() == name_lower:
                    full: str = os.path.join(dirpath, d)
                    if os.path.normcase(os.path.normpath(full)) != target_dir_norm:
                        conflicts.append(full)
        return conflicts

    def execute(self, context):
        export_root: str = bpy.path.abspath(context.scene.godot_export_path.strip())

        if not export_root:
            self.report({'ERROR'}, "Export path is not set. Set it in the Godot sidebar panel.")
            return {'CANCELLED'}

        if not os.path.isdir(export_root):
            self.report({'ERROR'}, f"Export path does not exist: {export_root}")
            return {'CANCELLED'}

        collection = context.view_layer.active_layer_collection.collection

        if collection == context.scene.collection:
            self.report({'ERROR'}, "Cannot export the Scene Collection directly — select a sub-collection.")
            return {'CANCELLED'}

        if collection.name.strip().lower() in ('reference', 'ref'):
            self.report({'ERROR'}, f"Cannot export collection '{collection.name}' — reference collections are excluded.")
            return {'CANCELLED'}

        material_map: dict = {}
        mesh_count_ref: list = [0]

        visible_only: bool = context.scene.godot_export_visible_only

        self._export_collection(
            context, collection, export_root, [collection.name], material_map, mesh_count_ref,
            visible_only,
        )

        map_dir: str = os.path.join(export_root, collection.name)
        os.makedirs(map_dir, exist_ok=True)
        map_path: str = os.path.join(map_dir, "material_map.json")
        with open(map_path, "w", encoding="utf-8") as f:
            json.dump(material_map, f, indent=2, ensure_ascii=False)

        count: int = mesh_count_ref[0]
        self.report({'INFO'}, f"Exported {count} mesh(es) + material_map.json to {map_dir}")
        return {'FINISHED'}

    def _export_collection(
        self,
        context,
        collection,
        export_root: str,
        path_parts: list,
        material_map: dict,
        mesh_count_ref: list,
        visible_only: bool = False,
    ) -> None:
        dir_path: str = os.path.join(export_root, *path_parts)
        os.makedirs(dir_path, exist_ok=True)

        # Build relative key path from the root collection (path_parts[0] is root, excluded from keys)
        rel_prefix: str = "/".join(path_parts[1:])

        for obj in collection.objects:
            if obj.type not in ('MESH', 'ARMATURE'):
                continue
            if visible_only and not obj.visible_get():
                continue
            # Skip meshes that export together with a parent mesh or rig
            if obj.type == 'MESH' and obj.parent is not None and obj.parent.type in ('MESH', 'ARMATURE'):
                continue

            file_path: str = os.path.join(dir_path, obj.name + ".glb")
            rel_key: str = f"{rel_prefix}/{obj.name}" if rel_prefix else obj.name

            family: list = self._collect_mesh_family(obj)

            # Armatures have no material slots of their own, so their family
            # starts at index 1. Map every mesh in the family to its own slot
            # list by name — a mesh with child meshes bundled into the same
            # .glb needs its own entry too, not just the root's slots, since
            # material_applier_post_import.gd looks materials up by mesh name.
            family_meshes: list = family[1:] if obj.type == 'ARMATURE' else family
            material_map[rel_key] = {
                mesh.name: [slot.material.name if slot.material else "" for slot in mesh.material_slots]
                for mesh in family_meshes
            }

            self._export_mesh_family(context, family, file_path)
            self._write_import_stub(file_path)
            mesh_count_ref[0] += 1

        for child in collection.children:
            self._export_collection(
                context,
                child,
                export_root,
                path_parts + [child.name],
                material_map,
                mesh_count_ref,
                visible_only,
            )

    def _write_import_stub(self, glb_path: str) -> None:
        import_path: str = glb_path + ".import"
        script_entry: str = f'import_script/path="{MATERIAL_APPLIER_SCRIPT_PATH}"'

        print(f"[GodotExporter] Writing import stub: {import_path}")

        if os.path.exists(import_path):
            with open(import_path, "r", encoding="utf-8") as f:
                content: str = f.read()

            content = content.replace("\r\n", "\n")

            # Replace line-by-line — handles empty value, wrong value, missing entry
            lines: list = content.splitlines()
            print(f"[GodotExporter] .import lines: {len(lines)}")

            new_lines: list = []
            found_entry: bool = False
            for ln in lines:
                if ln.startswith("script_class="):
                    continue  # strip old incorrect key
                if ln.startswith("import_script/path="):
                    print(f"[GodotExporter] Replacing: {ln!r}  ->  {script_entry!r}")
                    new_lines.append(script_entry)
                    found_entry = True
                else:
                    new_lines.append(ln)

            if not found_entry:
                print(f"[GodotExporter] import_script/path not found — inserting after [params]")
                result: list = []
                for ln in new_lines:
                    result.append(ln)
                    if ln.strip() == "[params]":
                        result.append(script_entry)
                        found_entry = True
                new_lines = result

            if not found_entry:
                print(f"[GodotExporter] No [params] section found — appending")
                new_lines += ["", "[params]", script_entry]

            content = "\n".join(new_lines) + "\n"
            print(f"[GodotExporter] Writing updated .import")
            with open(import_path, "w", encoding="utf-8") as f:
                f.write(content)
        else:
            print(f"[GodotExporter] No .import file yet — writing stub")
            with open(import_path, "w", encoding="utf-8") as f:
                f.write("[remap]\n\n[params]\n" + script_entry + "\n")

    def _collect_mesh_family(self, obj) -> list:
        """Return obj and all its mesh descendants (children, grandchildren, etc.).

        Works for a MESH or an ARMATURE root — meshes skinned to a rig are
        parented to the armature, so this also collects an armature's
        deform meshes.
        """
        result: list = [obj]
        for child in obj.children:
            if child.type == 'MESH':
                result.extend(self._collect_mesh_family(child))
        return result

    def _export_mesh_family(self, context, objects: list, file_path: str) -> None:
        prev_active = context.view_layer.objects.active
        prev_selected: list = list(context.selected_objects)

        # Hidden objects can't be selected — unhide them so they actually
        # make it into the export, then restore hidden state afterward.
        prev_hidden: dict = {o: o.hide_get() for o in objects if o.hide_get()}
        for o in prev_hidden:
            o.hide_set(False)

        for o in context.selected_objects:
            o.select_set(False)
        for o in objects:
            o.select_set(True)
        context.view_layer.objects.active = objects[0]

        bpy.ops.export_scene.gltf(
            filepath=file_path,
            use_selection=True,
            export_format='GLB',
            # export_apply strips modifiers but preserves the armature
            # deform, so skinning and actions still export correctly.
            export_apply=True,
            export_yup=True,
            export_materials='PLACEHOLDER',
            export_skins=True,
            export_animations=True,
            # Default 'ACTIONS' mode pulls in any action in the .blend file
            # whose bone paths are compatible with this armature, so rigs
            # sharing bone names cross-contaminate each other's animations.
            # NLA_TRACKS scopes export to only the tracks pushed down onto
            # this specific armature.
            export_animation_mode='NLA_TRACKS',
        )

        for o in objects:
            o.select_set(False)
        for o in prev_selected:
            o.select_set(True)
        context.view_layer.objects.active = prev_active

        for o, was_hidden in prev_hidden.items():
            o.hide_set(was_hidden)


class GODOT_OT_ReloadAddon(Operator):
    bl_idname = "godot.reload_addon"
    bl_label = "Reload Addon"
    bl_description = "Reload the Godot Asset Exporter addon from disk"

    def execute(self, context) -> set:
        import sys
        import importlib
        import traceback
        module_name: str = __name__
        if module_name not in sys.modules:
            self.report({'ERROR'}, f"Module {module_name!r} not in sys.modules")
            return {'CANCELLED'}
        mod = sys.modules[module_name]

        try:
            mod.unregister()
        except Exception:
            print("[GodotExporter] unregister() failed, continuing anyway:")
            traceback.print_exc()

        try:
            importlib.reload(mod)
        except Exception as e:
            traceback.print_exc()
            self.report({'ERROR'}, f"Reload failed, addon left unregistered: {e}")
            return {'CANCELLED'}

        try:
            mod.register()
        except Exception as e:
            traceback.print_exc()
            self.report({'ERROR'}, f"register() failed after reload: {e}")
            return {'CANCELLED'}

        self.report({'INFO'}, "Godot Asset Exporter reloaded.")
        return {'FINISHED'}


def _draw_help_popup(self, context) -> None:
    layout = self.layout
    layout.label(text="Tips:")
    layout.label(text="Materials should be named the same as Godot materials in res://assets/materials or res://game-assets/materials")
    layout.label(text="")
    layout.label(text="1. Set 'Assets Folder' to your Godot project's asset root.")
    layout.label(text="2. In the Outliner, select the collection you want to export.")
    layout.label(text="3. Click 'Export Active Collection'.")
    layout.label(text="4. Each mesh (with its child meshes) exports as one .glb,")
    layout.label(text="   plus a material_map.json and a Godot .import stub.")
    layout.label(text="   Every mesh in the family — root and children alike —")
    layout.label(text="   is keyed by name in material_map.json.")
    layout.label(text="5. After editing this addon's script, click 'Reload Addon'.")
    layout.label(text="")
    layout.label(text="If a folder matching the active collection's name already exists")
    layout.label(text="elsewhere in the project, a confirmation dialog warns you before export.")


class GODOT_OT_ShowHelp(Operator):
    bl_idname = "godot.show_export_help"
    bl_label = "Godot Exporter Help"
    bl_description = "Show usage instructions for the Godot Asset Exporter"

    def execute(self, context) -> set:
        context.window_manager.popup_menu(_draw_help_popup, title="Godot Asset Exporter — Usage", icon='INFO')
        return {'FINISHED'}


class GODOT_PT_ExportPanel(Panel):
    bl_label = "Godot Asset Exporter"
    bl_idname = "GODOT_PT_export_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Godot"

    def draw(self, context) -> None:
        layout = self.layout
        scene = context.scene

        layout.prop(scene, "godot_export_path", text="Assets Folder")
        layout.prop(scene, "godot_export_visible_only", text="Visible Only")

        layout.separator(type='LINE')

        active_name: str = context.view_layer.active_layer_collection.name
        layout.label(text=f"Active: {active_name}", icon='OUTLINER_COLLECTION')
        layout.operator(
            GODOT_OT_ExportCollection.bl_idname,
            text="Export Active Collection",
            icon='EXPORT',
        )
        layout.separator()
        layout.operator(GODOT_OT_ShowHelp.bl_idname, text="Help", icon='QUESTION')
        layout.operator(GODOT_OT_ReloadAddon.bl_idname, text="Reload Addon", icon='FILE_REFRESH')

        layout.separator()
        box = layout.box()
        box.label(text="Symlinked from repo:", icon='INFO')
        box.label(text="game-assets/godot-workflow-automation/godot_asset_exporter.py")


def menu_func_export(self, context) -> None:
    self.layout.operator(GODOT_OT_ExportCollection.bl_idname, text="Godot Assets (.glb)")


def register() -> None:
    bpy.types.Scene.godot_export_visible_only = bpy.props.BoolProperty(
        name="Visible Only",
        description="Only export mesh objects that are currently visible in the viewport",
        default=False,
    )
    bpy.types.Scene.godot_export_path = StringProperty(
        name="Godot Export Path",
        description="Root path of the Godot project assets folder",
        default="",
        subtype='DIR_PATH',
    )
    bpy.utils.register_class(GODOT_OT_ExportCollection)
    bpy.utils.register_class(GODOT_OT_ReloadAddon)
    bpy.utils.register_class(GODOT_OT_ShowHelp)
    bpy.utils.register_class(GODOT_PT_ExportPanel)
    bpy.types.TOPBAR_MT_file_export.append(menu_func_export)


def unregister() -> None:
    bpy.types.TOPBAR_MT_file_export.remove(menu_func_export)
    bpy.utils.unregister_class(GODOT_PT_ExportPanel)
    bpy.utils.unregister_class(GODOT_OT_ShowHelp)
    bpy.utils.unregister_class(GODOT_OT_ReloadAddon)
    bpy.utils.unregister_class(GODOT_OT_ExportCollection)
    del bpy.types.Scene.godot_export_path
    del bpy.types.Scene.godot_export_visible_only


if __name__ == "__main__":
    register()
