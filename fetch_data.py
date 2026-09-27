#!/usr/bin/env python3
"""Download every input the pipeline needs, driven by config.yaml. Safe to re-run: existing files are skipped.

Whole country (python fetch_data.py):
  1. Borders   geoBoundaries gbOpen ADM0 for `country` + `neighbours`   -> paths.borders_dir
  2. DEM       Copernicus GLO-90 1-degree tiles over the country's bbox  -> paths.tiles_dir
  3. Fill DEM  SRTM 1" (AWS terrain tiles) over the same bbox           -> paths.fill_dir

Region extras (python fetch_data.py --region NAME), on top of the above:
  4. GLO-30 tiles over the region bbox, if the preset sets `dem: glo30`  -> paths.tiles30_dir
  5. Trails from OpenStreetMap (Overpass API), if the preset has `trails` -> paths.trails_dir/NAME.geojson
"""
import argparse
import gzip
import json
import math
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import geopandas as gpd

from pipeline import DEM_SOURCES, load_config

BORDER_URL = ("https://github.com/wmgeolab/geoBoundaries/raw/main/releaseData/gbOpen/"
              "{c}/ADM0/geoBoundaries-{c}-ADM0.geojson")
OVERPASS_URL = "https://overpass-api.de/api/interpreter"


def ns(lat):
    return f"{'N' if lat >= 0 else 'S'}{abs(lat):02d}"


def ew(lon):
    return f"{'E' if lon >= 0 else 'W'}{abs(lon):03d}"


def degree_ranges(lon0, lat0, lon1, lat1):
    return range(math.floor(lat0), math.ceil(lat1)), range(math.floor(lon0), math.ceil(lon1))


def fetch_copernicus(src, tdir, lats, lons):
    """Per-prefix sync (no global bucket listing). Sea-only tiles don't exist: sync copies nothing."""
    d = DEM_SOURCES[src]
    tdir.mkdir(parents=True, exist_ok=True)
    for lat in lats:
        for lon in lons:
            t = f"Copernicus_DSM_COG_{d['code']}_{ns(lat)}_00_{ew(lon)}_00_DEM"
            if (tdir / t / f"{t}.tif").exists():
                continue
            print(f"{src} {t}", flush=True)
            subprocess.run(["aws", "s3", "sync", "--no-sign-request", "--only-show-errors",
                            "--exclude", "AUXFILES/*", f"s3://{d['bucket']}/{t}/", str(tdir / t)], check=True)


def fetch_trails(region, preset, out_file):
    """Hiking ways from OSM: `trails.highway` ways + every member way of route=hiking relations."""
    T = preset["trails"]
    w, s, e, n = preset["bbox"]
    bb = f"{s},{w},{n},{e}"
    highways = "|".join(T.get("highway", ["path", "footway", "bridleway"]))
    query = f"""[out:json][timeout:180];
(
  way["highway"~"^({highways})$"]({bb});
  {'relation["route"="hiking"](' + bb + ');' if T.get("hiking_routes", True) else ''}
);
(._;>;);
out body;"""
    print(f"trails: querying Overpass for {region}", flush=True)
    req = urllib.request.Request(OVERPASS_URL, data=urllib.parse.urlencode({"data": query}).encode(),
                                 headers={"User-Agent": "dem2stl-relief/1.0"})
    with urllib.request.urlopen(req, timeout=300) as r:
        data = json.load(r)

    nodes = {el["id"]: (el["lon"], el["lat"]) for el in data["elements"] if el["type"] == "node"}
    exclude = set(T.get("exclude_highway", ["motorway", "trunk", "primary", "secondary", "tertiary"]))
    feats = []
    for el in data["elements"]:
        if el["type"] != "way":
            continue
        hw = el.get("tags", {}).get("highway")
        if hw in exclude:
            continue
        coords = [nodes[i] for i in el["nodes"] if i in nodes]
        if len(coords) >= 2:
            feats.append({"type": "Feature", "geometry": {"type": "LineString", "coordinates": coords},
                          "properties": {"osm_id": el["id"], "highway": hw,
                                         "name": el.get("tags", {}).get("name")}})
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
    print(f"trails: {len(feats)} ways -> {out_file}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("-r", "--region", help="also fetch region extras (GLO-30, trails) for this preset")
    ap.add_argument("--refresh-trails", action="store_true", help="re-download trails even if present")
    args = ap.parse_args()
    cfg = load_config(args.config, args.region)
    P = cfg["paths"]

    # 1. borders ---------------------------------------------------------------
    bdir = Path(P["borders_dir"])
    bdir.mkdir(parents=True, exist_ok=True)
    for code in [cfg["country"], *cfg["neighbours"]]:
        f = bdir / f"{code}.geojson"
        if f.exists() and f.stat().st_size > 1000:
            continue
        print(f"border {code}", flush=True)
        urllib.request.urlretrieve(BORDER_URL.format(c=code), f)
        if f.stat().st_size < 1000:
            sys.exit(f"{f} is {f.stat().st_size} bytes - a Git LFS pointer, not data")

    lats, lons = degree_ranges(*gpd.read_file(bdir / f"{cfg['country']}.geojson").total_bounds)
    print(f"country tile range: lat {lats.start}..{lats.stop - 1}, lon {lons.start}..{lons.stop - 1}")

    # 2. Copernicus GLO-90 over the country --------------------------------------
    fetch_copernicus("glo90", Path(P["tiles_dir"]), lats, lons)

    # 3. SRTM 1" fill tiles -------------------------------------------------------
    fdir = Path(P["fill_dir"])
    fdir.mkdir(parents=True, exist_ok=True)
    for lat in lats:
        for lon in lons:
            name = f"{ns(lat)}{ew(lon)}"
            hgt = fdir / f"{name}.hgt"
            if hgt.exists():
                continue
            gz = fdir / f"{name}.hgt.gz"
            r = subprocess.run(["aws", "s3", "cp", "--no-sign-request", "--only-show-errors",
                                f"s3://elevation-tiles-prod/skadi/{ns(lat)}/{name}.hgt.gz", str(gz)],
                               capture_output=True)
            if r.returncode != 0:
                continue      # sea-only: no tile
            print(f"fill {name}", flush=True)
            with gzip.open(gz, "rb") as src, open(hgt, "wb") as dst:
                shutil.copyfileobj(src, dst)
            gz.unlink()

    # 4-5. region extras ------------------------------------------------------------
    preset = cfg["_region"]
    if preset:
        if cfg.get("dem", "glo90") == "glo30":
            rl, rn = degree_ranges(*preset["bbox"])
            fetch_copernicus("glo30", Path(P["tiles30_dir"]), rl, rn)
        if preset.get("trails"):
            tf = Path(P["trails_dir"]) / f"{args.region}.geojson"
            if args.refresh_trails or not tf.exists():
                fetch_trails(args.region, preset, tf)

    print("done")


if __name__ == "__main__":
    main()
