# Development log

History, measurements and findings from building the Greece print (2026-09-27). The README describes how to use the project; this file records why it ended up the way it is.

## Environment: Setup fixes and gotchas


| Symptom | Cause / fix |
|---|---|
| `COPY failed: stat src/hmm/bin/hmm: file does not exist` | hmm's Makefile output path. Dockerfile now `find`s the binary and copies it to `/usr/local/bin/hmm` in the build stage |
| `pull access denied for relief-map` | Compose tried to pull the local-only image. Fixed with `pull_policy: never` (harmless anyway) |
| Image rebuilds on every `run` | `pull_policy: build` does that. Use `never` and build explicitly |
| `Sending build context … 241 MB` | Data dirs were sent to the daemon. Fixed by `.dockerignore` |
| `WARN Docker Compose requires buildx plugin` | Legacy builder in use; works. Install `docker-buildx` (or `docker-buildx-plugin`) for BuildKit |
| Don't ^Z a `docker compose run` | Suspending it lost the job and container. Let it finish or Ctrl+C |
| `gdalwarp` warning: *Several coordinate operations are going to be used* | Caused by EPSG:2100's GGRS87 datum. Fixed by using the same projection on WGS84 (see Design decisions) |


## Design decisions as recorded during development

Some values were superseded later (e.g. 280 mm → 220 mm tiles); the README has current values.



