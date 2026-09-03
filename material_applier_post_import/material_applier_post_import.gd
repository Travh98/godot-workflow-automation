@tool
class_name MaterialApplierPostImport
extends EditorScenePostImport

## Gets applied as import_script for models from the Blender-Godot pipeline
## Uses material_map.json to find names of materials, then applies them

func _post_import(scene: Node) -> Object:
	var source: String = get_source_file()
	print("MaterialApplier: running on ", source)

	var matches: Array = _find_all_matching_maps(source)
	if matches.is_empty():
		print("MaterialApplier: no material_map.json entry found for ", source)
		return scene

	if matches.size() > 1:
		var other_paths: Array = []
		for i: int in range(1, matches.size()):
			other_paths.append("%s (key '%s')" % [matches[i].map_path, matches[i].rel_key])
		push_warning("MaterialApplier: '%s' matched in multiple material_map.json files — using '%s' (key '%s'), ignoring shadowed match(es): %s" % [
			source.get_file(), matches[0].map_path, matches[0].rel_key, ", ".join(other_paths),
		])

	var mat_data: Variant = matches[0].data
	print("MaterialApplier: found for '", matches[0].rel_key, "' in ", matches[0].map_path, ": ", mat_data)
	if mat_data is Array:
		_apply_to_node(scene, mat_data as Array)
	elif mat_data is Dictionary:
		# Armature export: material list is keyed by skinned mesh name
		# instead of one flat list for the whole family.
		_apply_by_mesh_name(scene, mat_data as Dictionary)
	print("MaterialApplier: done")
	return scene


func _apply_to_node(node: Node, mat_names: Array) -> void:
	if node is MeshInstance3D:
		for i: int in mat_names.size():
			var mat_name: String = mat_names[i]
			if mat_name.is_empty():
				continue
			var mat_path: String = _find_material_path(mat_name)
			if mat_path.is_empty():
				push_warning("MaterialApplier: no .tres/.material/.res found for: " + mat_name)
				continue
			(node as MeshInstance3D).set_surface_override_material(i, load(mat_path) as Material)
	for child: Node in node.get_children():
		_apply_to_node(child, mat_names)


func _apply_by_mesh_name(node: Node, mesh_materials: Dictionary) -> void:
	if node is MeshInstance3D:
		var mat_names: Variant = _lookup_mesh_materials(node.name, mesh_materials)
		if mat_names is Array:
			for i: int in mat_names.size():
				var mat_name: String = mat_names[i]
				if mat_name.is_empty():
					continue
				var mat_path: String = _find_material_path(mat_name)
				if mat_path.is_empty():
					push_warning("MaterialApplier: no .tres/.material/.res found for: " + mat_name)
					continue
				(node as MeshInstance3D).set_surface_override_material(i, load(mat_path) as Material)
	for child: Node in node.get_children():
		_apply_by_mesh_name(child, mesh_materials)


func _lookup_mesh_materials(node_name: String, mesh_materials: Dictionary) -> Variant:
	if mesh_materials.has(node_name):
		return mesh_materials[node_name]
	# Godot's "-col"/"-convcol" import suffixes generate a collision shape and
	# strip the suffix from the resulting MeshInstance3D's name, so a mesh
	# named e.g. "angled-pillar-convcol" in Blender/material_map.json shows up
	# here as just "angled-pillar". Fall back to trying the suffixed keys.
	for suffix in ["-col", "-convcol"]:
		if mesh_materials.has(node_name + suffix):
			return mesh_materials[node_name + suffix]
	return null


func _find_material_path(mat_name: String) -> String:
	# Search project specific materials, then search game-assets materials, recursively
	for root: String in ["res://assets/materials", "res://game-assets/materials"]:
		var matches: Dictionary = {}
		_collect_material_matches(root, mat_name, matches)
		for ext: String in [".tres", ".material", ".res"]:
			if matches.has(ext):
				return matches[ext]
	return ""


func _collect_material_matches(dir_path: String, mat_name: String, matches: Dictionary) -> void:
	var dir: DirAccess = DirAccess.open(dir_path)
	if dir == null:
		return

	dir.list_dir_begin()
	var entry: String = dir.get_next()
	while entry != "":
		if not entry.begins_with("."):
			var full_path: String = dir_path.path_join(entry)
			if dir.current_is_dir():
				_collect_material_matches(full_path, mat_name, matches)
			else:
				var ext: String = "." + entry.get_extension()
				if entry.get_basename() == mat_name and ext in [".tres", ".material", ".res"]:
					if matches.has(ext):
						push_warning("MaterialApplier: multiple '%s%s' materials found — using '%s', ignoring '%s'. Rename one to disambiguate." % [mat_name, ext, matches[ext], full_path])
					else:
						matches[ext] = full_path
		entry = dir.get_next()
	dir.list_dir_end()


## Walks every directory from the glb's folder up to res://, opening any
## material_map.json found along the way. A directory closer to the glb
## isn't assumed to be the only source of truth — a stale map left behind
## higher (or lower) in the tree can silently shadow the real one, so every
## level is checked and reported. Returns matches ordered nearest-first;
## the caller uses index 0 and warns about the rest.
func _find_all_matching_maps(glb_path: String) -> Array:
	var matches: Array = []
	var dir: String = glb_path.get_base_dir()
	while dir.length() >= "res://".length():
		var map_path: String = dir + "/material_map.json"
		var f := FileAccess.open(ProjectSettings.globalize_path(map_path), FileAccess.READ)
		if f != null:
			var material_map: Variant = JSON.parse_string(f.get_as_text())
			if material_map is Dictionary:
				var rel_key: String = glb_path.trim_prefix(dir + "/").trim_suffix(".glb")
				if (material_map as Dictionary).has(rel_key):
					matches.append({
						"map_path": map_path,
						"rel_key": rel_key,
						"data": (material_map as Dictionary)[rel_key],
					})
		if dir == "res://":
			break
		var parent: String = dir.get_base_dir()
		if parent == dir:
			break
		dir = parent
	return matches
