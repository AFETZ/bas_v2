# CAVISE Map Integration

## Product asset boundary

The official CAVISE bundles are the canonical geometry for the customer map.
They already contain the CARLA-to-Blender export and the portable Sionna RT
scene. P3 must select from these assets; it must not export CARLA again, draw a
new terrain, add synthetic buildings, or stitch different towns together.

Each official bundle is expected to provide:

- an editable `<Town>_Editor_lod0_Full.blend` master artifact;
- `map/scene.xml` plus the PLY meshes loaded by Sionna RT;
- `map/transforms.xml` as the coordinate-transform source;
- compact export metadata and checksums.

Sionna uses the existing `scene.xml` and referenced PLY files directly. The
Blender file remains the editable master asset. Gazebo geometry will be a
derivative of that same master geometry, never a separately authored map.

Gazebo export may decimate visual meshes, tile the world, and simplify
collision meshes. Vegetation collision may be disabled. These operations may
reduce runtime cost but must preserve object placement and the documented
coordinate frame. They must not introduce replacement terrain, buildings, or
transitions that are absent from the selected CAVISE town.

## P3A selection state

`network/config/cavise_map_catalog.yaml` records the official filenames and
local inventory state without user-specific absolute paths. On 2026-08-27,
Town01 was downloaded from its canonical Drive file ID, inspected, verified
against all 310 entries in `SHA256SUMS`, and prepared outside Git.

Town01 is the explicit user-selected development map. Its ROI is the complete
retained footprint:

- X: -1577.708829814624 to 1613.447172459159 m;
- Y: -1474.8671726619064 to 1716.2871790967324 m;
- Z: -273.01936977670994 to 220.3868920750865 m;
- 401 buildings, 3 terrain objects, 719 road objects, and 8774 vegetation
  objects.

This footprint is only 3191.156 by 3191.154 m and its global Z span is
493.406 m. Town01 therefore does not satisfy the customer 10 by 10 km and
up-to-200 m requirements. It is selected to implement and test the real asset
path without synthetic padding, tiling copies, or false compliance claims.

Run the bounded metadata path first:

```bash
export CAVISE_MAPS_DIR=/external/path/containing/cavise
scripts/product/prepare_cavise_map.sh --metadata-only
```

The inspector reads the ZIP central directory and only allow-listed compact
metadata. It does not open PLY or Blender payloads. The measured Town01
selection is stored in `network/config/customer_map_roi.yaml`. Prepare only
that bundle with an explicit extraction acknowledgement:

```bash
scripts/product/prepare_cavise_map.sh --prepare-selected --allow-large-extract
```

Full checksum verification is opt-in through `--verify-all`; it is not part of
normal metadata or prepare runs.

## Coordinate contract

The selected bundle's `map/transforms.xml` is the transformation authority.
Town01 static vertices already include SUMO offset `[0.06, 328.61]`. Its
dynamic SUMO-to-Sionna remap, rotation, and translation are identity, and the
metadata declares `static_vertices_baked: true`.

Sionna retains the bundle frame. The Gazebo derivative must use the same frame
and numerical placements. Any runtime pose adapter must apply the recorded
transform explicitly; it may not compensate by moving map geometry.

## External assets and legacy smoke scene

ZIP, PLY, and Blender assets stay outside Git. Prepared content is extracted
only under the ignored `.external/cavise_maps/<Town>/` directory or used from
an already extracted directory below `CAVISE_MAPS_DIR`.

The existing `m4_canonical` generator, configs, and checked-in world are a
legacy synthetic smoke fixture. They remain only for tests that still depend
on them. They are not the customer map, are not an implementation of the 10 by
10 km requirement, and must not be generated, extended, or used by the active
product path.

## Next task: P3B

P3B will derive a tiled or selectively loaded Town01 Gazebo visual mesh and
simplified collision mesh from the selected Blender/CAVISE geometry. It will
then add a numeric alignment test that samples common landmarks in the
original Sionna scene and the Gazebo derivative. This exercises the real asset
path but cannot close the separate 10 by 10 km product requirement. Heavy
Sionna path tracing remains outside P3A.