| Topic | Decision |
|---|---|
| DEM | Copernicus **GLO-90** (3″ ≈ 90 m). Out-resolves the printer at all planned scales; GLO-30 only needed finer than ~1:300,000 |
| Fill DEM | **SRTM 1″** (AWS public terrain tiles, `skadi`), used **only** on flat country islands where it shows relief > `land_threshold_m`. Copernicus has real gaps (Donousa is 0 in both GLO-90 and GLO-30; SRTM reads 224 m). Vertical datum difference (EGM96 vs EGM2008) < 1 m, negligible |
| Projection | Greek Grid (EPSG:2100) parameters **on the WGS84 datum**: `+proj=tmerc +lat_0=0 +lon_0=24 +k=0.9996 +x_0=500000 +y_0=0 +ellps=WGS84`. Plain EPSG:2100 (GGRS87 datum) made gdalwarp mix several datum transformations, and geopandas could pick a different one → DEM/border misalignment. Same map, no datum shift |
| Borders | geoBoundaries gbOpen ADM0: GRC, TUR, ALB, MKD, BGR, MNE, XKX (Kosovo), SRB |
| Why MNE / XKX / SRB | MNE/XKX dip below 42.0°N (inside the DEM). SRB (south tip ~42.2°N) is outside the DEM but inside the 4×4 frame (top edge ~42.4°N) — frame areas with no DEM and no polygon become sea. Any country inside the **frame** needs a polygon, not just those inside the DEM. Italy, Cyprus, Libya are outside the 4×4 frame |
| Kastellorizo | Included (it's Greece). Costs ~15% width vs. cropping at Rhodes |
| Sea definition | Outside all land polygons (not DEM = 0) |
| Classes | 0 = sea, 1 = neighbour land, 2 = Greece. Rasterize neighbours first, Greece last → Greece wins overlaps |
| Unclassified land | DEM > 5 m outside every polygon → class of the **nearest polygon pixel**. Covers lake holes (Ohrid, Great Prespa are cut out of the polygons), gaps between separately published borders (Evros, MKD–BGR, ALB), and islets missing from the GRC polygon |
| Tiling | **230 mm tiles** (280 mm first; fit was too tight on the SV06 Plus, printed at 70% instead). Grid = `ceil(W/280) × ceil(H/280)`, not necessarily square. Sea-only tiles skipped or printed as flat plates. Neighbouring tiles share edge row/column so seams match |
| Global params | Exaggeration, `h_max`, base, colour Z heights computed **once for the whole country**, never per tile |
| Sea base | 1.5–2 mm (warp resistance at 280 mm; 2–3 layers is too flimsy) |
| Neighbours | Flat plateau 1–2 layers above sea |
| Greek land | `+ land_offset` (**4 layers**, was 1) so plains stand clearly above the sea (≈1 mm step at the coast) and every island clears the plateau |
| Exaggeration | **Custom curve**: list of `[elevation_m, mm]` points, smooth monotone (PCHIP) interpolation. Convex shape = flat lowlands, bumpy hills, very pronounced > 1,000–1,500 m. (Log/γ<1 does the opposite — lifts lowlands, compresses peaks — so it was dropped.) |
| Colour | Z-banded `M600` swaps: blue sea → grey neighbours → hypsometric bands for Greece |
| Config | All parameters in **one config file**; script derives everything else (pixel size, mm values) from the chosen scale |

### Height transform
```
z = base                                              (sea)
z = base + plateau                                    (neighbours)
z = base + plateau + land_offset + curve(h)            (Greece, h clipped to curve range)
```
Starting curve (a first guess to tune from the preview): `0→0, 300→0.6, 1000→4, 1500→10, 3000→28 mm`. At 1:818,568 that is ≈ 1.6× / 4× / 9.8× / 9.8× exaggeration per segment; peak ≈ 26 mm above land base.

### Colour side effect
Swaps are per layer, so Greek land also gets the grey plateau layers between sea top and plateau top. Invisible from above; a thin line on steep coastal cliffs seen from the side.

### Scale reference
Greece bbox ≈ 898 km (with Kastellorizo) × 772 km.

| Grid (280 mm tiles) | Scale | Nozzle width on ground | Approx. size |
|---|---|---|---|
| 3×3 | ~1:1,070,000 | ~430 m | 0.84 m |
| 4×3 | ~1:920,000 | ~370 m | 1.1 × 0.84 m (1 column partly unused) |
| 4×4 | ~1:800,000 | ~320 m | 1.1 m |
| 5×4 | ~1:690,000 | ~275 m | 1.4 × 1.1 m |

Smallest printable feature ≈ 2 extrusion lines ≈ 0.9 mm on the print. Kastellorizo (~6 km) is safe at all of these.

### Open decisions
- **Target grid** (limited by wall space, not data) — configurable via `layout.grid`; scale is derived from it
- Relief curve points
- Colour palette and swap heights
- Minimum island size (dilate vs. drop)
- Tile joining method (backing board / pins / dovetails)


## Data downloads and verification

These manual commands were replaced by `fetch_data.py`; kept for reference and expected results.

### 3.1 DEM tiles
Per-prefix fetch — each 1° tile has its own S3 prefix, so no global bucket listing:
```bash
docker compose run --rm relief bash -c '
for lat in $(seq 34 41); do for lon in $(seq 19 29); do
  t=$(printf "Copernicus_DSM_COG_30_N%02d_00_E%03d_00_DEM" $lat $lon)
  echo "$t"
  aws s3 sync --no-sign-request --exclude "AUXFILES/*" \
    "s3://copernicus-dem-90m/$t/" "tiles/$t/"
done; done'
```
- Sea-only tiles don't exist in the bucket; those prefixes return nothing.
- **Don't** `sync` from the bucket root with `--include` filters: it lists the entire global dataset first and sits silent for minutes.
- If `AUXFILES/` (edit/error/water masks) ever get downloaded, they're not needed: `find tiles -type d -name AUXFILES -exec rm -rf {} +`

Verify:
```bash
docker compose run --rm relief bash -c 'ls tiles | wc -l; gdalbuildvrt /tmp/t.vrt tiles/*/*.tif && gdalinfo /tmp/t.vrt | grep -E "Size is|Origin"'
```
Expected (verified 2026-09-27): **71 tiles**, `Size is 13200, 9600`, `Origin = (19.0, 42.0)` → 19–30°E, 34–42°N.

### 3.2 Borders
```bash
mkdir -p borders
for c in GRC TUR ALB MKD BGR MNE XKX SRB; do
  curl -L -o borders/$c.geojson \
    https://github.com/wmgeolab/geoBoundaries/raw/main/releaseData/gbOpen/$c/ADM0/geoBoundaries-$c-ADM0.geojson
done
```
geoBoundaries uses Git LFS — a ~130-byte file means you got an LFS pointer, not data.

Verify:
```bash
docker compose run --rm relief bash -c 'ls -lh borders; for f in borders/*.geojson; do echo "$f"; ogrinfo -so -al "$f" | grep -E "Feature Count|Extent"; done'
```
Expected (verified 2026-09-27):

| File | Size | Extent (lon/lat) |
|---|---|---|
| ALB | 52K | (19.28, 39.65) – (21.06, 42.66) |
| BGR | 68K | (22.35, 41.23) – (28.61, 44.21) |
| GRC | 1.2M | (19.37, 34.80) – (29.61, 41.75) |
| MKD | 91K | (20.45, 40.85) – (23.03, 42.37) |
| TUR | 6.8M | (25.67, 35.81) – (44.82, 42.10) |
| MNE | 752K | (18.43, 41.85) – (20.35, 43.56) |
| XKX | 517K | (20.01, 41.86) – (21.79, 43.27) |

| SRB | 727K | (18.81, 42.23) – (23.01, 46.19) |

All `Feature Count: 1`, total ~11 MB. MNE and XKX dip below 42.0°N (41.85 / 41.86); SRB's south edge at 42.23°N confirms it sits in the no-DEM band of a 4×4 frame. 

### 3.3 Fill DEM (SRTM 1″)
Only used to patch DEM gaps on flat country islands.
```bash
mkdir -p fill_tiles
docker compose run --rm relief bash -c '
for lat in $(seq 34 41); do for lon in $(seq 19 29); do
  f=$(printf "N%02dE%03d" $lat $lon)
  aws s3 cp --no-sign-request --only-show-errors "s3://elevation-tiles-prod/skadi/N$lat/$f.hgt.gz" fill_tiles/ 2>/dev/null \
    && gunzip -f "fill_tiles/$f.hgt.gz" && echo "$f"
done; done'
```
Sea-only tiles don't exist (the cp fails silently). Unzipped `.hgt` files are ~25 MB each.

How the source was chosen (2026-09-27, point 25.815°E 37.105°N on Donousa):
```bash
docker compose run --rm relief bash -c '
for f in \
  /vsicurl/https://copernicus-dem-30m.s3.amazonaws.com/Copernicus_DSM_COG_10_N37_00_E025_00_DEM/Copernicus_DSM_COG_10_N37_00_E025_00_DEM.tif \
  /vsigzip//vsicurl/https://elevation-tiles-prod.s3.amazonaws.com/skadi/N37/N37E025.hgt.gz ; do
  echo "$f"; gdallocationinfo -valonly -wgs84 "$f" 25.815 37.105
done'
```
GLO-30 → 0, SRTM → 224.


## Pipeline runs

### 4.4 First run results (2026-09-27, 4×4, before CRS/SRB fixes)
- Scale **1:818,572**, 163.7 m/px, 327 m per nozzle width, 5600×5600 px
- `h_max` 2,846.9 m (Olympus is 2,918 m — `-r average` at 164 m smooths the summit; expected)
- True-scale peak 3.48 mm → **5.8×** exaggeration at peak with `peak_mm: 20`
- Z bands: sea top 1.6, plateau top 1.8, land min 2.0, max 22.0 mm
- Tiles: 13 with Greece; r0c3, r1c3 neighbour-only; r3c0 sea-only
- Warnings: 1,462,240 px without DEM (frame past 42°N), 52,641 unclassified land px (to inspect)

### 4.5 Second run (2026-09-27, 4×4, WGS84-datum CRS + SRB)
- Datum warning gone
- Scale 1:818,568, h_max 2,847.1 m, exaggeration 5.8×, Z bands unchanged, tile occupancy unchanged
- `country_px_without_dem` 0 ✔, `frame_px_without_dem` 1,471,951, `sea_px_without_dem` 290,274, `unclassified_land_px` 52,511 (to inspect in preview)

### 4.6 Third run findings (2026-09-27, preview)
Red (unclassified) areas were: Ohrid & Great Prespa lakes (polygon holes), border-gap slivers (Evros, MKD–BGR, ALB–GRC), and islets/coastal bits missing from the GRC polygon. → nearest-polygon reassignment. One Cycladic island rendered flat (no relief) → flat-island check added.

### 4.7 Known DEM gaps
- **Donousa** (≈25.8°E, 37.1°N): flagged magenta by the flat-island check. Verified 2026-09-27 against the raw GLO-90 tile `N37_00_E025`: all values 0 over the island (max 0, mean 0) → the island is **missing from Copernicus GLO-90 itself**, not lost in processing. GLO-30 also 0; SRTM 224 m → patched from SRTM (see Fill DEM).
- **Western Greece** (Lefkada north tip, inside the Amvrakikos Gulf, the Messolonghi coast): small magenta specks. Most likely genuine low ground (sand spits, lagoon barriers, lagoon islets at 0–3 m), not DEM holes — the flat-island check can't tell the two apart by elevation alone. Handled by the flat-island report + patch rule: patched only if SRTM shows relief, otherwise `confirmed_flat`.

### 4.8 Fourth run (2026-09-27, SRTM fill)
- Donousa `patched` (SRTM max 374 m, 12.97 km²) ✔
- 11 `confirmed_flat`, 17 `unverified`. All unverified are ≤ 0.08 km² — smaller than one nozzle-width square at this scale (~0.1 km²) — at ports/deltas/lagoons (Piraeus, Thessaloniki/Axios, Preveza lagoon), where SRTM has no data. Not printable anyway; left as is
- Relief curve: max Z 28.17 mm; exaggeration 1.6× / 4× / 9.8× / 9.8×
- Reassigned land: 25,937 px → country, 26,574 px → neighbour


## Slicing

### 4.10 First slice (2026-09-27)
- `mesh.py` produced 15 tiles (r3c0 skipped), 0.8–42 MB each
- `tile_r2c1` sliced in PrusaSlicer (PETG, 0.2 mm, 10% infill): fine, but only fit comfortably when **scaled to 70%** → ~6h17m, 68 g
- **Don't scale tiles in the slicer**: it scales Z too (base, colour-swap heights, layer alignment). Change `layout.tile_mm` and regenerate instead
- Changes after this: `tile_mm` 280 → 230, `land_offset_layers` 1 → 4


- Then: `tile_mm` 230 → **220** (230 still didn't fit comfortably)

## GDAL pin (2026-09-27)
- Everything above was developed on `ubuntu-small-latest`, which turned out to be a **dev build** (GDAL 3.14.0dev, 2026-09-25)
- Pinned to `ubuntu-small-3.13.3`, the newest stable tag listed by skopeo at the time
- Re-verified `pipeline.py` on 3.13.3 (4×4, 220 mm tiles, land offset 4 layers): no warnings, `country_px_without_dem` 0, same tile occupancy as every earlier run (13 country / r0c3, r1c3 neighbour-only / r3c0 sea-only), Donousa still `patched`
- Results: scale **1:1,047,005**, 209.4 m/px, 419 m per nozzle width, 4400×4400 px; `h_max` 2,817 m (coarser pixels smooth Olympus further); exaggeration 2.1× / 5.1× / 12.6× / 12.6×; Z bands sea 1.6, plateau 1.8, land min 2.6, max 28.4 mm
- Flat islands: 6 patched, 9 confirmed_flat, 12 unverified (all ≤ 0.09 km²); Donousa SRTM max 363 m at this resolution (374 m at 164 m/px)
- `mesh.py` on 3.13.3 ✔: 15 tiles (r3c0 skipped), every height check passed, max Z 28.40 mm (matches `z_bands_mm.max`), hmm error 0.00187 (= 0.05 mm) on all relief tiles, 68 – 585k triangles per tile (0.003 – 24% of naive), each tile < 1 s. Colour changes: plateau at 1.8 mm, country at 2.0 mm
