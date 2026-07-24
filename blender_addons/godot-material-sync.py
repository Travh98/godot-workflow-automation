bl_info = {
    "name": "Godot Material Sync",
    "author": "Travh98",
    "version": (1, 0, 0),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > Godot",
    "description": "Sync Godot .tres materials from res://assets/materials and res://game-assets/materials into Blender",
    "category": "Material",
}

import bpy
import datetime
import os
import re
from bpy.props import StringProperty
from bpy.types import Operator, Panel

MATERIAL_SEARCH_DIRS = ("assets/materials", "game-assets/materials")

# Godot's resource saver omits any StandardMaterial3D property left at its class
# default, so a missing key means "use this value" — not "leave Blender alone".
STANDARD_MATERIAL_DEFAULTS = {
    'albedo_color': (1.0, 1.0, 1.0, 1.0),
    'metallic': 0.0,
    'metallic_specular': 0.5,
    'roughness': 1.0,
    'normal_scale': 1.0,
    'emission': (0.0, 0.0, 0.0, 1.0),
    'emission_energy_multiplier': 1.0,
    'transparency': 0.0,
    'cull_mode': 0.0,
    'alpha_scissor_threshold': 0.5,
    'texture_filter': 3.0,  # TEXTURE_FILTER_LINEAR_WITH_MIPMAPS
}

# Godot's texture_filter enum, collapsed to the two interpolation modes Blender's
# Image Texture node actually has (mipmapping/anisotropy aren't representable there).
GODOT_FILTER_TO_INTERPOLATION = {
    0: 'Closest',  # NEAREST
    1: 'Linear',   # LINEAR
    2: 'Closest',  # NEAREST_WITH_MIPMAPS
    3: 'Linear',   # LINEAR_WITH_MIPMAPS
    4: 'Closest',  # NEAREST_WITH_MIPMAPS_ANISOTROPIC
    5: 'Linear',   # LINEAR_WITH_MIPMAPS_ANISOTROPIC
}

_KV_RE = re.compile(r'^([A-Za-z0-9_/]+)\s*=\s*(.*)$')
_HEADER_ATTR_RE = re.compile(r'(\w+)="([^"]*)"')
_EXT_RESOURCE_RE = re.compile(r'^ExtResource\(\s*"([^"]+)"\s*\)$')
_SUB_RESOURCE_RE = re.compile(r'^SubResource\(\s*"([^"]+)"\s*\)$')
_COLOR_RE = re.compile(r'^Color\(([^)]*)\)$')
_VEC_RE = re.compile(r'^Vector[234]\(([^)]*)\)$')


def _parse_value(raw: str):
    raw = raw.strip()
    m = _EXT_RESOURCE_RE.match(raw)
    if m:
        return ('ext_resource', m.group(1))
    m = _SUB_RESOURCE_RE.match(raw)
    if m:
        return ('sub_resource', m.group(1))
    m = _COLOR_RE.match(raw)
    if m:
        parts = [float(p.strip()) for p in m.group(1).split(',') if p.strip()]
        if len(parts) == 3:
            parts.append(1.0)
        return tuple(parts)
    m = _VEC_RE.match(raw)
    if m:
        return tuple(float(p.strip()) for p in m.group(1).split(',') if p.strip())
    if raw == 'true':
        return True
    if raw == 'false':
        return False
    if raw.startswith('"') and raw.endswith('"'):
        return raw[1:-1]
    try:
        return float(raw)
    except ValueError:
        return raw


def _parse_tres(filepath: str) -> dict:
    """Parse a Godot .tres text resource file into
    {'type': str, 'resource': {key: value}, 'ext_resources': {id: {'type':.., 'path':..}}}."""
    result = {'type': '', 'resource': {}, 'ext_resources': {}}
    section = None
    with open(filepath, 'r', encoding='utf-8') as f:
        lines = f.read().splitlines()

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith('[') and stripped.endswith(']'):
            header = stripped[1:-1]
            name = header.split(' ', 1)[0]
            attrs = dict(_HEADER_ATTR_RE.findall(header))
            if name == 'gd_resource':
                result['type'] = attrs.get('type', '')
                section = None
            elif name == 'ext_resource':
                rid = attrs.get('id')
                if rid:
                    result['ext_resources'][rid] = {
                        'type': attrs.get('type', ''),
                        'path': attrs.get('path', ''),
                    }
                section = None
            elif name == 'resource':
                section = 'resource'
            else:
                section = None
            continue

        if section == 'resource':
            m = _KV_RE.match(stripped)
            if m:
                key, raw_val = m.group(1), m.group(2)
                result['resource'][key] = _parse_value(raw_val)

    return result


