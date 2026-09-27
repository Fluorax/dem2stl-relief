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

## Relief tuning on Attica (2026-09-27)
- Region `attica` (1×1, 220 mm, ≈1:460,000). The slice looked "off" versus the smooth preview
- `curve [300, 1.6]`: the 0–300 m band got 8 layers → the undulating Athens basin became contour spaghetti
- `curve [300, 0] … [1500, 2.0]`: Attica's max is ~1,413 m (Parnitha) → everything under 2 mm, flat plates with outlines
- A region-specific curve (`[200, 0.2] … [1450, 12]`) was better, but hills still read weak and plains still terraced
- The coastline was suspected, but the preview showed coasts matching the relief (no polygon shelves) — ruled out
- Root cause: 0.2 mm layer quantisation (the smooth preview hides it), plus an absolute-elevation curve that can't tell a flat 300 m plateau from a 300 m hill (Salamina)
- Added: `relief.local` (regional base via blur + boosted local detail + deadband) and `preview_layers*.png` (layer-snapped preview). Synthetic test: flat 300 m plateau → 0 mm detail; 250 m hill on 50 m ground → +3.5 mm at 8×, 1:460k. Not yet run on real data
- First real run of `relief.local` on Attica: a big improvement (plains flat, Salamina/Aegina/Lavreotiki textured), but mountains and hills looked like **pins**. The detail term boosts pixel-scale peaks at 8×, so features become far taller than they are wide
- Added `smooth_km` (low-pass on the detail) and `cap_mm` (soft tanh cap: 1 → 0.98, 2 → 1.85, 4 → 3.05, 8 → 3.86 mm at cap 4). Starting values 0.4 km and 4 mm; not yet run
- Attica report (1:463,433): `smooth_km`/`cap_mm` were **not applied** (missing from the config). The detail term dominated (p1/p99 −4.4 / +7.0 mm, max 14.2 mm; base max 742 m), max Z 18.3 mm. The top-level curve was in use, not the Attica override (0–300 m at 0.9×)
- `clipped_at_land_base_px` 306,772: valley detail pushed below the land base and clipped → flat floors with cones rising out of them. Added `valley_factor`
- The second Attica run still used the old values (`exaggeration` 8, no `smooth_km`/`cap_mm`, `valley_factor` 1.0): the config edits weren't reaching the region. Rewrote `config.yaml` in full. `relief.local` now lives **only in the `attica` preset** (`blur_km` 3, `exaggeration` 5, `deadband_m` 15, `smooth_km` 0.8, `cap_mm` 3, `valley_factor` 0.3), together with an Attica curve for its ~740 m regional base. The whole map is back to the verified curve-only setting
- The tamer settings (`smooth_km` 0.8, `cap_mm` 3) overcorrected: ridges became round blobs, only a few mm tall, printed as wide contour rings. Switched to comparing variants side by side: presets `attica_linear` (plain curve `[1400, 9]` ≈ 3×, no local) and `attica_mid` (`blur_km` 3, `exaggeration` 6, `deadband_m` 10, `smooth_km` 0.3, `cap_mm` 5, `valley_factor` 0.6)
- `attica_linear`: max Z 11.6 mm, 3.0× uniform. `attica_mid`: max Z 9.7 mm, detail p99 3.5 mm (max 4.7), still 248,286 px clipped at the land base → "pimples" on flat floors. The base curve is near 0 at low elevations, so there's no room for valleys
- Added `attica_bal`: lifted low end of the curve (`200 m` → 1.0 mm), `exaggeration` 7, `smooth_km` 0.15, `cap_mm` 8, `valley_factor` 0.4. The target is between the knives (max 18.3) and the pimples (max 9.7)
- The user liked `attica_linear` (granular, true slopes) and `attica_mid` (flattened plains) for different reasons. Reconciled with `attica_hybrid`: a linear curve plus `local.exaggeration` equal to the curve slope (≈2.98× → 3.0), so the output equals linear except that the `deadband_m` (25 m, `blur_km` 2) flattens small undulation. Numerical check: with deadband 0 it matches linear within 0.04 mm (3.0 vs the true 2.979 slope)

