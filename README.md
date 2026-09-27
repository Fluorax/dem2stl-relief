# relief-map

Turn a country's elevation data into **tiled, exaggerated-relief 3D-printable STLs** with layer-based colour changes. Only the chosen country is in relief; neighbouring countries print as a flat plateau and the sea as a thin base.

First target: **Greece**, a stress test with high mountains and thousands of islands. You can also print a single region, such as the Peloponnese or Attica, instead of the whole map.

- Copernicus GLO-90 elevation, with automatic SRTM patching of islands missing from Copernicus
- A custom exaggeration curve that keeps plains flat and makes mountains pronounced
- Any tile grid, with the print scale derived automatically; tiles share edges so the seams match
- Colour-change layer heights for single-extruder printers (`M600`)
- Everything runs in Docker, and every parameter lives in one config file

> **Status:** Greece whole-map pipeline verified end to end (preview → STL → sliced in PrusaSlicer). Region printing is new and not yet test-printed. Open items are listed under [Roadmap](#roadmap).

---

## Quick start

Requirements: Docker (with Compose), an image viewer, and a slicer. `UID`/`GID` must be exported so output files belong to you.

```bash
export UID GID
docker compose build                                   # GDAL + Python + hmm (heightmap mesher)
docker compose run --rm relief python fetch_data.py    # borders, DEM, fill DEM (~2 GB for Greece)
docker compose run --rm relief python pipeline.py      # heightmap + preview   -> out/full/
docker compose run --rm relief python mesh.py          # one STL per tile      -> out/full/stl/
```

Check `out/full/preview_small.png` before meshing (see [Checking the preview](#checking-the-preview)).

### Printing a region

```bash
docker compose run --rm relief python pipeline.py --region attica
docker compose run --rm relief python mesh.py --region attica      # -> out/attica/stl/
```

Regions are presets in `config.yaml` under `regions`. Each needs a lon/lat `bbox`, and the frame is fitted to that box instead of the whole country. A preset can override any other setting (for example `layout.grid: [1, 1]` for a single tile, or its own `relief.curve`).

By default, all country land inside the box prints in relief. To print *only* an area, such as the Peloponnese without the mainland across the gulf, add `focus: path/to/polygon.geojson`. Country land outside that polygon then prints flat like a neighbour. You can draw a polygon at [geojson.io](https://geojson.io). The bundled bboxes are approximate, so check the preview.

#### Adding a region

1. **Get a bounding box.** Draw it at [bboxfinder.com](http://bboxfinder.com) (or geojson.io) and copy it as `lon_min, lat_min, lon_max, lat_max`.
2. **Add a preset** under `regions` in `config.yaml`:
   ```yaml
   regions:
     crete:
       bbox: [23.45, 34.75, 26.40, 35.75]
       layout:
         grid: [2, 1]          # Crete is ~3x wider than tall -> 2 tiles side by side
       # focus: regions/crete.geojson   # optional: only land inside this polygon in relief
       # relief: {curve: [...]}         # optional: region-specific exaggeration
   ```
3. **Match the grid to the box's shape.** The frame is fitted to the bbox, so a wide box on a `[1, 1]` grid wastes most of the tile. Aim for `cols/rows ≈ box width/height`.
4. **Run it:** `pipeline.py --region crete`, check `out/crete/preview_small.png` and the report's `scale` and `finer_than_dem`, then run `mesh.py --region crete`.

A focus polygon is a GeoJSON file you keep in the repo (for example under `regions/`). It can be rough over the sea, because only country land inside it matters. The region must lie within the downloaded data, i.e. inside the country's extent.

### Hiking maps

A small area (one mountain, ~20 km) can work as a topo map to study before a hike. See the `xerovouni` preset:
```bash
docker compose run --rm relief python fetch_data.py --region xerovouni   # GLO-30 tiles + OSM trails
docker compose run --rm relief sh -c "python pipeline.py --region xerovouni && python mesh.py --region xerovouni"
```
- **`dem: glo30`** switches the region to Copernicus GLO-30 (30 m). At ~1:100,000 a pixel is ~20 m, so GLO-90 would be far too coarse.
- **`trails`** downloads hiking paths from OpenStreetMap (ways tagged `highway` in the list, plus every way in a `route=hiking` relation) and cuts them into the land as benches `width_mm` wide: the groove floor is the lowest surface within the trail width, minus `depth_layers`. On slopes that makes a visible ledge, where a plain groove would vanish into the layer steps. `mode: emboss` raises a rib instead (top = highest surface within the width + `depth_layers`). They show dark red in the preview. Use `--refresh-trails` to re-download.
- Use a **linear curve** with low exaggeration (~1–1.5×): mountains are already tall at this scale.

Small regions mean larger scales. When the report shows `finer_than_dem: true`, the print is finer than GLO-90's ~90 m resolution and can't gain more detail.

---

## Configuration

Everything is in `config.yaml`. The scripts derive everything else, including scale, pixel size and colour-change heights.

| Key | Meaning |
|---|---|
| `country` / `neighbours` | ISO3 codes (geoBoundaries). The country prints in relief; neighbours print flat. Every country inside the **frame** needs to be listed, not just those inside the DEM |
| `crs` | Projected CRS for the print. Greece uses the Greek Grid projection on the WGS84 datum, which avoids datum-shift mismatches between GDAL and geopandas |
| `layout.grid` | `[cols, rows]` of tiles. **Scale is derived** so the country (or region bbox) fits the frame minus `margin_mm` |
| `layout.tile_mm` / `margin_mm` / `px_mm` | Tile edge, minimum frame margin, and print-space pixel size. `tile_mm / px_mm` must be an integer |
| `print.first_layer_mm` / `print.layer_mm` | First-layer and layer height. The slicer must use exactly these values, or colour swaps won't land on layer boundaries. `first_layer_mm` defaults to `layer_mm` |
| `print.base_mm` | Sea thickness. Must sit on the layer grid (`first_layer_mm + k × layer_mm`); the pipeline refuses otherwise and suggests the nearest valid values |
| `print.plateau_layers` | Neighbours' height above the sea, in layers |
| `print.land_offset_layers` | Country land's minimum height above the plateau, in layers. Lifts plains and islands clearly off the sea |
| `relief.curve` | `[elevation_m, mm]` points: height above the land base. Smooth, monotone interpolation. Steeper segments mean more exaggeration there. Values are in print mm, so they stay valid when the grid changes. With `relief.local` on, the curve applies to the regional base elevation |
| `relief.local` | Optional local-relief boost: `blur_km` (what counts as surroundings), `exaggeration` (detail × true scale), `deadband_m` (smaller detail is flattened), `smooth_km` (low-passes the detail so peaks aren't needles), `cap_mm` (soft tanh cap: small hills keep their full boost, big peaks get rounded), `valley_factor` (scales negative detail; below 1 keeps valleys from being clipped flat at the land base). Makes hills stand out wherever they are, and keeps plateaus flat |
| `mesh.max_error_mm` | Maximum vertical deviation when the mesh is simplified |
| `mesh.skip_sea_only` | Don't mesh pure-sea tiles |
| `colours.bands_m` | Elevations where a new land colour starts; converted to layer heights |
| `checks.land_threshold_m` | DEM above this counts as land |
| `dem` | `glo90` (default) or `glo30`; regions can override |
| `regions` | Presets for printing part of the map (see above). Region-only keys: `bbox`, `focus`, `trails` |

### Current Greece settings

The whole map is a 4×4 grid of 220 mm tiles, 0.88 m across at 1:1,047,005 (419 m of ground per 0.4 mm nozzle width). The relief curve is `0→0, 300→0.6, 1000→4, 1500→10, 3000→28 mm`, which keeps plains nearly flat and exaggerates everything above ~1,000 m by roughly 12×. Layers are 0.2 mm first, then 0.12 mm. The sea is 1.64 mm, the plateau 2 layers (0.24 mm) above it, and land starts 7 layers (0.84 mm) above the plateau.

---

## How it works

**`pipeline.py`** turns the DEM and borders into a print heightmap and preview:

1. **Reproject.** Merges the DEM tiles and warps them onto the print grid, averaging down to the print resolution.
2. **Classify.** Every pixel becomes sea, neighbour or country from the border polygons. Land the DEM shows but no polygon covers (lakes cut out of polygons, gaps between separately published borders, islets missing from the polygon) goes to the nearest polygon's class.
3. **Patch DEM gaps.** A country island that is flat in Copernicus is checked against SRTM. If SRTM shows relief, the island's elevations come from SRTM; if SRTM also shows it low, it stays flat (sand spits, lagoon islets). Donousa, for example, is missing from Copernicus entirely.
4. **Heights.** Each class gets its height:
   ```
   sea        z = base
   neighbour  z = base + plateau
   country    z = base + plateau + land_offset + curve(elevation)
   ```
   With `relief.local` on, the country line becomes `… + curve(regional) + exaggeration × (elevation − regional)`, where *regional* is the elevation blurred over `blur_km`. A flat plateau at 300 m and a 300 m hill then print differently: the plateau stays flat and the hill stands out.
5. **Report and preview.** Writes the scale, exaggeration per curve segment, local-relief stats, tile occupancy and checks, plus two hillshaded previews: a smooth one, and one with heights snapped to print layers.

**`mesh.py`** cuts the heightmap into tiles, with neighbouring tiles sharing their edge row so the seams match. Each tile is meshed with [hmm](https://github.com/fogleman/hmm) (adaptive triangulation with a solid base) and converted to millimetres. The script verifies each mesh's height against the heightmap and writes one STL per tile, plus a manifest with the colour-change heights.

### Outputs (`out/<full|region>/`)

| File | Content |
|---|---|
| `preview_small.png`, `preview.png` | Check map (¼ and full resolution) with the tile grid |
| `preview_layers_small.png`, `preview_layers.png` | Same, with heights snapped to `layer_mm`: what the print will actually look like. Tune the relief with this one |
| `report.json` | Scale, resolution, exaggeration, Z bands, tile occupancy, warnings, flat-island list |
| `z.tif` | Print heightmap (mm), the input to `mesh.py` |
| `dem.tif`, `fill.tif`, `class.tif`, `unclassified.tif`, `hillshade.tif` | Intermediates and diagnostics |
| `stl/tile_r<row>c<col>.stl` | Print-ready tiles: bottom at Z = 0, north at +Y |
| `stl/manifest.json` | Per-tile stats, skipped sea-only tiles, colour-change layer heights |

### Checking the preview

| Colour | Meaning |
|---|---|
| Blue | Sea |
| Dark blue | Sea with no DEM coverage. Open sea is normal; a land-shaped patch means a neighbour is missing from `neighbours` |
| Grey | Neighbour plateau |
| Green → brown → white | Country, by elevation, hillshaded at print proportions |
| Magenta | Flat country island that neither DEM can confirm. Investigate if it's large |
| White grid | Tiles, labelled `r<row>c<col>` with row 0 at the north |

In `report.json`, `country_px_without_dem` must be 0. `flat_country_islands` lists every flat island with its coordinates, area and status (`patched`, `confirmed_flat` or `unverified`).

---

## Printing

- **Don't scale the STLs in the slicer.** Scaling also changes Z, which breaks the base thickness and the colour-change heights. Change `layout.tile_mm` and regenerate instead.
- **Colour changes** are listed in `stl/manifest.json` as layer-top heights, where the new colour starts on that layer. With the current settings: plateau colour at 1.76 mm, then country colour at 2.0 mm, plus any `colours.bands_m`. In PrusaSlicer, right-click the layer slider and choose *Add colour change*.
- **Klipper** has no `M600` by default. Add a `gcode_macro M600` that pauses, parks and retracts.
- **Layer heights** in the slicer must equal `print.first_layer_mm` and `print.layer_mm` (see `slicer_layers_must_be` in the manifest).
- Warped corners show up as steps at the tile seams, so bed adhesion matters.

Reference slice: one Greece tile at ~196 mm (a 280 mm tile scaled to 70%), 0.2 mm layers, PETG, 10% infill: ~6h17m, 68 g.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `pull access denied for relief-map` | Harmless: Compose tried to pull a local-only image. `pull_policy: never` is set |
| `WARN Docker Compose requires buildx plugin` | The legacy builder works. Install `docker-buildx` to get BuildKit |
| A border file is ~130 bytes | That's a Git LFS pointer rather than GeoJSON. Delete it and re-run `fetch_data.py` |
| Red or dark-blue land shapes in the preview | Add the missing country code to `neighbours`, then re-run `fetch_data.py` |
| `mesh top … != heightmap top` | Mesh height check failed. Don't print; open an issue |
| Don't ^Z a `docker compose run` | The job and container get lost. Use Ctrl+C |

Development history and measurements are in [docs/DEVLOG.md](docs/DEVLOG.md).

---

## Roadmap

- Minimum island size rule (drop or enlarge islands below printable size)
- Colour palette (`colours.bands_m`)
- Tile joining (backing board, pins or dovetails)

---

## Data sources and attribution

| Data | Source | Licence / attribution |
|---|---|---|
| Elevation | Copernicus DEM GLO-90 (AWS Open Data, `copernicus-dem-90m`) | Free to use with attribution. Verify the exact wording on the Copernicus DEM licence page before publishing prints |
| Elevation patches | SRTM 1″ via AWS Terrain Tiles (`elevation-tiles-prod/skadi`) | NASA SRTM, public domain |
| Borders | geoBoundaries gbOpen ADM0 | CC BY 4.0. Cite geoBoundaries (Runfola et al.) |
| Elevation (hiking regions) | Copernicus DEM GLO-30 (AWS Open Data, `copernicus-dem-30m`) | As GLO-90 |
| Trails | OpenStreetMap via the Overpass API | ODbL: "© OpenStreetMap contributors" on anything shared that contains trails |
| Meshing | [hmm](https://github.com/fogleman/hmm) by Michael Fogleman | Built from source in the image |

## Licence

Code: [MIT](LICENSE).

The licence covers this repository's code only. Generated STLs and prints are shaped by the data licences: credit **Copernicus DEM** and **geoBoundaries** (CC BY 4.0) when sharing them.