def _resolve_res_path(res_path: str, project_root: str) -> str:
    if res_path.startswith('res://'):
        res_path = res_path[len('res://'):]
    return os.path.normpath(os.path.join(project_root, res_path))


def _load_image(res_path: str, project_root: str):
    abs_path = _resolve_res_path(res_path, project_root)
    if not os.path.isfile(abs_path):
        print(f"[MaterialSync] Texture not found on disk: {abs_path}")
        return None
    return bpy.data.images.load(abs_path, check_existing=True)


def _get_principled(mat):
    mat.use_nodes = True
    tree = mat.node_tree
    for node in tree.nodes:
        if node.type == 'BSDF_PRINCIPLED':
            return tree, node
    # No principled node (e.g. user replaced it) — build a minimal default graph.
    tree.nodes.clear()
    principled = tree.nodes.new('ShaderNodeBsdfPrincipled')
    output = tree.nodes.new('ShaderNodeOutputMaterial')
    output.location = (principled.location.x + 300, principled.location.y)
    tree.links.new(principled.outputs['BSDF'], output.inputs['Surface'])
    return tree, principled


def _make_tex_node(tree, image, location, non_color: bool = False, label: str = ""):
    node = tree.nodes.new('ShaderNodeTexImage')
    node.image = image
    node.location = location
    node.label = label
    if non_color and image is not None:
        image.colorspace_settings.name = 'Non-Color'
    return node


def _ext_path(value, ext_resources: dict):
    if isinstance(value, tuple) and len(value) == 2 and value[0] == 'ext_resource':
        return ext_resources.get(value[1], {}).get('path', '')
    return None


def _prune_dead_upstream(tree, node) -> None:
    """Remove node if none of its outputs are used anywhere, then recurse into
    whatever fed its inputs — cleans up whole texture chains a previous sync
    left behind (e.g. Image Texture -> Normal Map), but stops as soon as it
    hits a node still feeding some other socket (e.g. a shared ORM texture)."""
    if node is None:
        return
    if any(output.is_linked for output in node.outputs):
        return
    upstream = [link.from_node for inp in node.inputs for link in inp.links]
    tree.nodes.remove(node)
    for up in upstream:
        _prune_dead_upstream(tree, up)


def _clear_socket_link(tree, principled, input_name: str, mat_name: str) -> None:
    """Disconnect whatever currently feeds a socket and prune it if now unused —
    Godot is the source of truth, so a synced socket should never be left
    pointing at a stale node from a previous sync (or a manual edit)."""
    socket = principled.inputs[input_name]
    if not socket.is_linked:
        return
    for link in list(socket.links):
        from_node = link.from_node
        print(f"[MaterialSync] {mat_name}: {input_name} was linked to '{from_node.name}' — disconnecting so Godot's value applies")
        tree.links.remove(link)
        _prune_dead_upstream(tree, from_node)


def _set_input(tree, principled, input_name: str, value, mat_name: str) -> None:
    """Set a Principled BSDF socket's default_value, clearing any existing
    link first (a linked socket ignores its default_value)."""
    _clear_socket_link(tree, principled, input_name, mat_name)
    socket = principled.inputs[input_name]
    old_value = socket.default_value
    print(f"[MaterialSync] {mat_name}: {input_name} {old_value!r} -> {value!r}")
    socket.default_value = value


def _link_input(tree, principled, input_name: str, from_socket, mat_name: str) -> None:
    """Connect from_socket to a Principled BSDF input, clearing any existing
    link first so old texture chains don't linger unused in the node tree."""
    _clear_socket_link(tree, principled, input_name, mat_name)
    tree.links.new(from_socket, principled.inputs[input_name])
    print(f"[MaterialSync] {mat_name}: linked texture -> {input_name}")