## Layer heights (2026-09-27)
- The sliced preview's contour rings are mostly the "Feature type" view drawing a perimeter at every step; the 3D view of `attica_hybrid` looked right
- The printer is calibrated for a 0.2 mm first layer, so it has to stay; the rest go to 0.12 mm. Added `print.first_layer_mm`: layer tops at 0.2 + k × 0.12, `base_mm` validated against the grid, swaps and bands snapped to it, and the layer preview uses the same grid
- To keep the physical heights close: `base_mm` 1.6 → 1.64, `plateau_layers` 1 → 2 (0.24 mm), `land_offset_layers` 4 → 7 (0.84 mm). Swaps: plateau 1.76 mm, country 2.00 mm. Not yet run; the whole-map baseline numbers above were at 0.2 mm layers

## Hiking maps (2026-09-27)
- Goal: single-mountain topo prints to study before a hike. First target Xerovouni, central Evia (Portaris 1,453 m, 2nd highest on Evia after Dirfys 1,743 m; a ~3 km SW–NE ridge, steep north face; trailheads Steni, Seta, Metochi)
- Frame ~20 × 20 km including Dirfys and Steni: at ~1:100,000 a pixel is ~20 m, which fits GLO-30. At 12 km (Xerovouni only, ~1:60,000) GLO-30 would look soft
- Added: `dem: glo30` per region (fetch only the region's tiles), OSM trails via Overpass as grooves (0.8 mm wide, 2 layers deep, country land only, floored at the country-colour layer), a linear curve preset (`[1800, 22]` ≈ 1.2×). Bbox is approximate. Not yet run
- Next: Konitsa / Tymfi
- First Xerovouni run: scale 1:100,264, 20.1 m/px (`finer_than_dem` true vs GLO-30's 30 m), max Z 23.8 mm (Dirfys 1,738 m), 223 trail ways / 104 km / 19,473 groove px. The relief looked much better than any GLO-90 region, but **the trails weren't visible** in the slice: a plain 0.24 mm groove disappears into the layer steps on slopes
- Changed: trails are now bench-cut (floor = minimum surface over the trail width − depth), and upsampling uses `cubic` instead of `average`. Not yet run
- Xerovouni rerun with the bench cut: trails better. The user suggested embossing as an alternative → added `trails.mode: cut | emboss`
- Added `konitsa`: Konitsa (40.05 N, 20.75 E), Trapezitsa (2,024 m, north of the Aoos), the Aoos gorge, Tymfi (Gamila 2,497 m at 39.982 N, 20.815 E; ~20–25 km E–W × 15 km N–S). Bbox [20.66, 39.90, 20.96, 40.12] ≈ 25 km, ~1:125,000, linear ≈ 1.1×. Assumed "the gorge" = Aoos; Vikos (SW of Tymfi) needs the bbox extended south. The Albanian border is a few km west of Konitsa, so part of the frame is neighbour plateau. Not yet run
- `konitsa` run (after fixing a config.yaml that had pipeline.py pasted into it): the user confirmed it looks great with the Aoos-gorge frame

## Small prints (2026-09-27)
- Konitsa took 22.5 h (220 mm, 0.2 + 0.12 mm layers, mostly land, up to ~25 mm tall). Cheap subjects are small islands on smaller tiles, where most of the area is thin sea
- Added `focus_point` / `focus_others` (keep one connected land piece). Preset `salamina`: bbox [23.37, 37.84, 23.60, 38.00] (approximate), point at the island centre (37.933 N, 23.50 E), `tile_mm` 150, curve `[450, 8.7]` ≈ 3× (Mavrovouni 404 m), GLO-30, trails cut. Not yet run
- `fetch_data.py --region salamina`: Overpass (overpass-api.de) returned HTTP 504 (the server was busy). Added retries across public mirrors (overpass-api.de, overpass.kumi.systems, overpass.private.coffee) with 30/60/90 s backoff
- The mirrors were busy as well. Added `fetch_data.py --no-trails`; the pipeline now warns and prints without trails when the file is missing, instead of stopping. Also: sea-only Copernicus tiles are recorded in `<tiles dir>/.missing`, so reruns stop re-checking the ~17 of them
