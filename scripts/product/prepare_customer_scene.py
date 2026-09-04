#!/usr/bin/env python3
"""Append a synthetic 10 km field to canonical Town01; never transform Town01.
Requires the isolated geometry tool Shapely 2.1.1 (GEOS constrained triangulation).
"""
from __future__ import annotations
import argparse
import json
import math
import struct
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
import numpy as np
import shapely
from shapely.geometry import Polygon, LineString, Point, box
from shapely.ops import unary_union
from shapely.strtree import STRtree
sys.path.insert(0, str(Path(__file__).resolve().parent))
from prepare_town01_gazebo import read_ply_header, mesh_entries, convert_ply_to_obj

ROOT = Path(__file__).resolve().parents[2]


def triangles(path):
    with path.open('rb') as stream:
        h = read_ply_header(stream, path)
        v = []
        for _ in range(h.vertex_count):
            raw = h.vertex_struct.unpack(stream.read(h.vertex_struct.size))
            v.append(tuple(raw[i] for i in h.xyz_indices))
        for _ in range(h.face_count):
            n = h.face_count_struct.unpack(stream.read(h.face_count_struct.size))[0]
            ids = [h.face_index_struct.unpack(stream.read(h.face_index_struct.size))[0] for _ in range(n)]
            for i in range(1,n-1):
                yield [v[k] for k in (ids[0],ids[i],ids[i+1])]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, default=ROOT/'.external/cavise_maps/Town01')
    parser.add_argument('--output', type=Path, default=ROOT/'.external/customer_10km')
    args = parser.parse_args()
    src, out = args.bundle.resolve(), args.output.resolve()
    out.mkdir(parents=True,exist_ok=True)
    surface = []
    for path, _ in mesh_entries(src/'map/scene.xml', {'terrain','ground','road','sidewalk'}):
        valid = [Polygon(t) for t in triangles(path) if Polygon(t).area > 1e-7]
        surface.append(unary_union(valid))
    union = shapely.make_valid(unary_union(surface))
    polygons = list(union.geoms) if union.geom_type=='MultiPolygon' else [union]
    # Interior road/terrain holes belong to Town01 and stay untouched.
    core = unary_union([Polygon(p.exterior) for p in polygons if p.geom_type=='Polygon'])
    parts = list(core.geoms) if core.geom_type=='MultiPolygon' else [core]
    edges = []
    for p in parts:
        coords = list(p.exterior.coords)
        edges += [LineString([a,b]) for a,b in zip(coords,coords[1:])]
    tree = STRtree(edges)
    field = box(-5000,-5000,5000,5000).difference(core)
    vertices, faces, lookup = [], [], {}
    seam_errors = []
    for x in range(-5000,5000,200):
        for y in range(-5000,5000,200):
            cell = field.intersection(box(x,y,x+200,y+200))
            if cell.is_empty:
                continue
            for tri in shapely.constrained_delaunay_triangles(cell).geoms:
                ids = []
                for px,py,*_ in list(tri.exterior.coords)[:3]:
                    key = (round(px,8),round(py,8))
                    if key not in lookup:
                        point = Point(px,py)
                        edge = edges[tree.nearest(point)]
                        boundary = edge.interpolate(edge.project(point))
                        d = point.distance(boundary)
                        bz = boundary.z
                        # Smooth scenario hills, not geodesy. 140 m total relief ceiling
                        # leaves room for the original Town01 minimum of -60.89 m.
                        hill = 15 + 115*math.exp(-((px-2500)**2+(py-2200)**2)/1800000)
                        hill += 8*math.sin(px/700)*math.sin(py/900)
                        q = min(1., d/600.)
                        weight = q*q*(3-2*q)
                        z = bz*(1-weight)+hill*weight
                        if d<1e-5:
                            seam_errors.append(abs(z-bz))
                        lookup[key]=len(vertices)
                        vertices.append((px,py,z))
                    ids.append(lookup[key])
                a,b,c=(vertices[i] for i in ids)
                if (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])<0:
                    ids[1],ids[2]=ids[2],ids[1]
                faces.append(ids)
    ply=out/'field.ply'
    with ply.open('wb') as f:
        f.write(('ply\nformat binary_little_endian 1.0\nelement vertex %d\nproperty float x\nproperty float y\nproperty float z\nelement face %d\nproperty list uchar int vertex_indices\nend_header\n'%(len(vertices),len(faces))).encode())
        for p in vertices: f.write(struct.pack('<fff',*p))
        for t in faces: f.write(struct.pack('<Biii',3,*t))
    converted=convert_ply_to_obj(ply,out/'field.obj','terrain')
    scene=ET.parse(src/'map/scene.xml')
    for element in scene.findall('.//string[@name="filename"]'):
        value=Path(element.get('value'))
        if not value.is_absolute(): element.set('value',str((src/'map'/value).resolve()))
    material=ET.SubElement(scene.getroot(),'bsdf',type='itu-radio-material',id='mat_customer_field')
    ET.SubElement(material,'string',name='type',value='concrete')
    shape=ET.SubElement(scene.getroot(),'shape',type='ply',id='mesh_customer_field')
    ET.SubElement(shape,'string',name='filename',value=str(ply))
    ET.SubElement(shape,'ref',id='mat_customer_field',name='bsdf')
    scene.write(out/'scene.xml',encoding='unicode')
    world=ET.parse(src/'gazebo/town01.sdf')
    for uri in world.findall('.//mesh/uri'):
        if not Path(uri.text).is_absolute(): uri.text=str((src/'gazebo'/uri.text).resolve())
    model=ET.SubElement(world.find('world'),'model',name='customer_field')
    ET.SubElement(model,'static').text='true'
    link=ET.SubElement(model,'link',name='geometry')
    for tag in ('visual','collision'):
        node=ET.SubElement(link,tag,name='field_'+tag)
        geom=ET.SubElement(node,'geometry'); mesh=ET.SubElement(geom,'mesh')
        ET.SubElement(mesh,'uri').text=str(out/'field.obj')
    world.write(out/'customer.sdf',encoding='unicode')
    area=sum(abs(np.cross(np.subtract(vertices[b][:2],vertices[a][:2]),np.subtract(vertices[c][:2],vertices[a][:2])))/2 for a,b,c in faces)
    summary=dict(source='canonical CAVISE Town01 unchanged plus synthetic field/hills',extent_m=[-5000,5000,-5000,5000],
        external_vertices=len(vertices),external_triangles=len(faces),external_bounds_min_m=converted.bounds_min,
        external_bounds_max_m=converted.bounds_max,external_area_m2=area,expected_external_area_m2=field.area,
        external_area_error_m2=abs(area-field.area),seam_samples=len(seam_errors),seam_interpolation_max_error_m=max(seam_errors,default=None),
        geometry='same float32 PLY vertices and triangles exported to Gazebo OBJ visual and exact mesh collision',
        town01_transform='identity',terrain_provenance='synthetic scenario, not survey',
        building_floors=None,building_floors_reason='Town01 meshes have no storey metadata; no exact floor count inferred',
        material='explicit generic ITU concrete reference for external terrain; not measured soil',
        shapely=shapely.__version__,geos=shapely.geos_version_string)
    (out/'geometry_summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    print(json.dumps(summary))

if __name__=='__main__': main()
