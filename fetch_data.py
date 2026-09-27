#!/usr/bin/env python3
"""Download every input the pipeline needs, driven by config.yaml. Safe to re-run: existing files are skipped.

  1. Borders   geoBoundaries gbOpen ADM0 for `country` + `neighbours`   -> paths.borders_dir
  2. DEM       Copernicus GLO-90 1-degree tiles over the country's bbox  -> paths.tiles_dir
  3. Fill DEM  SRTM 1" (AWS terrain tiles) over the same bbox           -> paths.fill_dir

Usage: python fetch_data.py [-c config.yaml]
"""
import argparse
import gzip
import math
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

import geopandas as gpd
import yaml

BORDER_URL = ("https://github.com/wmgeolab/geoBoundaries/raw/main/releaseData/gbOpen/"
              "{c}/ADM0/geoBoundaries-{c}-ADM0.geojson")


def ns(lat):
    return f"{'N' if lat >= 0 else 'S'}{abs(lat):02d}"


def ew(lon):
    return f"{'E' if lon >= 0 else 'W'}{abs(lon):03d}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--config", default="config.yaml")
    cfg = yaml.safe_load(open(ap.parse_args().config))
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

    lon0, lat0, lon1, lat1 = gpd.read_file(bdir / f"{cfg['country']}.geojson").total_bounds
    lats = range(math.floor(lat0), math.ceil(lat1))
    lons = range(math.floor(lon0), math.ceil(lon1))
    print(f"tile range: lat {lats.start}..{lats.stop - 1}, lon {lons.start}..{lons.stop - 1}")

    # 2. Copernicus GLO-90 (per-prefix sync: no global bucket listing) ------------
    tdir = Path(P["tiles_dir"])
    tdir.mkdir(parents=True, exist_ok=True)
    for lat in lats:
        for lon in lons:
            t = f"Copernicus_DSM_COG_30_{ns(lat)}_00_{ew(lon)}_00_DEM"
            if (tdir / t / f"{t}.tif").exists():
                continue
            print(f"dem {t}", flush=True)
            subprocess.run(["aws", "s3", "sync", "--no-sign-request", "--only-show-errors",
                            "--exclude", "AUXFILES/*", f"s3://copernicus-dem-90m/{t}/", str(tdir / t)],
                           check=True)   # sea-only tiles don't exist: sync just copies nothing

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

    print("done")


if __name__ == "__main__":
    main()
