# Transcoding GPU Benchmark

A Docker container (built for Unraid, runs anywhere with Docker) that measures what your
GPU can really do as a media-server transcoder — and lets you compare your results with
the community.

## What it measures

- **Streaming** — how many simultaneous **4K HEVC → 1080p H.264 (8 Mbit)** transcodes your
  GPU sustains at ≥ 1.0× realtime (the strict worst-stream rule). One honest headline number.
- **Conversion** — how fast it re-encodes a library (per-file speed, and the worker count
  that actually finishes a batch fastest — for Tdarr / Unmanic users).
- **CPU baseline** — run the same test on your CPU and see how many times faster and more
  power-efficient your GPU is.
- **Realism options** — HDR → SDR tone-mapping (where the GPU supports it) and subtitle
  burn-in, each measured as its own profile.

Works with **Intel** (VAAPI, iGPU and Arc), **AMD** (VAAPI), **NVIDIA** (NVENC/NVDEC), and
pure **CPU** software encoding. Live telemetry (engine load, power, VRAM), a web scoreboard,
and per-stream speeds while the ramp runs.

## Leaderboard

Comparable runs (the canonical shipped-clip profiles) can be submitted to the community
leaderboard: results are grouped per GPU (and per RAM generation for iGPUs), the headline is
the **median of clean-start runs** — never a single lucky result — and submissions are
validated server-side. IPs are hashed for rate-limiting and never stored; install IDs are
random, not derived from your hardware. The leaderboard server source is in
[`leaderboard/`](leaderboard/).

## Status

**Public release in preparation.** The Docker Hub image, Unraid Community Apps template,
and full documentation are on their way — alongside a pinned, checksummed release of the
benchmark source clips so every submission decodes byte-identical data. Until then, consider
this repository a preview; things may change without notice.

## License

MIT — see [LICENSE](LICENSE).
