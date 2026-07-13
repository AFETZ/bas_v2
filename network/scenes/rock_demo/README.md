# rock_demo matched Gazebo/Sionna scene

`rock_demo` is a matched-scene scaffold for checking that shared terrain,
settlement blocks, and the rock obstacle are seen consistently by Gazebo and
Sionna RT. No SDF-to-Sionna converter is used here. Blender or deterministic
OBJ generation is the canonical geometry path; the Gazebo and Sionna artifacts
reference the exported meshes in the same ENU meter frame.

The validated 65-second runtime proof uses the ROS package world assets under
`src/multiagent_simulation/worlds/rock_demo/` and the network configs
`network/config/scenario_rock_demo.yaml`,
`network/config/radio_24ghz_rock_demo.yaml`, and
`network/config/jammers_rock_demo.yaml`. The `network/scenes/rock_demo`
subtree is the portable authoring/export scaffold for the same mature workflow.

## Runtime Scene

The ROS package world assets are the validated runtime source of truth:

- `src/multiagent_simulation/worlds/rock_demo/rock_demo.sdf`
- `src/multiagent_simulation/worlds/rock_demo/sionna_scene.xml`
- `src/multiagent_simulation/worlds/rock_demo/engineering_terrain.obj`
- `src/multiagent_simulation/worlds/rock_demo/engineering_buildings.obj`
- `src/multiagent_simulation/worlds/rock_demo/radio_blocker.obj`

`engineering_terrain.obj` is a deterministic 10 km x 10 km engineering terrain
mesh. `engineering_buildings.obj` adds settlement blocks. `radio_blocker.obj`
is the rock-shadow obstacle used for the low-altitude link demo.

Gazebo uses the terrain/building/rock meshes as visual geometry. The drone
physics still uses a stable ground plane plus a box collision proxy for the
rock, so headless SITL does not crash on arbitrary OBJ collision meshes.
Sionna uses the OBJ meshes as RF geometry.

Regenerate the deterministic engineering meshes with:

```bash
./network/scenes/rock_demo/generate_engineering_scene_assets.py
```

## Canonical Portable Obstacle

The portable `network/scenes/rock_demo/` scaffold still contains the original
Blender-style single-obstacle export path:

- Model: `rock_demo_spire`
- Mesh asset: `gazebo/models/rock_demo_spire/meshes/rock_demo_spire.obj`
- Material sidecar: `gazebo/models/rock_demo_spire/meshes/rock_demo_spire.mtl`
- Mesh units: meters
- Coordinate frame: ENU, Z-up
- Mesh local origin: obstacle ground-contact center
- Runtime scale: `1 1 1`

## Files

- `gazebo/models/rock_demo_spire/model.sdf` defines the static Gazebo model.
- `gazebo/models/rock_demo_spire/model.config` makes it discoverable through
  `model://rock_demo_spire`.
- `gazebo/worlds/rock_demo.world` is a standalone Gazebo world containing the
  obstacle include.
- `gazebo/include/rock_demo_spire.include.sdf` is a copy-paste include snippet
  for an existing world.
- `sionna/rock_demo.xml` is the Mitsuba/Sionna scene referencing the same OBJ.
- `scene_manifest.yaml` records the shared paths and pose for review.

## Gazebo hookup

Standalone smoke scene:

```bash
export GZ_SIM_RESOURCE_PATH="$PWD/network/scenes/rock_demo/gazebo/models:${GZ_SIM_RESOURCE_PATH:-}"
gz sim network/scenes/rock_demo/gazebo/worlds/rock_demo.world
```

To attach the obstacle to an existing Gazebo world, add this include inside the
target `<world>` and keep the same pose:

```xml
<include>
  <uri>model://rock_demo_spire</uri>
  <name>rock_demo_spire</name>
  <pose>125 -80 0 0 0 0</pose>
</include>
```

The parent process still needs:

```bash
export GZ_SIM_RESOURCE_PATH="$PWD/network/scenes/rock_demo/gazebo/models:${GZ_SIM_RESOURCE_PATH:-}"
```

## Sionna hookup

The current provider reads its scene from `network/config/radio_24ghz.yaml`.
Keep this scene directory self-contained and point a copied/overridden radio
config at:

```yaml
sionna:
  scene:
    id: rock_demo
    source: mitsuba_xml
    path: network/scenes/rock_demo/sionna/rock_demo.xml
```

Then start the provider with that config, for example:

```bash
python3 network/radio_provider/provider.py serve \
  --mode real_sionna \
  --radio-config path/to/radio_24ghz_rock_demo.yaml
```

## Blender-as-canonical pipeline

1. In Blender, author the obstacle in meters with Z up and local origin at the
   ground-contact center.
2. Apply transforms before export, so the OBJ vertices are in final local mesh
   units and runtime scale remains `1 1 1`.
3. Export OBJ and MTL together into
   `gazebo/models/rock_demo_spire/meshes/`.
4. Do not bake the world pose into the mesh. Keep the world transform mirrored
   in the Gazebo include/world pose and the Sionna `to_world` transform.
5. If the obstacle moves, update both pose declarations and
   `scene_manifest.yaml` in the same change.
