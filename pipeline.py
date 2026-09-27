#!/usr/bin/env python3
"""relief-map pipeline, stages 1-3: DEM + borders -> preview heightmap (z in print mm).

Usage:
  python pipeline.py                     # whole country  -> <out_dir>/full/
  python pipeline.py --region attica     # a preset from config.yaml `regions` -> <out_dir>/attica/

Outputs in <out_dir>/<region or "full">/:
  dem.tif           DEM warped onto the print grid (metres)
  class.tif         0 = sea, 1 = neighbour, 2 = country
  z.tif             print height in mm (the heightmap that will be tiled and meshed)
  unclassified.tif  BEFORE reassignment: 1 = DEM land with no polygon (reassigned to nearest
                    polygon's class); 2 = no DEM and no polygon (stays sea)
  report.json       scale, resolution, Z bands, tile occupancy, warnings
  preview.png       hillshaded check map (full resolution) with tile grid
  preview_small.png same, 1/4 size

Preview colours: blue = sea, dark blue = sea with no DEM (verify it's really sea),
grey = neighbour plateau, green->brown->white = country by elevation,
magenta = flat country island that the fill DEM can't verify (no fill coverage) -> investigate.
Flat islands the fill DEM shows real relief on are patched from it; ones it confirms as flat
(sand spits, lagoon islets) render normally. Every flat island is listed in report.json.
White lines/labels = tile grid.
"""
import argparse
import glob
import json
import subprocess
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
import yaml
from rasterio.features import rasterize
from rasterio.transform import from_origin
from PIL import Image, ImageDraw
from scipy import ndimage
from scipy.interpolate import PchipInterpolator
from pyproj import Transformer

NODATA = -9999.0


def run(cmd):
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)


def load_border(borders_dir, code, crs):
    path = Path(borders_dir) / f"{code}.geojson"
    if not path.exists():
        sys.exit(f"missing border file: {path}")
    return gpd.read_file(path).to_crs(crs).geometry.union_all()


def deep_merge(base, override):
    merged = dict(base)
    for k, v in override.items():
        merged[k] = deep_merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return merged


def load_config(path, region=None):
    """config.yaml, with a region preset's overrides merged on top.

    Adds cfg["_region"] (the preset, or None) and cfg["_out"] (output directory)."""
    cfg = yaml.safe_load(open(path))
    preset = None
    if region:
        presets = cfg.get("regions") or {}
        if region not in presets:
            sys.exit(f"unknown region '{region}'. Defined: {', '.join(presets) or 'none'}")
        preset = presets[region]
        if "bbox" not in preset:
            sys.exit(f"region '{region}' needs a bbox: [lon_min, lat_min, lon_max, lat_max]")
        cfg = deep_merge(cfg, {k: v for k, v in preset.items() if k not in ("bbox", "focus")})
    cfg["_region"] = preset
    cfg["_out"] = Path(cfg["paths"]["out_dir"]) / (region or "full")
    return cfg


