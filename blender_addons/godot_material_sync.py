bl_info = {
    "name": "Godot Material Sync",
    "author": "Travh98",
    "version": (1, 3, 0),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > Godot",
    "description": "Sync Godot .tres materials from res://assets/materials and res://game-assets/materials into Blender",
    "category": "Material",
}

import bpy
import datetime
import importlib
import os
import re
import shutil
import sys
import uuid
from bpy.props import StringProperty
from bpy.types import Operator, Panel

MATERIAL_SEARCH_DIRS = ("assets/materials", "game-assets/materials")

# Synced materials are marked as assets and filed under this parent Asset Browser catalog
# (keeps them separate from other materials loaded in Blender). Each material's subfolder
# under the Godot materials dir becomes a nested catalog beneath it.
CATALOG_ROOT = "GodotMaterialSync"
_NULL_CATALOG_ID = "00000000-0000-0000-0000-000000000000"
_CATS_FILENAME = "blender_assets.cats.txt"
_CATS_HEADER = (
    "# This is an Asset Catalog Definition file for Blender.\n"
    "#\n"
    "# Empty lines and lines starting with `#` will be ignored.\n"
    "# The first non-ignored line should be the version indicator.\n"
    "# Other lines are of the format \"UUID:catalog/path/for/assets:simple catalog name\"\n"
    "\n"
    "VERSION 1\n"
    "\n"
)

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


def _category_for(tres_path: str, search_dir: str) -> str:
    """Catalog path for a .tres: its subfolder(s) under the materials dir, nested under
    CATALOG_ROOT, e.g. materials/wood/oak/x.tres -> 'GodotMaterialSync/wood/oak'.
    Files at the root of the materials dir go directly in CATALOG_ROOT."""
    rel_dir = os.path.relpath(os.path.dirname(tres_path), search_dir)
    parts = [p for p in re.split(r'[\\/]+', rel_dir) if p and p != '.']
    return "/".join([CATALOG_ROOT] + parts)


def _catalog_uuid(catalog_path: str) -> str:
    # Deterministic so re-syncing (or syncing on another machine) yields the same catalog.
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "godot_material_sync/" + catalog_path))


def _ensure_catalogs(catalog_paths: set) -> dict:
    """Make sure blender_assets.cats.txt (next to the .blend) lists every catalog plus its
    parents, keeping existing entries. Returns {catalog_path: uuid}, or {} if the .blend
    hasn't been saved yet (there's nowhere to put the file)."""
    if not bpy.data.filepath:
        return {}
    cats_path = os.path.join(os.path.dirname(bpy.data.filepath), _CATS_FILENAME)

    text = ""
    existing: dict = {}  # path -> uuid
    if os.path.isfile(cats_path):
        with open(cats_path, 'r', encoding='utf-8') as f:
            text = f.read()
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith('#') or line.startswith('VERSION'):
                continue
            parts = line.split(':', 2)
            if len(parts) >= 2:
                existing[parts[1]] = parts[0]
    else:
        text = _CATS_HEADER

    result: dict = {}
    added: list = []
    for path in sorted(catalog_paths):
        pieces = path.split('/')
        for i in range(1, len(pieces) + 1):
            sub = "/".join(pieces[:i])
            if sub not in existing:
                existing[sub] = _catalog_uuid(sub)
                added.append(f"{existing[sub]}:{sub}:{sub.replace('/', '-')}\n")
        result[path] = existing[path]

    if added:
        if text and not text.endswith('\n'):
            text += '\n'
        with open(cats_path, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text + "".join(added))
        print(f"[MaterialSync] Added {len(added)} asset catalog(s) to {cats_path}")
    return result


def _mark_material_as_asset(mat, catalog_id) -> bool:
    """Mark a material as an asset and file it under its catalog. The Godot folder is the
    source of truth, so the catalog is re-applied on every sync. Returns True if newly marked."""
    newly_marked = mat.asset_data is None
    if newly_marked:
        mat.asset_mark()
        try:
            mat.asset_generate_preview()
        except Exception:
            pass  # previews can't be generated in some contexts (e.g. background mode)
    if catalog_id and mat.asset_data is not None:
        mat.asset_data.catalog_id = catalog_id
    return newly_marked


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

        tres_paths: list = []  # (path, search_dir)
        skipped_unsupported: int = 0
        for rel_dir in MATERIAL_SEARCH_DIRS:
            search_dir = os.path.join(project_root, rel_dir)
            if not os.path.isdir(search_dir):
                continue
            for root, _dirs, files in os.walk(search_dir):
                for fname in files:
                    if fname.endswith('.tres'):
                        tres_paths.append((os.path.join(root, fname), search_dir))
                    elif fname.endswith('.material') or fname.endswith('.res'):
                        skipped_unsupported += 1

        if not tres_paths:
            self.report({'WARNING'}, "No .tres materials found in assets/materials or game-assets/materials.")
            return {'CANCELLED'}

        created_count: int = 0
        updated_count: int = 0
        newly_marked_count: int = 0
        all_warnings: list = []

        categories: dict = {tp: _category_for(tp, sd) for tp, sd in tres_paths}
        try:
            catalog_ids: dict = _ensure_catalogs(set(categories.values()))
        except OSError as e:
            catalog_ids = {}
            all_warnings.append(f"could not write {_CATS_FILENAME} ({e}) — assets left uncategorised")
        if not catalog_ids and not bpy.data.filepath:
            all_warnings.append(
                f"save the .blend first so the '{CATALOG_ROOT}' catalogs can be created — materials were marked as assets but left uncategorised")

        for tres_path, _search_dir in tres_paths:
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

            if _mark_material_as_asset(mat, catalog_ids.get(categories[tres_path])):
                newly_marked_count += 1

            if existing:
                updated_count += 1
            else:
                created_count += 1

        for w in all_warnings:
            print(f"[MaterialSync] {w}")

        if catalog_ids:
            try:
                bpy.ops.asset.library_refresh()  # make open Asset Browsers pick up the new catalog
            except Exception:
                pass

        msg: str = f"Synced materials: {created_count} created, {updated_count} updated"
        msg += f", {newly_marked_count} newly marked as assets in '{CATALOG_ROOT}'"
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