def _apply_material(mat, parsed: dict, project_root: str) -> list:
    """Apply parsed Godot resource props onto a Blender material. Returns list of warning strings."""
    warnings: list = []
    props = parsed['resource']
    ext_resources = parsed['ext_resources']
    res_type = parsed['type']

    print(f"[MaterialSync] {mat.name}: parsed type={res_type!r} props={props!r}")

    tree, principled = _get_principled(mat)
    x_cursor = principled.location.x - 400
    y_cursor = principled.location.y

    def next_loc():
        nonlocal y_cursor
        loc = (x_cursor, y_cursor)
        y_cursor -= 300
        return loc

    # Base color / albedo — Godot renders texture * albedo_color (a tint), so when both
    # are present they're combined with a Multiply node rather than fighting over Base Color.
    # texture_filter is material-wide in Godot but Blender's Image Texture node only has a
    # per-node interpolation setting — applied to the albedo node as the representative one.
    albedo_color = props.get('albedo_color', STANDARD_MATERIAL_DEFAULTS['albedo_color'])
    texture_filter = props.get('texture_filter', STANDARD_MATERIAL_DEFAULTS['texture_filter'])
    albedo_tex_path = _ext_path(props.get('albedo_texture'), ext_resources)
    if albedo_tex_path:
        image = _load_image(albedo_tex_path, project_root)
        if image:
            tex_node = _make_tex_node(tree, image, next_loc(), non_color=False, label="Albedo")
            tex_node.interpolation = GODOT_FILTER_TO_INTERPOLATION.get(int(texture_filter), 'Linear')
            mix = tree.nodes.new('ShaderNodeMixRGB')
            mix.blend_type = 'MULTIPLY'
            mix.location = (tex_node.location.x + 300, tex_node.location.y)
            mix.label = "Albedo Tint"
            mix.inputs['Fac'].default_value = 1.0
            mix.inputs['Color2'].default_value = albedo_color
            tree.links.new(tex_node.outputs['Color'], mix.inputs['Color1'])
            _link_input(tree, principled, 'Base Color', mix.outputs['Color'], mat.name)
            transparency = props.get('transparency', STANDARD_MATERIAL_DEFAULTS['transparency'])
            if transparency and transparency != 0.0:
                _link_input(tree, principled, 'Alpha', tex_node.outputs['Alpha'], mat.name)
            else:
                _clear_socket_link(tree, principled, 'Alpha', mat.name)
                principled.inputs['Alpha'].default_value = 1.0
        else:
            warnings.append(f"albedo_texture not found: {albedo_tex_path}")
    else:
        _set_input(tree, principled, 'Base Color', albedo_color, mat.name)
        _clear_socket_link(tree, principled, 'Alpha', mat.name)
        principled.inputs['Alpha'].default_value = 1.0

    # Metallic
    metallic = props.get('metallic', STANDARD_MATERIAL_DEFAULTS['metallic'])
    if isinstance(metallic, float):
        _set_input(tree, principled, 'Metallic', metallic, mat.name)
    metallic_tex_path = _ext_path(props.get('metallic_texture'), ext_resources)
    if metallic_tex_path:
        image = _load_image(metallic_tex_path, project_root)
        if image:
            node = _make_tex_node(tree, image, next_loc(), non_color=True, label="Metallic")
            _link_input(tree, principled, 'Metallic', node.outputs['Color'], mat.name)
        else:
            warnings.append(f"metallic_texture not found: {metallic_tex_path}")

    # Metallic Specular — Godot's dielectric specular reflectance, maps onto the
    # Principled BSDF's Specular IOR Level (renamed from "Specular" in Blender 4.0).
    metallic_specular = props.get('metallic_specular', STANDARD_MATERIAL_DEFAULTS['metallic_specular'])
    if isinstance(metallic_specular, float):
        if 'Specular IOR Level' in principled.inputs:
            _set_input(tree, principled, 'Specular IOR Level', metallic_specular, mat.name)
        else:
            warnings.append("Principled BSDF has no 'Specular IOR Level' input — metallic_specular skipped")

    # Roughness
    roughness = props.get('roughness', STANDARD_MATERIAL_DEFAULTS['roughness'])
    if isinstance(roughness, float):
        _set_input(tree, principled, 'Roughness', roughness, mat.name)
    roughness_tex_path = _ext_path(props.get('roughness_texture'), ext_resources)
    if roughness_tex_path:
        image = _load_image(roughness_tex_path, project_root)
        if image:
            node = _make_tex_node(tree, image, next_loc(), non_color=True, label="Roughness")
            _link_input(tree, principled, 'Roughness', node.outputs['Color'], mat.name)
        else:
            warnings.append(f"roughness_texture not found: {roughness_tex_path}")

    # Combined ORM texture (ORMMaterial3D)
    orm_tex_path = _ext_path(props.get('orm_texture'), ext_resources)
    if res_type == 'ORMMaterial3D' and orm_tex_path:
        image = _load_image(orm_tex_path, project_root)
        if image:
            node = _make_tex_node(tree, image, next_loc(), non_color=True, label="ORM (R=AO G=Rough B=Metal)")
            sep = tree.nodes.new('ShaderNodeSeparateColor')
            sep.location = (node.location.x + 300, node.location.y)
            tree.links.new(node.outputs['Color'], sep.inputs['Color'])
            _link_input(tree, principled, 'Roughness', sep.outputs['Green'], mat.name)
            _link_input(tree, principled, 'Metallic', sep.outputs['Blue'], mat.name)
        else:
            warnings.append(f"orm_texture not found: {orm_tex_path}")

    # Normal map — normal_enabled defaults to false, which means "no normal map",
    # so clear any link left behind by a previous sync when it's off/absent.
    if props.get('normal_enabled'):
        normal_tex_path = _ext_path(props.get('normal_texture'), ext_resources)
        if normal_tex_path:
            image = _load_image(normal_tex_path, project_root)
            if image:
                node = _make_tex_node(tree, image, next_loc(), non_color=True, label="Normal")
                normal_map = tree.nodes.new('ShaderNodeNormalMap')
                normal_map.location = (node.location.x + 300, node.location.y)
                scale = props.get('normal_scale', STANDARD_MATERIAL_DEFAULTS['normal_scale'])
                if isinstance(scale, float):
                    normal_map.inputs['Strength'].default_value = scale
                tree.links.new(node.outputs['Color'], normal_map.inputs['Color'])
                _link_input(tree, principled, 'Normal', normal_map.outputs['Normal'], mat.name)
            else:
                warnings.append(f"normal_texture not found: {normal_tex_path}")
    else:
        _clear_socket_link(tree, principled, 'Normal', mat.name)

    # Emission — emission_enabled defaults to false ("no emission"), so reset
    # Emission Color/Strength to Godot's defaults (and clear any texture link) when off/absent.
    if props.get('emission_enabled'):
        emission_color = props.get('emission', STANDARD_MATERIAL_DEFAULTS['emission'])
        if isinstance(emission_color, tuple):
            _set_input(tree, principled, 'Emission Color', emission_color, mat.name)
        strength = props.get('emission_energy_multiplier', STANDARD_MATERIAL_DEFAULTS['emission_energy_multiplier'])
        if isinstance(strength, float):
            _set_input(tree, principled, 'Emission Strength', strength, mat.name)
        emission_tex_path = _ext_path(props.get('emission_texture'), ext_resources)
        if emission_tex_path:
            image = _load_image(emission_tex_path, project_root)
            if image:
                node = _make_tex_node(tree, image, next_loc(), non_color=False, label="Emission")
                _link_input(tree, principled, 'Emission Color', node.outputs['Color'], mat.name)
            else:
                warnings.append(f"emission_texture not found: {emission_tex_path}")
    else:
        _set_input(tree, principled, 'Emission Color', STANDARD_MATERIAL_DEFAULTS['emission'], mat.name)
        _set_input(tree, principled, 'Emission Strength', STANDARD_MATERIAL_DEFAULTS['emission_energy_multiplier'], mat.name)

    # Transparency / blend mode
    transparency = props.get('transparency', STANDARD_MATERIAL_DEFAULTS['transparency'])
    if isinstance(transparency, float):
        mode = int(transparency)
        blend_map = {0: 'OPAQUE', 1: 'BLEND', 2: 'CLIP', 3: 'HASHED', 4: 'BLEND'}
        try:
            mat.blend_method = blend_map.get(mode, 'OPAQUE')
        except Exception:
            pass  # older/newer Blender versions expose blend modes differently
        alpha_scissor = props.get('alpha_scissor_threshold', STANDARD_MATERIAL_DEFAULTS['alpha_scissor_threshold'])
        if isinstance(alpha_scissor, float):
            try:
                mat.alpha_threshold = alpha_scissor
            except Exception:
                pass

    # Cull mode: Godot 0=Back, 1=Front, 2=Disabled
    cull_mode = props.get('cull_mode', STANDARD_MATERIAL_DEFAULTS['cull_mode'])
    if isinstance(cull_mode, float):
        mat.use_backface_culling = int(cull_mode) != 2

    return warnings


