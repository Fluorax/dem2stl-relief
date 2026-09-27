# AGENTS.md — context for coding agents

Repo: `dem2stl-relief` (GitHub). Turns a country's DEM into tiled, exaggerated-relief, colour-banded 3D-print STLs. First target: Greece. User-facing docs: `README.md`. History, measurements and the reasons behind decisions: `docs/DEVLOG.md`. **Read both before changing behaviour.**

## Working rules

- **Ask, don't assume.** If a parameter, value or intent is unclear, ask the user. Never fill a gap silently, especially in config or docs, which are treated as sources of truth. Label guesses as guesses (e.g. region bboxes are approximate).
- **`config.yaml` is the single source of truth.** Every tunable goes there, once. Scripts derive everything else (scale, pixel size, mm values, colour-swap heights). Never duplicate a setting in code, the README or a second file.
- **Everything runs in Docker.** Don't install anything on the host. `docker compose run --rm relief <cmd>`. Rebuild (`docker compose build`) only when the `Dockerfile` changes.
- **Keep docs current in the same change.** Behaviour, config keys or outputs change → update `README.md`. Run results, findings, bugs and verification → append to `docs/DEVLOG.md` (dated).
- **Verify with the pipeline's own checks**, not by eye alone (see Verification below). If something can't be run (e.g. no data present), say so explicitly.
- Keep answers and summaries succinct.

## Layout

| Path | Role |
|---|---|
| `config.yaml` | All parameters, plus `regions` presets |
| `fetch_data.py` | Downloads borders (geoBoundaries), Copernicus GLO-90 tiles, SRTM 1″ fill tiles. Idempotent |
| `pipeline.py` | Stages 1–3: DEM + borders → `out/<full\|region>/z.tif`, preview PNGs, `report.json`. Exposes `load_config()` |
| `mesh.py` | Stages 4–5: `z.tif` → `stl/tile_r<row>c<col>.stl` + `stl/manifest.json`. Imports `load_config` from `pipeline.py` |
| `Dockerfile` | Stage 1 builds `hmm` from source; stage 2 is `ghcr.io/osgeo/gdal:ubuntu-small-3.13.3` (pinned; `latest` is a GDAL dev build) + venv |
| `docker-compose.yml` | Service `relief`, runs as host UID/GID, mounts repo at `/work`, `pull_policy: never` |
| `tiles/`, `tiles30/`, `fill_tiles/`, `borders/`, `trails/`, `out/` | Data and outputs. Git- and docker-ignored; never commit |

## Commands

```bash
export UID GID
docker compose build
docker compose run --rm relief python fetch_data.py
docker compose run --rm relief python pipeline.py [--region NAME]
docker compose run --rm relief python mesh.py [--region NAME] [--only r0c0 r1c2]
```

## Invariants — don't break these

