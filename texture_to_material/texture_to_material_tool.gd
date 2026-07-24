@tool
class_name TextureToMaterialTool
extends Node

## Batch generates a folder of materials from a folder of texture images.

## Files sharing a category_sourcepack_subject_variant prefix
## (differing only by trailing _maptype, e.g. _color/_normal/_roughness) are
## combined into a single material with each maptype wired to its texture slot.

const IMAGE_EXTS: Array[String] = ["png", "jpg", "jpeg"]
const _RESULT_CREATED: int = 0
const _RESULT_UPDATED: int = 1
const _RESULT_FAILED: int = 2
const MAPTYPE_SLOTS: Dictionary = {
	"color": {"texture": "albedo_texture", "enabled": ""},
	"albedo": {"texture": "albedo_texture", "enabled": ""},
	"normal": {"texture": "normal_texture", "enabled": "normal_enabled"},
	"roughness": {"texture": "roughness_texture", "enabled": ""},
	"metallic": {"texture": "metallic_texture", "enabled": ""},
	"ao": {"texture": "ao_texture", "enabled": "ao_enabled"},
	"emission": {"texture": "emission_texture", "enabled": "emission_enabled"},
}

## Folder of textures to generate materials from
@export_dir var input_folder: String = "res://game-assets/textures/default/wood/"
## Folder to output generated materials to
@export_dir var output_folder: String = "res://game-assets/materials/"
@export var recursive: bool = true

@export_group("Batch material settings")
@export var albedo_color: Color = Color.WHITE
@export_range(0.0, 1.0, 0.01) var metallic: float = 0.0
@export_range(0.0, 1.0, 0.01) var roughness: float = 1.0
@export_range(0.0, 1.0, 0.01) var specular: float = 0.5

@export_tool_button("Generate Materials")
var generate_materials_action: Callable = generate_materials


func generate_materials() -> void:
	if input_folder.is_empty() or output_folder.is_empty():
		push_error("TextureToMaterialTool: input_folder and output_folder must both be set")
		return

	if not _validate_folder_alignment():
		return

	DirAccess.make_dir_recursive_absolute(ProjectSettings.globalize_path(output_folder))

	var texture_paths: Array[String] = _collect_texture_files(input_folder, recursive)
	if texture_paths.is_empty():
		push_warning("TextureToMaterialTool: no image files found in ", input_folder)
		return

	var groups: Dictionary = {}  # relative_dir -> {group_key: {maptype: texture_path}}
	for path: String in texture_paths:
		var relative_dir: String = _relative_dir(path, input_folder)
		var parsed: Dictionary = _parse_texture_name(path)
		var group_key: String = parsed["group_key"]
		if not groups.has(relative_dir):
			groups[relative_dir] = {}
		if not groups[relative_dir].has(group_key):
			groups[relative_dir][group_key] = {}
		groups[relative_dir][group_key][parsed["maptype"]] = path

	var created := 0
	var updated := 0
	var failed := 0

	for relative_dir: String in groups.keys():
		var dir_groups: Dictionary = groups[relative_dir]
		for group_key: String in dir_groups.keys():
			var result: int = _write_material(relative_dir, group_key, dir_groups[group_key])
			if result == _RESULT_CREATED:
				created += 1
			elif result == _RESULT_UPDATED:
				updated += 1
			else:
				failed += 1

	print("TextureToMaterialTool: created ", created, ", updated ", updated, ", failed ", failed)

	if Engine.is_editor_hint():
		var fs := EditorInterface.get_resource_filesystem()
		if fs != null:
			fs.scan()