class GODOT_OT_SyncMaterials(Operator):
    bl_idname = "godot.sync_materials"
    bl_label = "Sync Materials"
    bl_description = "Create/update Blender materials from Godot .tres materials in res://assets/materials and res://game-assets/materials"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context) -> set:
        project_root: str = bpy.path.abspath(context.scene.godot_material_project_path.strip())

        if not project_root:
            self.report({'ERROR'}, "Godot project folder is not set. Set it in the Godot sidebar panel.")
            return {'CANCELLED'}

        if not os.path.isdir(project_root):
            self.report({'ERROR'}, f"Godot project folder does not exist: {project_root}")
            return {'CANCELLED'}

        tres_paths: list = []
        skipped_unsupported: int = 0
        for rel_dir in MATERIAL_SEARCH_DIRS:
            search_dir = os.path.join(project_root, rel_dir)
            if not os.path.isdir(search_dir):
                continue
            for root, _dirs, files in os.walk(search_dir):
                for fname in files:
                    if fname.endswith('.tres'):
                        tres_paths.append(os.path.join(root, fname))
                    elif fname.endswith('.material') or fname.endswith('.res'):
                        skipped_unsupported += 1

        if not tres_paths:
            self.report({'WARNING'}, "No .tres materials found in assets/materials or game-assets/materials.")
            return {'CANCELLED'}

        created_count: int = 0
        updated_count: int = 0
        all_warnings: list = []

        for tres_path in tres_paths:
            mat_name: str = os.path.splitext(os.path.basename(tres_path))[0]

            mtime = os.path.getmtime(tres_path)
            mtime_str = datetime.datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M:%S')
            print(f"[MaterialSync] Reading {tres_path} (last modified on disk: {mtime_str})")

            try:
                parsed: dict = _parse_tres(tres_path)
            except Exception as e:
                all_warnings.append(f"{mat_name}: failed to parse ({e})")
                continue

            if not parsed['type'] or 'Material' not in parsed['type']:
                all_warnings.append(f"{mat_name}: not a material resource ({parsed['type'] or 'unknown type'})")
                continue

            existing: bool = mat_name in bpy.data.materials
            mat = bpy.data.materials.get(mat_name)
            if mat is None:
                mat = bpy.data.materials.new(mat_name)

            warnings = _apply_material(mat, parsed, project_root)
            all_warnings.extend(f"{mat_name}: {w}" for w in warnings)

            if existing:
                updated_count += 1
            else:
                created_count += 1

        for w in all_warnings:
            print(f"[MaterialSync] {w}")

        msg: str = f"Synced materials: {created_count} created, {updated_count} updated"
        if skipped_unsupported:
            msg += f", {skipped_unsupported} skipped (.material/.res unsupported)"
        if all_warnings:
            msg += f", {len(all_warnings)} warning(s) — see console"
            self.report({'WARNING'}, msg)
        else:
            self.report({'INFO'}, msg)
        return {'FINISHED'}


