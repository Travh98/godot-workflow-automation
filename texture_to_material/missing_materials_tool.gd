@tool
class_name MissingMaterialsTool
extends Node

## Finds textures that have no matching material and generates the missing ones.
##
## The textures and materials roots are expected to share the same subfolder
## structure: textures/a/b/foo_color.png -> materials/a/b/foo.tres. Texture
## grouping, map-type wiring and material settings come from TextureToMaterialTool.
## Existing materials are never touched.

## Top-level textures folder
@export_dir var textures_root: String = "res://game-assets/textures/"
## Top-level materials folder (mirrors the textures folder structure)
@export_dir var materials_root: String = "res://game-assets/materials/"

## Folders to skip, including their subfolders. Give either the materials path
## (res://game-assets/materials/pixel/particles/) or the textures path; only the
## part below the root is used, so both match the same subfolder.
@export_dir var blacklisted_folders: Array[String] = []

@export_group("Batch material settings")
@export var albedo_color: Color = Color.WHITE
@export_range(0.0, 1.0, 0.01) var metallic: float = 0.0
@export_range(0.0, 1.0, 0.01) var roughness: float = 1.0
@export_range(0.0, 1.0, 0.01) var specular: float = 0.5

@export_tool_button("Dry Run (list missing)")
var dry_run_action: Callable = dry_run
@export_tool_button("Generate Missing Materials")
var generate_action: Callable = generate_missing


func dry_run() -> void:
	var missing: Array[Dictionary] = _find_missing()
	if missing.is_empty():
		print("MissingMaterialsTool: dry run - no missing materials")
		return
	print("MissingMaterialsTool: dry run - %d material(s) would be created:" % missing.size())
	for entry: Dictionary in missing:
		var files: PackedStringArray = []
		for p: String in entry["maptypes"].values():
			files.append(p.get_file())
		print("  ", entry["output_path"], "  <-  ", ", ".join(files))


func generate_missing() -> void:
	var missing: Array[Dictionary] = _find_missing()
	if missing.is_empty():
		print("MissingMaterialsTool: no missing materials")
		return

	var helper := TextureToMaterialTool.new()
	helper.input_folder = textures_root
	helper.output_folder = materials_root
	helper.albedo_color = albedo_color
	helper.metallic = metallic
	helper.roughness = roughness
	helper.specular = specular

	var created := 0
	var failed := 0
	for entry: Dictionary in missing:
		var result: int = helper._write_material(entry["relative_dir"], entry["group_key"], entry["maptypes"])
		if result == helper._RESULT_FAILED:
			failed += 1
		else:
			created += 1
			print("  created ", entry["output_path"])
	helper.free()

	print("MissingMaterialsTool: created ", created, ", failed ", failed)

	if Engine.is_editor_hint():
		var fs := EditorInterface.get_resource_filesystem()
		if fs != null:
			fs.scan()


## Returns [{relative_dir, group_key, maptypes, output_path}] for every texture
## group whose material file doesn't exist yet.
func _find_missing() -> Array[Dictionary]:
	var result: Array[Dictionary] = []
	if textures_root.is_empty() or materials_root.is_empty():
		push_error("MissingMaterialsTool: textures_root and materials_root must both be set")
		return result

	var helper := TextureToMaterialTool.new()
	var texture_paths: Array[String] = helper._collect_texture_files(textures_root, true)

	var blacklist: Array[String] = _blacklist_relative_dirs()
	var groups: Dictionary = {}  # output_path -> entry
	for path: String in texture_paths:
		var relative_dir: String = helper._relative_dir(path, textures_root)
		if _is_blacklisted(relative_dir, blacklist):
			continue
		var parsed: Dictionary = helper._parse_texture_name(path)
		var group_key: String = parsed["group_key"]
		var base: String = materials_root if relative_dir.is_empty() else materials_root.path_join(relative_dir)
		var output_path: String = base.path_join(group_key + ".tres")
		if not groups.has(output_path):
			groups[output_path] = {
				"relative_dir": relative_dir,
				"group_key": group_key,
				"maptypes": {},
				"output_path": output_path,
			}
		groups[output_path]["maptypes"][parsed["maptype"]] = path
	helper.free()

	var output_paths: Array = groups.keys()
	output_paths.sort()
	for output_path: String in output_paths:
		if not ResourceLoader.exists(output_path):
			result.append(groups[output_path])
	return result


## Blacklist entries converted to subpaths relative to their root.
func _blacklist_relative_dirs() -> Array[String]:
	var result: Array[String] = []
	for folder: String in blacklisted_folders:
		var rel: String = ""
		var matched := false
		for root: String in [materials_root, textures_root]:
			var base: String = root.trim_suffix("/") + "/"
			var f: String = folder.trim_suffix("/") + "/"
			if f.begins_with(base):
				rel = f.trim_prefix(base).trim_suffix("/")
				matched = true
				break
			if f == base:
				rel = ""
				matched = true
				break
		if not matched:
			push_warning("MissingMaterialsTool: blacklisted folder '%s' is not under either root, ignoring" % folder)
		else:
			result.append(rel)
	return result


func _is_blacklisted(relative_dir: String, blacklist: Array[String]) -> bool:
	for rel: String in blacklist:
		if rel.is_empty() or relative_dir == rel or relative_dir.begins_with(rel + "/"):
			return true
	return false
