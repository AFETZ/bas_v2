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
local inventory state without user-specific absolute paths. No official ZIP or
extracted bundle was available during the 2026-08-26 inventory, so P3A has not
selected a town or a 10 by 10 km ROI. Archive size is not treated as evidence
of retained map bounds.

Town01 is unsuitable because the supplied, previously confirmed retained
bounds are only approximately 3.2 by 3.2 km. Town13 is only the next likely
candidate to inspect. It cannot be selected until its compact metadata proves
the required bounds, terrain, buildings, artifacts, and coordinate transform.

The exact unblock action is to place
`CAVISE_SIONNA_Town13_EditorLOD0_Full_Official_20260731.zip` in the directory
named by `CAVISE_MAPS_DIR`; it is not downloaded automatically.

Run the bounded metadata path first:

```bash
export CAVISE_MAPS_DIR=/external/path/containing/cavise
scripts/product/prepare_cavise_map.sh --metadata-only
```

The inspector reads the ZIP central directory and only allow-listed compact
metadata. It does not open PLY or Blender payloads. After measurable metadata
supports a selection, add `network/config/customer_map_roi.yaml` and prepare
only that bundle with an explicit extraction acknowledgement:

```bash
scripts/product/prepare_cavise_map.sh --prepare-selected --allow-large-extract
```

Full checksum verification is opt-in through `--verify-all`; it is not part of
normal metadata or prepare runs.

## Coordinate contract

The selected bundle's `map/transforms.xml` is the transformation authority.
P3A must record the source, Sionna, and Gazebo frames, the SUMO offset when one
is defined, and whether static vertex coordinates are baked. Until those
values are read, no identity transform, axis swap, origin, or offset is
assumed.

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

After a bundle and ROI are selected from measured metadata, P3B will derive a
tiled or selectively loaded Gazebo visual mesh and simplified collision mesh
from the selected Blender/CAVISE geometry. It will then add a numeric alignment
test that samples common landmarks in the original Sionna scene and the Gazebo
derivative. Heavy Sionna path tracing and full scene conversion remain outside
P3A.
