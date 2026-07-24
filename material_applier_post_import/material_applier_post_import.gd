@tool
class_name MaterialApplierPostImport
extends EditorScenePostImport

## Gets applied as import_script for models from the Blender-Godot pipeline
## Uses material_map.json to find names of materials, then applies them

func _post_import(scene: Node) -> Object:
	var source: String = get_source_file()
	print("MaterialApplier: running on ", source)

	var map_root: String = _find_map_root(source)
	if map_root.is_empty():
		print("MaterialApplier: no material_map.json found for ", source)
		return scene

	var map_file := FileAccess.open(ProjectSettings.globalize_path(map_root + "/material_map.json"), FileAccess.READ)
	if map_file == null:
		return scene
	var material_map: Variant = JSON.parse_string(map_file.get_as_text())
	if not material_map is Dictionary:
		return scene

	var rel_key: String = source.trim_prefix(map_root + "/").trim_suffix(".glb")
	print("MaterialApplier: looking up key '", rel_key, "'")
	var mat_data: Variant = (material_map as Dictionary).get(rel_key, [])
	print("MaterialApplier: found for '", rel_key, "': ", mat_data)
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


func _find_map_root(glb_path: String) -> String:
	var dir: String = glb_path.get_base_dir()
	while dir.length() > "res://".length():
		var f := FileAccess.open(ProjectSettings.globalize_path(dir + "/material_map.json"), FileAccess.READ)
		if f != null:
			return dir
		var parent: String = dir.get_base_dir()
		if parent == dir:
			break
		dir = parent
	return ""
