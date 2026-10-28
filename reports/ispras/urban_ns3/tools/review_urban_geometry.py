#!/usr/bin/env python3
"""Read-only independent AABB scene/route review, not propagation or flight physics."""
import argparse
import hashlib
import itertools
import json
import math
from pathlib import Path
import re
import xml.etree.ElementTree as ET


def intersects(a,b,lo,hi,margin=0):
    interval = [0.,1.]
    for k in range(3):
        lower, upper = lo[k]-margin, hi[k]+margin
        if a[k] == b[k]:
            if a[k] < lower or a[k] > upper:
                return False
        else:
            enters, leaves = sorted(((lower-a[k])/(b[k]-a[k]),(upper-a[k])/(b[k]-a[k])))
            interval = [max(interval[0],enters),min(interval[1],leaves)]
            if interval[0] > interval[1]:
                return False
    return True


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--config",required=True,type=Path)
    p.add_argument("--world",required=True,type=Path)
    p.add_argument("--ns3-attributes",required=True,type=Path)
    p.add_argument("--previous-config",required=True,type=Path)
    p.add_argument("--output",required=True,type=Path)
    a=p.parse_args()
    c=json.loads(a.config.read_text())
    old=json.loads(a.previous_config.read_text())
    buildings=c["buildings"]
    world=ET.parse(a.world).find("world")
    checked,issues=[],[]
    for b in buildings:
        model=world.find(f"model[@name='urban_building_{b['id']}']")
        pose=list(map(float,model.findtext("pose").split()))
        assert pose[3:] == [0.,0.,0.],"axis-aligned scene must not rotate building"
        for tag in ("collision","visual"):
            shape=model.find(f"link/{tag}")
            size=list(map(float,shape.findtext("geometry/box/size").split()))
            assert shape.find("pose") is None,"unexpected shape-local transform"
            lo=[pose[k]-size[k]/2 for k in range(3)]
            hi=[pose[k]+size[k]/2 for k in range(3)]
            ok=max(abs(x-y) for x,y in zip(lo+hi,b["min"]+b["max"]))<1e-9
            checked.append(dict(id=b["id"],shape=tag,observed_min=lo,observed_max=hi,equal=ok))
            if not ok:issues.append(f"Gazebo {tag} mismatch {b['id']}")
    attrs=[]
    for line in a.ns3_attributes.read_text().splitlines():
        match=re.match(r'value /\$ns3::BuildingListPriv/BuildingList/(\d+)/\$ns3::Building/Boundaries "([^"]+)"',line)
        if match:attrs.append((int(match[1]),list(map(float,match[2].split("|")))))
    attrs.sort()
    expected=[[b["min"][0],b["max"][0],b["min"][1],b["max"][1],b["min"][2],b["max"][2]] for b in buildings]
    ns3_equal=[x[1]for x in attrs] == expected
    if not ns3_equal:issues.append("ns3 actual boundaries mismatch")
    route_hits=[]
    for uid,route in c["routes"].items():
        points=[c["spawns"][uid]]+route
        for index,(start,end) in enumerate(zip(points,points[1:])):
            for b in buildings:
                if intersects(start,end,b["min"],b["max"],3):
                    route_hits.append(dict(uav_id=int(uid),segment=index,building_id=b["id"]))
    if route_hits:issues.append("planned route enters inflated building")
    # Representative simultaneous configurations are geometric feasibility
    # checks only; a policy-dependent flight need not reach them simultaneously.
    states={"initial_airborne":0,"north_corridor_waypoint2":2,"final_inspection":4}
    graphs={}
    for name,index in states.items():
        positions={0:c["gcs"],**{int(uid):route[index] for uid,route in c["routes"].items()}}
        edges={}
        for i,j in itertools.combinations(range(6),2):
            blocked=[b["id"] for b in buildings if intersects(positions[i],positions[j],b["min"],b["max"])]
            edges[f"{i}-{j}"]=dict(geometric_los=not blocked,blocker_ids=blocked)
        clear=lambda i,j: edges[f"{min(i,j)}-{max(i,j)}"]["geometric_los"]
        candidates=[i for i in range(1,6) if clear(0,i) and all(clear(i,j)for j in range(1,6)if j!=i)]
        graphs[name]=dict(positions=positions,edges=edges,all_peer_LOS_candidate_ids=candidates,
                         actual_packet_availability="not inferred from geometry")
    changes=[]
    for uid,route in c["routes"].items():
        for index,(before,after) in enumerate(zip(old["routes"][uid],route)):
            if before!=after:changes.append(dict(uav_id=int(uid),waypoint=index,before=before,after=after))
    inputs={str(path.resolve()):hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (a.config,a.world,a.ns3_attributes,a.previous_config)}
    out=dict(scope="static route and common-box identity review, not flight/packet prediction",
        inputs_sha256=inputs,review_script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        config_scenario=c["scenario"],world_name=world.get("name"),gazebo_shapes=checked,
        actual_ns3_boundaries=[x[1]for x in attrs],actual_ns3_boundaries_match=ns3_equal,
        planned_route_hits_in_3m_building_envelope=route_hits,representative_geometric_graphs=graphs,
        unchanged_radio_parameters=c["radio"]==old["radio"],unchanged_buildings=c["buildings"]==old["buildings"],
        route_changes=changes,issues=issues,
        limits=["3m-inflated segment checks do not prove vehicle tracking, stopping distance or collision-free actual trajectories.",
            "All-peer LOS feasibility does not prove fresh native peer replies or packet delivery; policy uses measured probes.",
            "Representative positions are not replayed or forced into the simulator.",
            "Prior failed return-route pilot is a separate retained result; north-facade route is a pilot-informed revision before freeze."])
    a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(out,indent=2)+"\n")
    print(json.dumps(dict(issues=issues,radio_unchanged=out["unchanged_radio_parameters"],
        graphs={k:v["all_peer_LOS_candidate_ids"]for k,v in graphs.items()},changes=changes),indent=2))


if __name__=="__main__":main()