def write_tif(path, arr, transform, crs, nodata=None):
    profile = dict(driver="GTiff", height=arr.shape[0], width=arr.shape[1], count=1,
                   dtype=arr.dtype, crs=crs, transform=transform, compress="deflate",
                   nodata=nodata)
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(arr, 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("-r", "--region", help="name of a preset under `regions` in the config")
    args = ap.parse_args()
    cfg = load_config(args.config, args.region)
    P, L, PR, R = cfg["paths"], cfg["layout"], cfg["print"], cfg["relief"]
    crs = cfg["crs"]
    region = cfg["_region"]
    out = cfg["_out"]
    out.mkdir(parents=True, exist_ok=True)

    # --- layout -> derived scale ------------------------------------------------
    cols, rows = L["grid"]
    tile, margin, px = L["tile_mm"], L["margin_mm"], L["px_mm"]
    tile_px = tile / px
    if abs(tile_px - round(tile_px)) > 1e-9:
        sys.exit("tile_mm / px_mm must be an integer")
    tile_px = int(round(tile_px))

    country = load_border(P["borders_dir"], cfg["country"], crs)
    neighbours = [load_border(P["borders_dir"], c, crs) for c in cfg["neighbours"]]

    if region:
        to_proj = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
        xmin, ymin, xmax, ymax = to_proj.transform_bounds(*region["bbox"], densify_pts=21)
    else:
        xmin, ymin, xmax, ymax = country.bounds
    frame_w, frame_h = cols * tile, rows * tile                       # mm
    m_per_mm = max((xmax - xmin) / (frame_w - 2 * margin),
                   (ymax - ymin) / (frame_h - 2 * margin))            # ground metres per print mm
    res = px * m_per_mm                                                # ground metres per pixel
    nx, ny = cols * tile_px, rows * tile_px
    cx, cy = (xmin + xmax) / 2, (ymin + ymax) / 2
    x0, y1 = cx - nx * res / 2, cy + ny * res / 2
    x1, y0 = x0 + nx * res, y1 - ny * res
    transform = from_origin(x0, y1, res, res)

    # --- stage 1: DEM onto the print grid ---------------------------------------
    tiles = sorted(glob.glob(str(Path(P["tiles_dir"]) / "*" / "*.tif")))
    if not tiles:
        sys.exit(f"no DEM tiles in {P['tiles_dir']} - run fetch_data.py")
    vrt, dem_path = out / "dem.vrt", out / "dem.tif"
    run(["gdalbuildvrt", "-q", "-overwrite", str(vrt), *tiles])
    run(["gdalwarp", "-q", "-overwrite", "-t_srs", crs,
         "-te", *map(str, (x0, y0, x1, y1)), "-ts", str(nx), str(ny),
         "-r", "average", "-dstnodata", str(NODATA), "-co", "COMPRESS=DEFLATE",
         str(vrt), str(dem_path)])
    with rasterio.open(dem_path) as src:
        dem = src.read(1).astype(np.float32)
    nodata = dem == NODATA

    # --- stage 2: class raster (later shapes overwrite earlier -> country wins) --
    shapes = [(g, 1) for g in neighbours] + [(country, 2)]
    cls = rasterize(shapes, out_shape=(ny, nx), transform=transform, fill=0, dtype="uint8")

    # DEM land outside every polygon (lake holes, border slivers, islets missing from
    # the polygon) -> class of the nearest polygon pixel.
    thr = cfg["checks"]["land_threshold_m"]
    unclassified = ((cls == 0) & ~nodata & (dem > thr)).astype("uint8")
    unclassified[(cls == 0) & nodata] = 2
    write_tif(out / "unclassified.tif", unclassified, transform, crs)
    iy, ix = ndimage.distance_transform_edt(cls == 0, return_distances=False, return_indices=True)
    fix = unclassified == 1
    cls[fix] = cls[iy[fix], ix[fix]]
    del iy, ix
    reassigned = {"to_country": int((fix & (cls == 2)).sum()),
                  "to_neighbour": int((fix & (cls == 1)).sum())}

    # optional focus polygon: only country land inside it stays 3D, the rest becomes plateau
    if region and region.get("focus"):
        focus = gpd.read_file(region["focus"]).to_crs(crs).geometry.union_all()
        inside = rasterize([(focus, 1)], out_shape=(ny, nx), transform=transform, fill=0,
                           dtype="uint8").astype(bool)
        cls[(cls == 2) & ~inside] = 1
    write_tif(out / "class.tif", cls, transform, crs)

    # --- stage 3: height transform ----------------------------------------------
    layer = PR["layer_mm"]
    base = PR["base_mm"]
    plateau = PR["plateau_layers"] * layer
    offset = PR["land_offset_layers"] * layer
    land = cls == 2
    h = np.where(nodata, 0.0, np.clip(dem, 0.0, None)).astype(np.float32)

    # --- flat country islands -> patch from fill DEM where it shows relief --------
    # status per island: 1 = patched, 2 = confirmed flat by fill DEM, 3 = unverified
    labels, n = ndimage.label(land)
    status = np.zeros((ny, nx), np.uint8)
    flat_list = []
    if n:
        ids = np.arange(1, n + 1)
        flat_ids = ids[ndimage.maximum(h, labels, ids) <= thr]
    else:
        flat_ids = np.array([], int)
    if len(flat_ids):
        fill = None
        fill_tiles = sorted(glob.glob(str(Path(P["fill_dir"]) / "*.hgt"))) if P.get("fill_dir") else []
        if fill_tiles:
            fvrt, fill_path = out / "fill.vrt", out / "fill.tif"
            run(["gdalbuildvrt", "-q", "-overwrite", "-srcnodata", "-32768", str(fvrt), *fill_tiles])
            run(["gdalwarp", "-q", "-overwrite", "-t_srs", crs,
                 "-te", *map(str, (x0, y0, x1, y1)), "-ts", str(nx), str(ny),
                 "-r", "average", "-dstnodata", str(NODATA), "-co", "COMPRESS=DEFLATE",
                 str(fvrt), str(fill_path)])
            with rasterio.open(fill_path) as src:
                fill = src.read(1).astype(np.float32)
            fill[fill == NODATA] = np.nan
        else:
            print("WARNING: flat country islands found but no fill tiles in paths.fill_dir - run fetch_data.py")

        flat_mask = np.isin(labels, flat_ids)
        if fill is not None:
            fmax = np.asarray(ndimage.maximum(np.nan_to_num(fill, nan=-1.0), labels, flat_ids))
        else:
            fmax = np.full(len(flat_ids), -1.0)
        codes = np.where(fmax > thr, 1, np.where(fmax >= 0, 2, 3)).astype(np.uint8)
        lut = np.zeros(n + 1, np.uint8)
        lut[flat_ids] = codes
        status = lut[labels]
        patch = status == 1
        if fill is not None and patch.any():
            h[patch] = np.clip(np.nan_to_num(fill[patch], nan=0.0), 0.0, None)

        to_ll = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
        coms = ndimage.center_of_mass(flat_mask, labels, flat_ids)
        areas = np.asarray(ndimage.sum(flat_mask, labels, flat_ids)) * res * res / 1e6
        names = {1: "patched", 2: "confirmed_flat", 3: "unverified"}
        for (r, c), a, fm, code in zip(coms, areas, fmax, codes):
            lon, lat = to_ll.transform(*(transform * (c + 0.5, r + 0.5)))
            flat_list.append({"lon": round(lon, 4), "lat": round(lat, 4), "area_km2": round(float(a), 2),
                              "fill_max_m": None if fm < 0 else round(float(fm), 1),
                              "status": names[int(code)]})
        flat_list.sort(key=lambda d: -d["area_km2"])
    del labels

    if not land.any():
        sys.exit("no country land inside the frame - check the region bbox / focus")
    h_max = float(h[land].max())
    pts = np.array(R["curve"], dtype=np.float64)
    if pts.shape[1] != 2 or np.any(np.diff(pts[:, 0]) <= 0) or np.any(np.diff(pts[:, 1]) < 0):
        sys.exit("relief.curve: [elevation_m, mm] pairs, elevation strictly increasing, mm non-decreasing")
    if pts[-1, 0] < h_max:
        print(f"WARNING: curve ends at {pts[-1, 0]} m but h_max is {h_max:.0f} m; higher ground is clipped")
    curve = PchipInterpolator(pts[:, 0], pts[:, 1])     # smooth and monotone between points
    relief_mm = curve(np.clip(h[land], pts[0, 0], pts[-1, 0])).astype(np.float32)

    z = np.full((ny, nx), base, np.float32)
    z[cls == 1] = base + plateau
    z[land] = base + plateau + offset + relief_mm
    write_tif(out / "z.tif", z, transform, crs)

    # exaggeration per curve segment: print mm / true-scale mm
    exaggeration = {}
    for (e0, m0), (e1, m1) in zip(pts[:-1], pts[1:]):
        true_mm = (e1 - e0) / m_per_mm
        exaggeration[f"{e0:.0f}-{e1:.0f} m"] = round((m1 - m0) / true_mm, 1)

    # --- preview (no GIS app needed) --------------------------------------------
    hs_path = out / "hillshade.tif"
    run(["gdaldem", "hillshade", "-q", "-compute_edges", "-z", str(m_per_mm),
         str(out / "z.tif"), str(hs_path)])
    with rasterio.open(hs_path) as src:
        shade = (0.35 + 0.65 * src.read(1).astype(np.float32) / 255.0)[..., None]

    rgb = np.zeros((ny, nx, 3), np.float32)
    rgb[cls == 0] = (40, 90, 160)
    rgb[(cls == 0) & nodata] = (20, 45, 90)
    rgb[cls == 1] = (175, 175, 175)
    frac = np.zeros((ny, nx), np.float32)
    frac[land] = h[land] / h_max
    stops = [0.0, 0.3, 0.7, 1.0]
    ramp = [(70, 140, 60), (150, 150, 80), (140, 100, 60), (250, 250, 250)]
    for ch in range(3):
        rgb[..., ch][land] = np.interp(frac[land], stops, [c[ch] for c in ramp])
    rgb = rgb * shade
    rgb[status == 3] = (220, 0, 220)

    img = Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8))
    draw = ImageDraw.Draw(img)
    for r in range(rows):
        for c in range(cols):
            x, y = c * tile_px, r * tile_px
            draw.rectangle([x, y, x + tile_px - 1, y + tile_px - 1], outline=(255, 255, 255), width=3)
            draw.text((x + 20, y + 20), f"r{r}c{c}", fill=(255, 255, 255))
    img.save(out / "preview.png")
    img.resize((nx // 4, ny // 4), Image.LANCZOS).save(out / "preview_small.png")

    tiles_report = {"country": [], "neighbour_only": [], "sea_only": []}
    for r in range(rows):
        for c in range(cols):
            t = cls[r * tile_px:(r + 1) * tile_px, c * tile_px:(c + 1) * tile_px]
            key = ("country" if (t == 2).any() else
                   "neighbour_only" if (t == 1).any() else "sea_only")
            tiles_report[key].append(f"r{r}c{c}")

    report = {
        "region": args.region or "full",
        "grid": [cols, rows],
        "frame_mm": [frame_w, frame_h],
        "scale": f"1:{m_per_mm * 1000:,.0f}",
        "ground_m_per_px": round(res, 1),
        "ground_m_per_nozzle_0.4mm": round(0.4 * m_per_mm),
        "pixels": [nx, ny],
        "h_max_m": round(h_max, 1),
        "true_scale_peak_mm": round(h_max / m_per_mm, 2),
        "vertical_exaggeration_by_segment": exaggeration,
        "z_bands_mm": {
            "sea_top": base,
            "plateau_top": round(base + plateau, 3),
            "land_min": round(base + plateau + offset, 3),
            "max": round(float(z.max()), 3),
        },
        "qgis_hillshade_z_factor_for_print_look": round(m_per_mm, 1),
        "tiles": tiles_report,
        "warnings": {
            "frame_px_without_dem": int(nodata.sum()),
            "country_px_without_dem": int((nodata & land).sum()),       # must be 0
            "finer_than_dem": res < 90.0,    # GLO-90 is ~90 m; below that the print can't gain detail
            "sea_px_without_dem": int((nodata & (cls == 0)).sum()),     # can't be verified as sea
            "reassigned_land_px": reassigned,
        },
        "flat_country_islands": {
            "patched": sum(d["status"] == "patched" for d in flat_list),
            "confirmed_flat": sum(d["status"] == "confirmed_flat" for d in flat_list),
            "unverified": sum(d["status"] == "unverified" for d in flat_list),
            "list": flat_list,
        },
    }
    (out / "report.json").write_text(json.dumps(report, indent=2))
    short = json.loads(json.dumps(report))
    short["flat_country_islands"]["list"] = flat_list[:10]
    print(json.dumps(short, indent=2))
    if len(flat_list) > 10:
        print(f"... {len(flat_list) - 10} more flat islands in {out / 'report.json'}")


if __name__ == "__main__":
    main()
