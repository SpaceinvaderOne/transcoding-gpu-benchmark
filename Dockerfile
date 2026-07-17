# Base on the official Jellyfin image purely as a delivery vehicle for the
# validated multi-vendor ffmpeg (jellyfin-ffmpeg) + Intel/AMD/NVIDIA driver stack.
# The Jellyfin media server itself is NEVER started: we override the entrypoint
# so the container boots straight into our benchmark + scoreboard.
FROM jellyfin/jellyfin:latest

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

# Mint the canonical source clips ONCE at build (CPU encoders — no GPU needed) and ship them in
# the image, so a run NEVER waits for generation: the selected clip is hot-copied into the RAM
# disk at start (~1 s) and read from there. Both clips come from the identical mandelbrot master,
# so the only difference between the HEVC and AV1 boards is the codec/decoder under test.
# This layer is placed before the code COPY so editing benchmark.py doesn't re-mint the clips.
# NOTE: for a public leaderboard these should be minted ONCE and pinned as fixed artifacts so the
# bitstream is byte-stable across base-image updates; generating at build risks baseline drift.
# 4K masters at 50 Mbit (heavy 4K load): HEVC + AV1 are 10-bit, H.264 is 8-bit (hw H.264 decode
# is 8-bit only). Plus a 1080p H.264 master at 10 Mbit — a realistic "1080p movie on a server"
# for the modernise-my-old-library scenario (~75 MB). Clip naming: source_<res>_<codec>.mkv.
RUN mkdir -p /app/clips \
    && /usr/lib/jellyfin-ffmpeg/ffmpeg -y -hide_banner -loglevel error \
         -f lavfi -i mandelbrot=size=3840x2160:rate=24 -t 60 -pix_fmt yuv420p10le \
         -c:v libsvtav1 -preset 6 -b:v 50M -g 240 -svtav1-params "tune=0:film-grain=0" \
         /app/clips/source_4k_av1.mkv \
    && /usr/lib/jellyfin-ffmpeg/ffmpeg -y -hide_banner -loglevel error \
         -f lavfi -i mandelbrot=size=3840x2160:rate=24 -t 60 -pix_fmt yuv420p10le \
         -c:v libx265 -preset medium -b:v 50M \
         -x265-params "keyint=240:min-keyint=240:log-level=none" \
         /app/clips/source_4k_hevc.mkv \
    && /usr/lib/jellyfin-ffmpeg/ffmpeg -y -hide_banner -loglevel error \
         -f lavfi -i mandelbrot=size=3840x2160:rate=24 -t 60 -pix_fmt yuv420p \
         -c:v libx264 -preset medium -b:v 50M \
         -x264-params "keyint=240:min-keyint=240" \
         /app/clips/source_4k_h264.mkv \
    && /usr/lib/jellyfin-ffmpeg/ffmpeg -y -hide_banner -loglevel error \
         -f lavfi -i mandelbrot=size=1920x1080:rate=24 -t 60 -pix_fmt yuv420p \
         -c:v libx264 -preset medium -b:v 10M \
         -x264-params "keyint=240:min-keyint=240" \
         /app/clips/source_1080p_h264.mkv \
    && /usr/lib/jellyfin-ffmpeg/ffmpeg -y -hide_banner -loglevel error \
         -f lavfi -i mandelbrot=size=3840x2160:rate=24 -t 60 -pix_fmt yuv420p10le \
         -c:v libx265 -preset medium -b:v 50M \
         -x265-params "keyint=240:min-keyint=240:log-level=none:hdr10=1:repeat-headers=1:colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc:master-display=G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,1):max-cll=1000,400" \
         /app/clips/source_4k_hdr.mkv
# source_4k_hdr.mkv: the HDR10 master for the tone-mapping profile — same mandelbrot bitstream
# recipe as the HEVC master plus BT.2020/PQ signalling + HDR10 mastering metadata, so the ONLY
# extra work vs the HEVC board is the tone-map stage itself.

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