class GODOT_OT_CleanupUnusedMaterials(Operator):
    bl_idname = "godot.cleanup_unused_materials"
    bl_label = "Cleanup to Only Used Materials"
    bl_description = "Delete every Blender material not currently assigned to any model's material slots"
    bl_options = {'REGISTER', 'UNDO'}

    def invoke(self, context, event) -> set:
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context) -> set:
        used_materials: set = set()
        for obj in bpy.data.objects:
            for slot in obj.material_slots:
                if slot.material is not None:
                    used_materials.add(slot.material)

        to_remove = [mat for mat in bpy.data.materials if mat not in used_materials]
        for mat in to_remove:
            print(f"[MaterialSync] Removing unused material: {mat.name}")
            bpy.data.materials.remove(mat, do_unlink=True)

        self.report({'INFO'}, f"Removed {len(to_remove)} unused material(s), kept {len(used_materials)}")
        return {'FINISHED'}


class GODOT_OT_ReloadMaterialSyncAddon(Operator):
    bl_idname = "godot.reload_material_sync_addon"
    bl_label = "Reload Addon"
    bl_description = "Reload the Godot Material Sync addon from disk"

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
            print("[MaterialSync] unregister() failed, continuing anyway:")
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

        self.report({'INFO'}, "Godot Material Sync reloaded.")
        return {'FINISHED'}


