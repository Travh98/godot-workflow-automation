# godot-workflow-automation
Tools I use to automate my Blender to Godot art workflow

## Mentality
These tools and ideas work for me, but are subject to change as I learn more.
Back up your precious files before using any of these tools.

- Godot holds the truth for Materials and Textures.
- Blender holds the truth for Animations or Models.

## Tools

### Batch Generate Godot Materials from a folder of Textures
If you have a folder of texture images (pngs, jpegs, etc) and want materials from them, use this tool to auto-generate materials.

To use, open the `texture_to_material_tool.tscn` scene in Godot and use the exported fields and buttons in the inspector to batch generate your materials.

### Sync Materials from Godot to Blender
You build materials in Godot. You want to see the Godot materials in blender. Install the `godot-material-sync.py` addon into Blender, point the addon to your Godot `.project` file and press sync. Blender will have copies of all of the materials from your Godot project inside of Blender, so your models in Blender will look exactly like how they will look in Godot.

### Batch export .glb from Blender to Godot, with Materials Mapped
You build models in Blender. You apply materials to the blender models, and the material names match the Godot material names. You install the `godot_asset_exporter.py` Blender addon. You have the `material_applier_post_import.gd` script in your project and have updated `godot_asset_exporter.py`'s `MATERIAL_APPLIER_SCRIPT_PATH` to point to the godot script.

Select the Blender collection you want to export and select the output models folder. Press export and the meshes will be exported as individual .glb files, and a material_map.json will hold the data for what material each mesh uses.

As the .glb files get imported into Godot, their import script `material_applier_post_import.gd` will find the materials in material_map.json and apply them to the model.