def _version_string() -> str:
    return ".".join(str(v) for v in bl_info["version"])


def _resolve_dev_source(scene) -> str:
    """Path of the repo copy of this script to sync from: the Dev Source File if set,
    else the one inside the Godot Project Folder. Empty string if neither exists."""
    explicit = bpy.path.abspath(scene.godot_material_dev_source.strip())
    if explicit:
        return explicit
    root = bpy.path.abspath(scene.godot_material_project_path.strip())
    if root:
        guess = os.path.join(root, "game-assets", "godot-workflow-automation", "blender_addons", "godot_material_sync.py")
        if os.path.isfile(guess):
            return guess
    return ""


def _reload_addon(source_file: str) -> str:
    """Copy source_file over the installed script (if different), unregister, drop the
    cached module and register the fresh code. Source is checked BEFORE unregister so a
    bad path leaves the working addon in place."""
    module_name: str = __name__
    installed = os.path.abspath(__file__)
    copied = False
    if source_file:
        if not os.path.isfile(source_file):
            raise FileNotFoundError(f"Dev Source File not found: {source_file}")
        if os.path.realpath(source_file) != os.path.realpath(installed):
            shutil.copyfile(source_file, installed)  # copyfile (not copy2) so the .pyc is invalidated
            copied = True
    sys.modules[module_name].unregister()
    del sys.modules[module_name]
    importlib.invalidate_caches()
    fresh = importlib.import_module(module_name)
    fresh.register()
    version = ".".join(str(v) for v in fresh.bl_info["version"])
    return f"Reloaded Godot Material Sync v{version} ({'copied from source' if copied else 'no file copied'})"


def _reload_later(source_file: str):
    """Timer callback: reloads once the operator has returned, so no class is replaced mid-run."""
    try:
        message = _reload_addon(source_file)
    except Exception as e:
        message = f"Reload failed: {e}"
    if not source_file and not message.startswith("Reload failed"):
        message += " - Dev Source File is empty, so no new code was copied"
    print("[MaterialSync]", message)
    try:
        bpy.context.window_manager.popup_menu(lambda menu, _context: menu.label(text=message), title="Godot Material Sync", icon='INFO')
    except Exception:
        pass
    try:
        bpy.context.workspace.status_text_set(message)
        bpy.app.timers.register(lambda: bpy.context.workspace.status_text_set(None), first_interval=10.0)
    except Exception:
        pass
    return None


class GODOT_OT_ReloadMaterialSyncAddon(Operator):
    bl_idname = "godot.reload_material_sync_addon"
    bl_label = "Reload Addon"
    bl_description = ("Re-import the addon code in place, first copying the Dev Source File over the installed "
                      "copy. If Dev Source File is empty, falls back to <Project Folder>/game-assets/godot-workflow-automation/blender_addons/godot_material_sync.py")

    def execute(self, context) -> set:
        source = _resolve_dev_source(context.scene)
        bpy.app.timers.register(lambda: _reload_later(source), first_interval=0.1)
        if source:
            self.report({'INFO'}, f"Reloading addon from {source} ...")
        else:
            self.report({'WARNING'}, "No Dev Source File found: only re-importing the installed file, no new code is copied")
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
    layout.label(text="   Each is marked as an asset in 'GodotMaterialSync/<subfolder>' (save the .blend first).")
    layout.label(text="4. After editing this addon's script, click 'Reload Addon'. It copies the repo script")
    layout.label(text="   (Dev Source File, or <Project Folder>/game-assets/godot-workflow-automation/) over the installed one.")
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
    bl_label = "Godot Material Sync v" + ".".join(str(v) for v in bl_info["version"])
    bl_idname = "GODOT_PT_material_sync_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Godot"

    def draw(self, context) -> None:
        layout = self.layout
        scene = context.scene

        layout.prop(scene, "godot_material_project_path", text="Project Folder")
        layout.prop(scene, "godot_material_dev_source", text="Dev Source File")

        layout.separator(type='LINE')
        layout.operator(GODOT_OT_SyncMaterials.bl_idname, text="Sync Materials", icon='MATERIAL')
        layout.operator(GODOT_OT_CleanupUnusedMaterials.bl_idname, text="Cleanup to Only Used Materials", icon='TRASH')
        layout.separator()
        layout.operator(GODOT_OT_ShowMaterialSyncHelp.bl_idname, text="Help", icon='QUESTION')
        layout.operator(GODOT_OT_ReloadMaterialSyncAddon.bl_idname, text="Reload Addon", icon='FILE_REFRESH')


def register() -> None:
    bpy.types.Scene.godot_material_project_path = StringProperty(
        name="Godot Project Path",
        description="Root path of the Godot project (containing project.godot)",
        default="",
        subtype='DIR_PATH',
    )
    bpy.types.Scene.godot_material_dev_source = StringProperty(
        name="Dev Source File",
        description=("Optional: the repo copy of godot_material_sync.py to copy over the installed addon on Reload. "
                     "If empty, uses <Project Folder>/game-assets/godot-workflow-automation/blender_addons/godot_material_sync.py when it exists"),
        default="",
        subtype='FILE_PATH',
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
    del bpy.types.Scene.godot_material_dev_source
    del bpy.types.Scene.godot_material_project_path


if __name__ == "__main__":
    register()
