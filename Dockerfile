# Base on the official Jellyfin image purely as a delivery vehicle for the
# validated multi-vendor ffmpeg (jellyfin-ffmpeg) + Intel/AMD/NVIDIA driver stack.
# The Jellyfin media server itself is NEVER started: we override the entrypoint
# so the container boots straight into our benchmark + scoreboard.
# PINNED BY DIGEST (not :latest): every validated number — capability probes, tone-map
# chains, leaderboard baselines — was measured on this exact ffmpeg/driver stack, and a
# silent base bump would quietly re-baseline the whole community. Bumping the base is a
# deliberate act: update the digest, re-validate on all three vendors, ship as a new version.
FROM jellyfin/jellyfin@sha256:aefb67e6a7ff1debdd154a78a7bbb780fd0c873d8639210a7f6a2016ad2b35db

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
         python3 \
         intel-gpu-tools \
         mesa-va-drivers \
         pciutils \
         dmidecode \
    && rm -rf /var/lib/apt/lists/*
#   python3         - the benchmark + web server
#   intel-gpu-tools - intel_gpu_top (live engine % / proof, Intel)
#   mesa-va-drivers - radeonsi VAAPI driver (AMD GPU support)
#   pciutils        - lspci, for friendly GPU model names in the picker
#   dmidecode       - system RAM speed readout (Intel iGPU context)

# The CANONICAL clips are NOT in the image any more: they are pinned, immutable GitHub Release
# assets (clips-v1), downloaded once on demand, hash-verified against the manifest baked into
# benchmark.py, and cached in /config/clips (appdata). This keeps the image ~1.85 GB smaller,
# makes container updates cheap, and guarantees every submission decodes byte-identical data.
# What IS minted here: four TINY 1-second probe clips used only by the boot-time capability
# probes (decode_supported / tonemap_supported) — probing must work offline, before any
# download. Low bitrate, ~1-4 MB each; the hdr probe keeps the BT.2020/PQ + HDR10 metadata.
RUN mkdir -p /app/probes \
    && /usr/lib/jellyfin-ffmpeg/ffmpeg -y -hide_banner -loglevel error \
         -f lavfi -i mandelbrot=size=3840x2160:rate=24 -t 1 -pix_fmt yuv420p10le \
         -c:v libx265 -preset ultrafast -b:v 8M -x265-params "log-level=none" \
         /app/probes/probe_4k_hevc.mkv \
    && /usr/lib/jellyfin-ffmpeg/ffmpeg -y -hide_banner -loglevel error \
         -f lavfi -i mandelbrot=size=3840x2160:rate=24 -t 1 -pix_fmt yuv420p10le \
         -c:v libsvtav1 -preset 12 -b:v 8M \
         /app/probes/probe_4k_av1.mkv \
    && /usr/lib/jellyfin-ffmpeg/ffmpeg -y -hide_banner -loglevel error \
         -f lavfi -i mandelbrot=size=3840x2160:rate=24 -t 1 -pix_fmt yuv420p \
         -c:v libx264 -preset ultrafast -b:v 8M \
         /app/probes/probe_4k_h264.mkv \
    && /usr/lib/jellyfin-ffmpeg/ffmpeg -y -hide_banner -loglevel error \
         -f lavfi -i mandelbrot=size=3840x2160:rate=24 -t 1 -pix_fmt yuv420p10le \
         -c:v libx265 -preset ultrafast -b:v 8M \
         -x265-params "log-level=none:hdr10=1:repeat-headers=1:colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc:master-display=G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,1):max-cll=1000,400" \
         /app/probes/probe_4k_hdr.mkv \
    && for p in hevc av1 h264 hdr; do \
         /usr/lib/jellyfin-ffmpeg/ffprobe -v error -select_streams v:0 \
           -show_entries stream=codec_name -of csv=p=0 /app/probes/probe_4k_$p.mkv \
           | grep -qE '^(hevc|av1|h264)$' || exit 1; \
       done
# ^ sanity gate: an encoder that exits 0 but emits a truncated/undecodable probe would ship
#   an image whose boot capability probes silently hide options — fail the BUILD instead

WORKDIR /app
COPY benchmark.py scoreboard.html burnin.ass /app/

# Tunables (all overridable at `docker run` time with -e)
ENV FFMPEG_BIN=/usr/lib/jellyfin-ffmpeg/ffmpeg \
    CLIPS_DIR=/app/clips \
    WEB_PORT=8088 \
    HOLD_SECONDS=25 \
    SETTLE_SECONDS=5 \
    PASS_THRESHOLD=1.0 \
    OUTPUT_BITRATE=8M \
    SOURCE_BITRATE=50M \
    SOURCE_DURATION=60 \
    MAX_STREAMS=128 \
    UNRAID_VER_FILE=/unraid-version \
    DMI_TABLE=/dmi/DMI

# NVIDIA: harmless without the runtime; NVIDIA users add `--runtime=nvidia` so these
# expose the GPU(s) for NVENC/NVDEC. Intel/AMD ignore them.
ENV NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,video,utility

EXPOSE 8088

# Jellyfin's base image ships a HEALTHCHECK that probes its web UI on :8096 — which
# we never start — so it would always report "unhealthy". Disable it; we're not Jellyfin.
HEALTHCHECK NONE

# Override Jellyfin's entrypoint entirely — boot directly into the benchmark.
ENTRYPOINT ["python3", "/app/benchmark.py"]
