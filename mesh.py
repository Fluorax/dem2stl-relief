#!/usr/bin/env python3
"""relief-map stages 4-5: out/z.tif -> one STL per tile.

Run pipeline.py first (with the same --region, if any). Reads the same config.yaml.

Usage:
  python mesh.py                    # all tiles of the whole-country run
  python mesh.py --only r3c1 r3c2   # selected tiles
  python mesh.py --region attica    # tiles of a region run

Outputs in <out_dir>/<region or "full">/stl/:
  tile_r<row>c<col>.stl   print-ready tile in mm: bottom at Z=0, sea surface at base_mm,
                          exactly tile_mm x tile_mm, north = +Y
  manifest.json           per-tile triangle count / size / Z range, skipped tiles,
                          and the PrusaSlicer colour-change heights

Neighbouring tiles share their edge row/column of heights, so seams match.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image
from scipy.interpolate import PchipInterpolator

from pipeline import load_config

STL_REC = np.dtype([("normal", "<f4", (3,)), ("v", "<f4", (3, 3)), ("attr", "<u2")])


def run(cmd):
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)


def read_stl(path):
    data = Path(path).read_bytes()
    n = int(np.frombuffer(data, "<u4", count=1, offset=80)[0])
    if len(data) != 84 + 50 * n:
        sys.exit(f"{path}: not a binary STL (size mismatch)")
    return data[:80], np.frombuffer(data, STL_REC, count=n, offset=84).copy()


def write_stl(path, header, tri):
    with open(path, "wb") as f:
        f.write(header)
        f.write(np.uint32(len(tri)).tobytes())
        f.write(tri.tobytes())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("-r", "--region", help="name of a preset under `regions` in the config")
    ap.add_argument("--only", nargs="*", help="mesh only these tiles, e.g. r3c1 r3c2")
    args = ap.parse_args()
    cfg = load_config(args.config, args.region)
    P, L, PR, M, C = cfg["paths"], cfg["layout"], cfg["print"], cfg["mesh"], cfg["colours"]
    out = cfg["_out"]
    stl_dir = out / "stl"
    stl_dir.mkdir(exist_ok=True)

    cols, rows = L["grid"]
    px = L["px_mm"]
    tp = int(round(L["tile_mm"] / px))
    layer, base = PR["layer_mm"], PR["base_mm"]
    first = PR.get("first_layer_mm", layer)
    plateau_top = base + PR["plateau_layers"] * layer
    land_min = plateau_top + PR["land_offset_layers"] * layer

    with rasterio.open(out / "z.tif") as src:
        z = src.read(1).astype(np.float64)
    if z.shape != (rows * tp, cols * tp):
        sys.exit(f"{out / 'z.tif'} is {z.shape}, config expects {(rows * tp, cols * tp)} - re-run pipeline.py")

    zrange = float(z.max() - base)                  # heights encoded relative to the sea surface
    hmm_error = M["max_error_mm"] / zrange          # hmm's error is relative to the full z range
    zp = np.pad(z, ((0, 1), (0, 1)), mode="edge")   # +1 row/col so every tile gets a shared edge

    flip_y = None                                   # detected once from the first tile with relief
    manifest = {"tiles": {}, "skipped_sea_only": []}
    for r in range(rows):
        for c in range(cols):
            name = f"r{r}c{c}"
            if args.only and name not in args.only:
                continue
            t = zp[r * tp:(r + 1) * tp + 1, c * tp:(c + 1) * tp + 1]
            if M["skip_sea_only"] and t.max() <= base + 1e-6:
                manifest["skipped_sea_only"].append(name)
                continue

            png, raw, final = (stl_dir / f"tile_{name}.png", stl_dir / f"tile_{name}.hmm.stl",
                               stl_dir / f"tile_{name}.stl")
            v = np.clip(np.round((t - base) / zrange * 65535), 0, 65535).astype(np.uint16)
            Image.fromarray(v).save(png)
            # hmm units: x/y in pixels; heightmap normalised 0..1; z = value * zscale.
            # -e and -b are in NORMALISED units (fractions of zscale), not pixels.
            run(["hmm", str(png), str(raw), "-z", str(zrange / px), "-e", str(hmm_error),
                 "-b", str(base / zrange)])
            header, tri = read_stl(raw)

            tri["v"] *= px                                   # pixels -> mm
            tri["v"][..., 2] -= tri["v"][..., 2].min()       # bottom at Z = 0

            # hmm's Y direction: detect once by locating the highest point
            if flip_y is None and t.max() > land_min + 1.0:
                ri, ci = np.unravel_index(np.argmax(t), t.shape)
                flat = tri["v"].reshape(-1, 3)
                x, y = flat[np.argmax(flat[:, 2]), :2]
                if abs(x - ci * px) > 2 * px:
                    sys.exit(f"orientation check failed on {name}: x {x:.1f} vs expected {ci * px:.1f}")
                flip_y = abs(y - ri * px) < abs(y - (tp - ri) * px)   # True -> hmm has row 0 at y=0
                print(f"hmm Y orientation detected on {name}: {'flipping' if flip_y else 'as is'}")
            if flip_y:
                tri["v"][..., 1] = tp * px - tri["v"][..., 1]
                tri["normal"][:, 1] *= -1
                tri["v"][:, [1, 2]] = tri["v"][:, [2, 1]]    # keep outward winding after mirroring

            zmin, zmax = float(tri["v"][..., 2].min()), float(tri["v"][..., 2].max())
            if abs(zmax - t.max()) > M["max_error_mm"] + 0.02:
                sys.exit(f"{name}: mesh top {zmax:.3f} mm != heightmap top {t.max():.3f} mm - "
                         "hmm base/scale semantics differ from assumptions")
            write_stl(final, header, tri)
            raw.unlink()
            png.unlink()
            manifest["tiles"][name] = {"triangles": int(len(tri)),
                                       "size_mb": round(final.stat().st_size / 1e6, 1),
                                       "z_mm": [round(zmin, 3), round(zmax, 3)]}
            print(f"{name}: {len(tri):,} triangles, Z {zmin:.2f}-{zmax:.2f} mm")

    # colour changes, as PrusaSlicer layer-top heights (new colour starts on that layer)
    curve_pts = np.array(cfg["relief"]["curve"], dtype=np.float64)
    curve = PchipInterpolator(curve_pts[:, 0], curve_pts[:, 1])
    swaps = [{"layer_top_mm": round(base + layer, 3), "to": "neighbour colour (plateau)"},
             {"layer_top_mm": round(plateau_top + layer, 3), "to": "country base colour"}]
    for e in C.get("bands_m") or []:
        mm = land_min + float(curve(e))
        top = first + np.ceil(round((mm - first) / layer, 6)) * layer
        swaps.append({"layer_top_mm": round(float(top), 3), "to": f"band from {e} m"})
    manifest["colour_changes"] = swaps
    if cfg["relief"].get("local") and C.get("bands_m"):
        manifest["colour_bands_note"] = ("relief.local is on: band heights follow the regional base "
                                         "elevation, so band edges are approximate on hills")
    manifest["slicer_layers_must_be"] = {"first_layer_mm": first, "layer_mm": layer}

    (stl_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps({k: v for k, v in manifest.items() if k != "tiles"}, indent=2))


if __name__ == "__main__":
    main()