def _draw_help_popup(self, context) -> None:
    layout = self.layout
    layout.label(text="Tips:")
    layout.label(text="Godot material filename (without extension) becomes the Blender material name.")
    layout.label(text="Only text resources (.tres) are supported — .material/.res are skipped.")
    layout.label(text="")
    layout.label(text="1. Set 'Project Folder' to your Godot project's root (containing project.godot).")
    layout.label(text="2. Click 'Sync Materials'.")
    layout.label(text="3. Materials are matched by name and created/updated in place.")
    layout.label(text="4. After editing this addon's script, click 'Reload Addon'.")
    layout.label(text="5. 'Cleanup to Only Used Materials' deletes any material not")
    layout.label(text="   assigned to a model's material slots (asks for confirmation).")


class GODOT_OT_ShowMaterialSyncHelp(Operator):
    bl_idname = "godot.show_material_sync_help"
    bl_label = "Godot Material Sync Help"
    bl_description = "Show usage instructions for Godot Material Sync"

    def execute(self, context) -> set:
        context.window_manager.popup_menu(_draw_help_popup, title="Godot Material Sync — Usage", icon='INFO')
        return {'FINISHED'}


class GODOT_PT_MaterialSyncPanel(Panel):
    bl_label = "Godot Material Sync"
    bl_idname = "GODOT_PT_material_sync_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Godot"

    def draw(self, context) -> None:
        layout = self.layout
        scene = context.scene

        layout.prop(scene, "godot_material_project_path", text="Project Folder")

        layout.separator(type='LINE')
        layout.operator(GODOT_OT_SyncMaterials.bl_idname, text="Sync Materials", icon='MATERIAL')
        layout.operator(GODOT_OT_CleanupUnusedMaterials.bl_idname, text="Cleanup to Only Used Materials", icon='TRASH')
        layout.separator()
        layout.operator(GODOT_OT_ShowMaterialSyncHelp.bl_idname, text="Help", icon='QUESTION')
        layout.operator(GODOT_OT_ReloadMaterialSyncAddon.bl_idname, text="Reload Addon", icon='FILE_REFRESH')

        layout.separator()
        box = layout.box()
        box.label(text="Symlinked from repo:", icon='INFO')
        box.label(text="game-assets/automation/godot-material-sync.py")


def register() -> None:
    bpy.types.Scene.godot_material_project_path = StringProperty(
        name="Godot Project Path",
        description="Root path of the Godot project (containing project.godot)",
        default="",
        subtype='DIR_PATH',
    )
    bpy.utils.register_class(GODOT_OT_SyncMaterials)
    bpy.utils.register_class(GODOT_OT_CleanupUnusedMaterials)
    bpy.utils.register_class(GODOT_OT_ReloadMaterialSyncAddon)
    bpy.utils.register_class(GODOT_OT_ShowMaterialSyncHelp)
    bpy.utils.register_class(GODOT_PT_MaterialSyncPanel)


def unregister() -> None:
    bpy.utils.unregister_class(GODOT_PT_MaterialSyncPanel)
    bpy.utils.unregister_class(GODOT_OT_ShowMaterialSyncHelp)
    bpy.utils.unregister_class(GODOT_OT_ReloadMaterialSyncAddon)
    bpy.utils.unregister_class(GODOT_OT_CleanupUnusedMaterials)
    bpy.utils.unregister_class(GODOT_OT_SyncMaterials)
    del bpy.types.Scene.godot_material_project_path


if __name__ == "__main__":
    register()
