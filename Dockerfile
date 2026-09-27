# syntax=docker/dockerfile:1
# Pinned stable release. (`ubuntu-small-latest` tracks GDAL master, e.g. 3.14.0dev on 2026-09-25.)
# List tags: docker run --rm quay.io/skopeo/stable list-tags docker://ghcr.io/osgeo/gdal
ARG GDAL_TAG=ubuntu-small-3.13.3

# --- Build hmm (heightmap -> adaptive triangle mesh) on the same base for ABI match ---
FROM ghcr.io/osgeo/gdal:${GDAL_TAG} AS hmm-build
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential git ca-certificates libglm-dev \
 && git clone --depth 1 https://github.com/fogleman/hmm /src/hmm \
 && make -C /src/hmm \
 && find /src/hmm -type f -name hmm -perm -u+x -exec cp {} /usr/local/bin/hmm \; \
 && /usr/local/bin/hmm --help >/dev/null 2>&1 || test -x /usr/local/bin/hmm

# --- Runtime: GDAL CLI + Python GIS stack + hmm ---
FROM ghcr.io/osgeo/gdal:${GDAL_TAG}
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3-venv python3-pip \
 && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir numpy scipy rasterio geopandas pyyaml pillow awscli
ENV PATH=/opt/venv/bin:$PATH
COPY --from=hmm-build /usr/local/bin/hmm /usr/local/bin/hmm
WORKDIR /work