func _write_material(relative_dir: String, group_key: String, maptype_paths: Dictionary) -> int:
	var output_subfolder: String = output_folder if relative_dir.is_empty() else output_folder.path_join(relative_dir)
	DirAccess.make_dir_recursive_absolute(ProjectSettings.globalize_path(output_subfolder))

	var output_path: String = output_subfolder.path_join(group_key + ".tres")
	var material: StandardMaterial3D
	var is_new := true

	if ResourceLoader.exists(output_path):
		var existing: Resource = load(output_path)
		if existing is StandardMaterial3D:
			material = existing
			is_new = false
		else:
			push_warning("TextureToMaterialTool: ", output_path, " exists but is not a StandardMaterial3D, skipping")
			return _RESULT_FAILED
	else:
		material = StandardMaterial3D.new()

	for maptype: String in maptype_paths.keys():
		if not MAPTYPE_SLOTS.has(maptype):
			continue
		var slot: Dictionary = MAPTYPE_SLOTS[maptype]
		var texture: Texture2D = load(maptype_paths[maptype])
		if texture == null:
			push_warning("TextureToMaterialTool: failed to load texture ", maptype_paths[maptype])
			continue
		material.set(slot["texture"], texture)
		if slot["enabled"] != "":
			material.set(slot["enabled"], true)

	material.albedo_color = albedo_color
	material.metallic = metallic
	material.roughness = roughness
	material.metallic_specular = specular
	if _path_has_segment(output_path, "pixel"):
		material.texture_filter = BaseMaterial3D.TEXTURE_FILTER_NEAREST
	if _path_has_segment(output_path, "glass"):
		material.transparency = BaseMaterial3D.TRANSPARENCY_ALPHA
		material.cull_mode = BaseMaterial3D.CULL_DISABLED

	var err: Error = ResourceSaver.save(material, output_path)
	if err != OK:
		push_warning("TextureToMaterialTool: failed to save ", output_path, " (error ", err, ")")
		return _RESULT_FAILED

	return _RESULT_CREATED if is_new else _RESULT_UPDATED


## Guards against generating into the wrong folder: if both paths sit under
## a "textures" / "materials" root, their remaining subpath must match
## (e.g. .../textures/pixel/nature -> .../materials/pixel/nature). Mismatches
## like textures/ -> materials/pixel/nature would dump every material into
## one subfolder, so they're blocked.
func _validate_folder_alignment() -> bool:
	var input_relative: Variant = _relative_after_segment(input_folder, "textures")
	var output_relative: Variant = _relative_after_segment(output_folder, "materials")
	if input_relative == null or output_relative == null:
		return true
	if input_relative != output_relative:
		push_error(
			"TextureToMaterialTool: input_folder subpath 'textures/%s' doesn't match output_folder subpath 'materials/%s' - aborting to avoid generating materials into the wrong folder." % [input_relative, output_relative]
		)
		return false
	return true


func _relative_after_segment(path: String, segment: String) -> Variant:
	var parts: PackedStringArray = path.trim_suffix("/").split("/")
	var idx: int = parts.find(segment)
	if idx == -1:
		return null
	return "/".join(parts.slice(idx + 1))


func _path_has_segment(path: String, segment: String) -> bool:
	for part: String in path.split("/"):
		if part.to_lower() == segment:
			return true
	return false


func _relative_dir(full_path: String, base_folder: String) -> String:
	var base: String = base_folder if base_folder.ends_with("/") else base_folder + "/"
	return full_path.trim_prefix(base).get_base_dir()


func _parse_texture_name(path: String) -> Dictionary:
	var stem: String = path.get_file().get_basename()
	var tokens: PackedStringArray = stem.split("_")
	if tokens.size() > 1 and MAPTYPE_SLOTS.has(tokens[-1]):
		var maptype: String = tokens[-1]
		var group_key: String = "_".join(tokens.slice(0, tokens.size() - 1))
		return {"group_key": group_key, "maptype": maptype}
	return {"group_key": stem, "maptype": "color"}


func _collect_texture_files(folder: String, recurse: bool) -> Array[String]:
	var result: Array[String] = []
	var dir: DirAccess = DirAccess.open(folder)
	if dir == null:
		push_warning("TextureToMaterialTool: could not open folder ", folder)
		return result

	dir.list_dir_begin()
	var entry: String = dir.get_next()
	while entry != "":
		if not entry.begins_with("."):
			var full_path: String = folder.path_join(entry)
			if dir.current_is_dir():
				if recurse:
					result.append_array(_collect_texture_files(full_path, recurse))
			elif entry.get_extension().to_lower() in IMAGE_EXTS:
				result.append(full_path)
		entry = dir.get_next()
	dir.list_dir_end()

	return result