- **Global parameters.** Heights, `h_max`, the curve, base and colour-swap Z are computed once for the whole frame, never per tile. Otherwise seams won't match.
- **Shared tile edges.** Each tile is `tile_mm/px_mm + 1` samples; neighbours share the edge row/column. The frame is edge-padded by 1 px.
- **Class order.** Rasterize neighbours first and the country last (the country wins overlaps). Then DEM land outside all polygons (> `land_threshold_m`) → class of the nearest polygon pixel (lake holes, border slivers, missing islets). Then the optional region `focus` turns country land outside the polygon into class 1.
- **Sea** = outside all land polygons, not DEM = 0.
- **CRS.** The Greek Grid parameters on the **WGS84 datum** (a PROJ string in the config). Plain EPSG:2100 made gdalwarp and geopandas choose different datum transformations → DEM/border misalignment.
- **Heights:** sea `base`; neighbour `base + plateau`; country `base + plateau + land_offset + max(curve(regional) + local, 0)`. `relief.curve` is PCHIP through `[elevation_m, mm]` points, in print mm. With `relief.local`: *regional* = elevation blurred over `blur_km` using country land only (a normalised convolution, so the sea doesn't drag coasts down); *local* = `cap·tanh(exaggeration × deadband(smooth(h − regional)) / m_per_mm / cap)` (`smooth_km`, `cap_mm`, `valley_factor` optional; `valley_factor` scales negative detail before the cap). Without it: regional = h, local = 0.
- **Tuning.** Judge relief with `preview_layers_small.png` (layer-snapped), not the smooth preview. An absolute-elevation curve alone can't separate flat plateaus from hills — that's why `relief.local` exists.
- **SRTM fill** is applied only to country islands whose Copernicus max ≤ threshold, and only if SRTM shows relief there (`patched`). Otherwise `confirmed_flat` / `unverified`. Never blend SRTM elsewhere.
- **hmm units.** x/y in pixels; heightmap normalised 0..1; `z = value × zscale`. **`-e` and `-b` are fractions of zscale, not pixels.** hmm already puts image row 0 at +Y (north at the back). `mesh.py` auto-detects orientation and hard-fails if the mesh top ≠ the heightmap top. Keep that check.
- **Layer grid.** Layer tops are `first_layer_mm + k × layer_mm` (currently 0.2 + k × 0.12; the printer is calibrated for a 0.2 mm first layer). `base_mm` must sit on that grid (the pipeline checks); plateau and land offsets are whole layers above it.
- **Resampling:** `average` when the print pixel is ≥ the DEM pixel, `cubic` when finer (upsampling), to avoid blocky DEM pixels.
- **DEM source** per region: `dem: glo90 | glo30` (`DEM_SOURCES` in `pipeline.py`; tile code `30` / `10`, buckets `copernicus-dem-90m` / `-30m`). `finer_than_dem` compares against the chosen source.
- **Trails** (region `trails` block): OSM ways from Overpass → `trails/<region>.geojson` → buffered by `width_mm/2`, rasterized on country land only, bench-cut (`mode: cut`, floor = grey-erosion minimum over the width − `depth_layers`, never below `plateau_top + layer`) or embossed (`mode: emboss`, top = grey-dilation maximum + `depth_layers`) (the country colour starts there). Trails are ODbL, so credit OSM on shared prints.
- **Colour swaps** are layer-top heights: plateau at `base + layer`, country at `plateau_top + layer`, plus `colours.bands_m` via the curve, snapped up to the grid.
- Users must **not scale STLs in the slicer** (it breaks Z). Change `layout.tile_mm` and regenerate.

## Verification (after any pipeline/mesh change)

`report.json` must show:
- `country_px_without_dem` = 0.
- Tile occupancy matching the preview.
- Donousa (≈25.81°E, 37.11°N) `patched`.
- No gdalwarp datum warning.

Preview legend: magenta means an unverified flat island; land-shaped dark blue or red in `unclassified.tif` means a country is missing from `neighbours`.

`mesh.py` must finish with no height-check failure, and each tile's Z max must match its heightmap.

Last verified baseline (GDAL 3.13.3, 4×4 grid, 220 mm tiles, `land_offset_layers: 4`):
- **pipeline.py:** scale 1:1,047,005, `h_max` 2,817 m, max Z 28.40 mm. 13 country tiles, r0c3/r1c3 neighbour-only, r3c0 sea-only. Flat islands: 6 patched / 9 confirmed_flat / 12 unverified.
- **mesh.py:** 15 tiles.

## Current settings & context

- Printer: Sovol SV06 Plus (Klipper; needs an `M600` macro), PrusaSlicer, PETG, 0.2 mm layers. 220 mm tiles chosen because larger ones didn't fit comfortably.
- Regions: `peloponnese`, `attica` (approximate bboxes, 1×1 grids). A new region = a `regions` preset (bbox, grid matching its aspect, optional `focus` GeoJSON, optional overrides).
- Git: this repo uses a **repo-local** identity (`git config --local user.*`); the user also has a Gitea account globally. Don't change global git config.

## Roadmap (open; confirm details with the user before implementing)

- Minimum island size rule (drop or enlarge sub-printable islands)
- Colour palette → `colours.bands_m`
- Tile joining (backing board, pins or dovetails)
- Hiking presets: `xerovouni` (run, good), `konitsa` (run, good; Aoos-gorge frame)
- README: exact Copernicus attribution wording still needs to be taken from the official licence page
