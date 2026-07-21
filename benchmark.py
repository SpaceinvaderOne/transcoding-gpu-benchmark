#!/usr/bin/env python3
"""
Multi-vendor GPU simultaneous-stream transcoding benchmark + live scoreboard.

Tests how many concurrent 4K HEVC -> 1080p H.264 (8 Mbit) real-time transcodes a GPU
can sustain. Works on Intel (VAAPI), AMD (VAAPI), and NVIDIA (NVENC/NVDEC). The GPU is
chosen live in the web UI.

Model (B): each ffmpeg runs flat-out (no -re) with -stream_loop -1, racing the hardware.
Per-stream speed is computed instantaneously from -progress out_time_us deltas (NOT
ffmpeg's cumulative speed= field). The ramp adds one stream per level; the answer is the
highest N where the WORST stream still sustains >= PASS_THRESHOLD x realtime over the
hold window. Methodology is identical across vendors so the numbers are comparable.

The only external-binary dependency is jellyfin-ffmpeg (+ optional lspci/dmidecode/
nvidia-smi/intel_gpu_top for info & proof). Everything else lives here.
"""
import os
import re
import sys
import glob
import json
import time
import shutil
import signal
import shlex
import threading
import subprocess
import hashlib
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------- config
def _env(k, d):
    return os.environ.get(k, d)

FFMPEG          = _env("FFMPEG_BIN", "/usr/lib/jellyfin-ffmpeg/ffmpeg")
FFPROBE         = _env("FFPROBE_BIN", FFMPEG.rsplit("ffmpeg", 1)[0] + "ffprobe")
RAMDISK         = _env("RAMDISK", "/ramdisk")
SOURCE          = os.path.join(RAMDISK, "source.mkv")
CLIPS_DIR       = _env("CLIPS_DIR", "/app/clips")   # transition-era images ship clips here
PROBES_DIR      = _env("PROBES_DIR", "/app/probes") # tiny 1s clips for capability probing only
# Canonical clips are PINNED GitHub Release assets (immutable tag clips-v1) — downloaded once,
# hash-verified against this baked-in manifest, cached in appdata. URL is HARDCODED by design:
# changing clips means shipping a new image with a new manifest, never a config edit.
CLIPS_BASE_URL  = "https://github.com/SpaceinvaderOne/transcoding-gpu-benchmark/releases/download/clips-v1/"
CLIP_MANIFEST   = {   # name -> (sha256, exact bytes) — extracted from the validated image
    "source_4k_hevc.mkv":  ("13ff9e46afac887744c508fac0bf343281ebf1168e8ff9017ab7532be9f5a27a", 455504333),
    "source_4k_av1.mkv":   ("8e2da2352791d4f3c066c29ebfe92b0bd657ec898233be635d43e099aee728f6", 409161502),
    "source_4k_h264.mkv":  ("9c44eef58045ceaf1e768a9f6736eb3119e67aae7f3fadde25de19ae58d920e1", 442156462),
    "source_4k_hdr.mkv":   ("41a36e640fa40609bcbab0ce0f42a1fba58c1ef3808606f816da6ec57cbd4bce", 455506587),
    "source_1080p_h264.mkv": ("6394d675568c48fc502adacb98bf59abebe8bfd4ebdb064d6751e9ead636f237", 84393066),
}
INPUT_CODECS    = ("h264", "hevc", "av1")           # selectable source codecs (must hw-decode)
BURNIN_ASS      = _env("BURNIN_ASS", "/app/burnin.ass")  # shipped deterministic burn-in subtitles
INPUT_DIR       = _env("INPUT_DIR", "/input")       # optional BYO-clip drop folder (read-only)
MAX_CUSTOM_FILES = int(_env("MAX_CUSTOM_FILES", "25"))  # >this in /input ⇒ "looks like a library"
HOLD_SECONDS    = float(_env("HOLD_SECONDS", "25"))
SETTLE_SECONDS  = float(_env("SETTLE_SECONDS", "5"))
PASS_THRESHOLD  = float(_env("PASS_THRESHOLD", "1.0"))
OUTPUT_BITRATE  = _env("OUTPUT_BITRATE", "8M")
SOURCE_BITRATE  = _env("SOURCE_BITRATE", "50M")
SOURCE_DURATION = _env("SOURCE_DURATION", "60")
SOURCE_SIZE     = _env("SOURCE_SIZE", "3840x2160")
SOURCE_FPS      = _env("SOURCE_FPS", "24")
MAX_STREAMS     = int(_env("MAX_STREAMS", "128"))
CONV_MAX_STREAMS = int(_env("CONV_MAX_STREAMS", "8"))   # conversion ramp stops at throughput plateau
WEB_PORT        = int(_env("WEB_PORT", "8088"))
STATE_FILE      = _env("STATE_FILE", "/tmp/state.json")
CONFIG_DIR      = _env("CONFIG_DIR", "/config")   # Unraid appdata mount (CPU baseline persistence)
POWERCAP_DIR    = _env("POWERCAP_DIR", "/powercap")  # optional RO mount of host
                                                     # /sys/devices/virtual/powercap (RAPL CPU power)
BASELINE_FILE   = os.path.join(CONFIG_DIR, "cpu_baseline.json")
CLIPS_CACHE_DIR = os.path.join(CONFIG_DIR, "clips")   # one-time clip cache (survives updates)
HISTORY_FILE    = os.path.join(CONFIG_DIR, "history.json")   # per-run history (newest last)
HISTORY_CAP     = int(_env("HISTORY_CAP", "200"))
INSTALL_ID_FILE = os.path.join(CONFIG_DIR, "install_id")   # random per-install uuid (submission dedup)
SUBMIT_SCHEMA   = 1                                        # leaderboard submission-contract version
CPU_PRESETS     = {"streaming": _env("CPU_PRESET_STREAM", "veryfast"),   # Plex/Jellyfin live reality
                   "convert":   _env("CPU_PRESET_CONVERT", "medium")}    # Tdarr/Unmanic library reality
CPU_ENCODERS    = {"h264": "libx264", "hevc": "libx265", "av1": "libsvtav1"}
SAMPLE_INTERVAL = float(_env("SAMPLE_INTERVAL", "1.0"))
DMI_TABLE       = _env("DMI_TABLE", "/dmi/DMI")   # raw SMBIOS table (optional RO mount)
UNRAID_VER_FILE = _env("UNRAID_VER_FILE", "/unraid-version")  # optional RO mount
DYNAMIX_CFG     = _env("DYNAMIX_CFG", "/dynamix.cfg")  # optional RO mount of Unraid's
                                                       # dynamix.cfg (temperature unit)
# leaderboard endpoint — hardcoded by policy (changed only by shipping a new image version);
# will move to gpu.spaceinvader.one once the domain's DNS is on Cloudflare. Setting the env var
# to empty/whitespace disables submission entirely (the Submit button never shows).
SUBMIT_URL      = _env("SUBMIT_URL", "https://gpu.spaceinvader.one/api/submit").strip()
TOOL_VERSION    = "1.2"

# SMBIOS Memory Device "Memory Type" enum (subset)
MEM_TYPE = {0x13: "DDR", 0x14: "DDR2", 0x18: "DDR3", 0x1A: "DDR4", 0x1E: "LPDDR",
            0x1F: "LPDDR2", 0x20: "LPDDR3", 0x21: "LPDDR4", 0x22: "DDR5", 0x23: "LPDDR5"}

APP_DIR    = os.path.dirname(os.path.abspath(__file__))
SCOREBOARD = os.path.join(APP_DIR, "scoreboard.html")

# stderr signatures for a stream that died at startup — split by WHICH ceiling it hit.
# SESSION = the driver's concurrent-NVENC-session limit; MEMORY = a VRAM allocation failure
# (measured on the patched 5090: each 4K session holds ~1.4 GB, the card fills at ~96% and the
# next session dies in the CUDA scale filter with "Cannot allocate memory").
SESSION_CAP_PATTERNS = (
    "openencodesessionex",
    "encode session",
    "maximum number",
    "no encode",
)
MEMORY_CAP_PATTERNS = (
    "cannot allocate memory",
    "out of memory",
)
NVENC_CAP_PATTERNS = SESSION_CAP_PATTERNS + MEMORY_CAP_PATTERNS   # legacy: "any known ceiling"

# NVENC session-limit patch detection (keylase/nvidia-patch). The patch NOPs the session-count
# check inside libnvidia-encode.so; we detect it by scanning that library (bind-mounted into the
# container by the NVIDIA runtime) for the per-driver-version stock vs patched byte signatures.
# The signature table ships as data (nvenc_sigs.json, extracted from patch.sh) because you can
# only PATCH a version the table covers — so any patched card is, by construction, detectable as
# long as we ship the current table. Unknown version ⇒ None (fail safe, never a wrong answer).
NVENC_SIGS_FILE = _env("NVENC_SIGS_FILE", os.path.join(APP_DIR, "nvenc_sigs.json"))
NVENC_ENCODE_LIBS = (
    "/usr/lib64/libnvidia-encode.so.",
    "/usr/lib/x86_64-linux-gnu/libnvidia-encode.so.",
    "/usr/lib/libnvidia-encode.so.",
)


def load_nvenc_sigs(path=NVENC_SIGS_FILE):
    """{driver_version: [stock_hex, patched_hex]} or {} if the data file is missing."""
    try:
        with open(path) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def nvenc_lock_state(lib_bytes, driver_version, sigs):
    """Driver NVENC session-cap state from the encode library's bytes:
    'unlocked' (patched — cap removed), 'locked' (stock signature intact), or None (the driver
    version isn't in the signature table, or neither signature is present — can't tell)."""
    sig = sigs.get(driver_version or "")
    if not sig or not lib_bytes:
        return None
    try:
        stock, patched = bytes.fromhex(sig[0]), bytes.fromhex(sig[1])
    except (ValueError, IndexError):
        return None
    if patched and patched in lib_bytes:
        return "unlocked"
    if stock and stock in lib_bytes:
        return "locked"
    return None


def detect_nvenc_unlocked(driver_version, sigs=None):
    """True/False/None — is this NVIDIA driver's NVENC session cap patched out? Locates the
    encode library for the running driver version and scans it. None when undeterminable."""
    if not driver_version:
        return None
    sigs = load_nvenc_sigs() if sigs is None else sigs
    if driver_version not in sigs:
        return None
    for base in NVENC_ENCODE_LIBS:
        lib = base + driver_version
        try:
            with open(lib, "rb") as f:
                data = f.read()
        except Exception:
            continue
        state = nvenc_lock_state(data, driver_version, sigs)
        if state is not None:
            return state == "unlocked"
    return None


# ------------------------------------------------- pure helpers (unit tested)
def parse_progress_kv(line):
    """Parse one ffmpeg -progress 'key=value' line. Returns (key, value) or None."""
    line = line.strip()
    if not line or "=" not in line:
        return None
    k, v = line.split("=", 1)
    return k.strip(), v.strip()


def compute_inst_speed(prev_out_us, prev_wall, cur_out_us, cur_wall):
    """Instantaneous speed = (delta output seconds) / (delta wall seconds).

    out values are ffmpeg out_time_us (microseconds); wall is monotonic seconds.
    Returns 0.0 if no wall time elapsed (avoids div-by-zero on the first sample).
    """
    dw = cur_wall - prev_wall
    if dw <= 0:
        return 0.0
    return ((cur_out_us - prev_out_us) / 1_000_000.0) / dw


def is_session_cap_error(errtext):
    """True if ffmpeg stderr looks like a hardware/driver encode-session cap."""
    t = (errtext or "").lower()
    return any(p in t for p in NVENC_CAP_PATTERNS)


def death_reason(errtext):
    """Classify a zero-output startup death by its stderr: 'session' (driver session cap),
    'memory' (VRAM allocation failure), or None (unrecognised)."""
    t = (errtext or "").lower()
    if any(p in t for p in SESSION_CAP_PATTERNS):
        return "session"
    if any(p in t for p in MEMORY_CAP_PATTERNS):
        return "memory"
    return None


def vram_slope(samples):
    """Per-session VRAM cost (MB) = MEDIAN of consecutive per-level increments, normalised per
    session. Increments only — the level-1 absolute carries one-time context/pool costs. Median
    (not mean/fit) so a single co-tenant allocation blip can't skew it. Needs >= 2 increments."""
    s = sorted(samples)
    incs = []
    for (na, va), (nb, vb) in zip(s, s[1:]):
        if nb > na and va is not None and vb is not None:
            incs.append((vb - va) / (nb - na))
    if len(incs) < 2:
        return None
    incs.sort()
    m = len(incs) // 2
    return incs[m] if len(incs) % 2 else (incs[m - 1] + incs[m]) / 2


def predict_wall(current_n, free_mb, slope_mb):
    """Predicted total concurrent sessions before VRAM runs out: N + floor(free/slope)."""
    if free_mb is None or not slope_mb or slope_mb <= 0:
        return None
    return current_n + int(free_mb // slope_mb)


def classify_stop(died_zero_output, reason, fail_level, predicted_wall):
    """The four-state limit taxonomy (see the VRAM-instrumentation spec):
    throughput (worst stream slid below threshold), session (driver cap signature),
    memory (alloc signature, or unrecognised death that lands where the VRAM prediction said
    the wall was, +/-1), unknown (hard death nowhere the prediction expected — flag-worthy)."""
    if not died_zero_output:
        return "throughput"
    if reason in ("session", "memory"):
        return reason
    if predicted_wall is not None and abs(fail_level - predicted_wall) <= 1:
        return "memory"
    return "unknown"


def millideg_to_c(raw):
    """Convert a sysfs millidegree value to °C (rounded to 0.1), or None."""
    try:
        return round(int(raw) / 1000.0, 1)
    except (TypeError, ValueError):
        return None


def estimate_igpu_power(loaded_pkg, idle_pkg):
    """iGPU power estimate = rise in CPU-package power under load (>=0). None if missing."""
    if loaded_pkg is None or idle_pkg is None:
        return None
    return round(max(0.0, loaded_pkg - idle_pkg), 1)


def _num(s):
    """First number in a string like '149.7 W' / '55 %', else None."""
    if s is None:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return float(m.group()) if m else None


def parse_nvidia_xml(xml_text):
    """Parse `nvidia-smi -q -x` output for the first GPU. {} on failure.
    Returns util/enc/dec/temp/power/power_max/clock/throttle(bool)/procs(list)."""
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return {}
    g = root.find("gpu")
    if g is None:
        return {}

    def t(path):
        el = g.find(path)
        return el.text if el is not None else None

    throttle = False
    er = g.find("clocks_event_reasons")
    if er is not None:
        for child in er:
            if "Not Active" not in (child.text or "Not Active"):
                throttle = True
    procs = [pi.findtext("process_name") or "?" for pi in g.findall("processes/process_info")]
    return {
        "util": _num(t("utilization/gpu_util")),
        "enc": _num(t("utilization/encoder_util")),
        "dec": _num(t("utilization/decoder_util")),
        "temp": _num(t("temperature/gpu_temp")),
        "power": _num(t("gpu_power_readings/instant_power_draw")
                      or t("gpu_power_readings/average_power_draw")
                      or t("gpu_power_readings/power_draw")
                      or t("power_readings/power_draw")
                      or t("power_readings/instant_power_draw")),
        "power_max": _num(t("gpu_power_readings/current_power_limit")
                          or t("power_readings/power_limit")),
        "clock": _num(t("clocks/sm_clock")),
        "throttle": throttle,
        "procs": [p for p in procs if p and p != "?"],
    }


def parse_fdinfo(text):
    """Parse a /proc/<pid>/fdinfo/<fd> DRM client. {} if not a DRM fd.
    Returns {driver, pdev, video_ns}."""
    kv = {}
    for line in text.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            kv[k.strip()] = v.strip()
    if "drm-driver" not in kv:
        return {}
    m = re.search(r"\d+", kv.get("drm-engine-video", "0"))
    return {"driver": kv["drm-driver"], "pdev": kv.get("drm-pdev"),
            "video_ns": int(m.group()) if m else 0}


def parse_amd_engines(text):
    """Parse VCN media-engine counters from an amdgpu /proc/<pid>/fdinfo/<fd>.
    The encode/decode load lives in drm-engine-enc/-dec (nanosecond accumulators) — the
    GPU-wide gpu_busy_percent is the GFX pipe and stays near-idle during transcode.
    On this RDNA4/radeonsi stack all VCN work (HEVC decode AND H.264 encode) is accounted
    on the single `enc` ring (there is no separate `dec` ring), and the scale_vaapi VPP runs
    on `compute` — so we read enc (=VCN media) and compute (=scaler).
    Returns {enc_ns, dec_ns, comp_ns, enc_cap, dec_cap, pdev, client} or {} if not amdgpu."""
    kv = {}
    for line in text.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            kv[k.strip()] = v.strip()
    if kv.get("drm-driver") != "amdgpu":
        return {}

    def _ns(key):
        m = re.search(r"\d+", kv.get(key, "0"))
        return int(m.group()) if m else 0

    def _cap(key):
        m = re.search(r"\d+", kv.get(key, ""))
        return int(m.group()) if m else 1   # absent capacity line == single instance

    return {"enc_ns": _ns("drm-engine-enc"), "dec_ns": _ns("drm-engine-dec"),
            "comp_ns": _ns("drm-engine-compute"),
            "enc_cap": _cap("drm-engine-capacity-enc"),
            "dec_cap": _cap("drm-engine-capacity-dec"),
            "pdev": kv.get("drm-pdev"), "client": kv.get("drm-client-id")}


def engine_pct(delta_ns, dt_s, cap):
    """Engine occupancy % over a window: busy-ns / (wall-ns * instances), clamped [0,100].
    None when the window is non-positive (can't divide)."""
    if dt_s is None or dt_s <= 0:
        return None
    cap = cap or 1
    pct = 100.0 * delta_ns / (dt_s * 1e9 * cap)
    return round(max(0.0, min(100.0, pct)), 1)


def smbios_mem_speeds(data):
    """Parse a raw SMBIOS structure table (bytes); return (configured_max, rated_max)
    memory speeds in MT/s, or (None, None). Walks Type 17 (Memory Device) records:
    Speed at offset 0x15, Configured Memory Speed at 0x20 (both u16 LE; 0/0xFFFF = unknown)."""
    cfg, rated = [], []
    i, n = 0, len(data)
    while i + 4 <= n:
        stype, length = data[i], data[i + 1]
        if length < 4 or stype == 127:        # bad/end-of-table
            break
        if stype == 17:
            if length > 0x16 and i + 0x17 <= n:
                sp = data[i + 0x15] | (data[i + 0x16] << 8)
                if sp not in (0, 0xFFFF):
                    rated.append(sp)
            if length > 0x21 and i + 0x22 <= n:
                cs = data[i + 0x20] | (data[i + 0x21] << 8)
                if cs not in (0, 0xFFFF):
                    cfg.append(cs)
        # advance: skip formatted area, then the string-set (ends at a double NUL)
        j = i + length
        while j + 1 < n and not (data[j] == 0 and data[j + 1] == 0):
            j += 1
        j += 2
        if j <= i:
            break
        i = j
    return (max(cfg) if cfg else None, max(rated) if rated else None)


def smbios_mem_type(data):
    """Return the memory type string (e.g. 'DDR4', 'DDR5') from SMBIOS Type 17, or None.
    Memory Type is a 1-byte field at offset 0x12 of each Memory Device record."""
    i, n = 0, len(data)
    while i + 4 <= n:
        stype, length = data[i], data[i + 1]
        if length < 4 or stype == 127:
            break
        if stype == 17 and length > 0x12 and i + 0x13 <= n:
            t = MEM_TYPE.get(data[i + 0x12])
            if t:
                return t
        j = i + length
        while j + 1 < n and not (data[j] == 0 and data[j + 1] == 0):
            j += 1
        j += 2
        if j <= i:
            break
        i = j
    return None


# ------------------------------------------------------------- shared state
STATE = {
    "ui": "idle",                 # idle|preparing|running|done|error — which SCREEN to show
    "source_ready": False,        # is a source clip already present (Start is near-instant)?
    "phase": "idle",              # idle|preparing|ramping|settling|holding|done|error (live detail)
    "message": "Ready.",
    "gpus": [],                   # detected GPUs (picker)  [{idx,name,vendor,api,available,note,is_igpu}]
    "selected_idx": None,         # which GPU is chosen
    "selected_name": None,
    "selected_input": "hevc",     # SOURCE codec to decode (h264|hevc|av1) — must be hw-decodable
    "selected_codec": "h264",     # output codec for the run (h264|hevc|av1)
    "selected_source_res": "4k",  # source resolution (4k|1080p) — advanced; default 4K
    "selected_target_res": "1080p",  # output resolution (4k|1080p|720p, <= source) — default 1080p
    "selected_mode": "streaming",  # streaming (how many at once) | convert (how fast)
    "selected_subs": False,       # burn the shipped subtitles in (streaming realism toggle)
    "clips": [],                  # canonical-clip cache states [{name,status,size_mb}]
    "clips_shipped": True,        # image bakes the clips in (transition) ⇒ hide the clips panel
    "clip_dl": None,              # live download progress {name,pct,mb,total_mb}
    "custom_files": [],           # BYO-clip drop folder contents [{name,path,codec,res}]
    "custom_library": False,      # /input looks like a media library (too many files)
    "selected_custom": None,      # chosen custom file NAME (None ⇒ use the shipped clip)
    "vendor": None,               # intel | amd | nvidia (of the chosen GPU)
    "driver": None,               # VAAPI driver (iHD/radeonsi) or NVIDIA driver version
    "kernel_driver": None,        # kernel DRM driver (i915/xe/amdgpu/nvidia)
    "is_igpu": False,             # show CPU + RAM speed only for the Intel iGPU
    "cpu": None,                  # CPU model (iGPU only)
    "ram_speed": None,            # configured RAM speed e.g. "2133 MT/s" (iGPU only)
    "ram_type": None,             # DDR4 | DDR5 (iGPU only)
    "ram_hint": None,             # "below rated 2667 MT/s (XMP/EXPO off?)" (iGPU only)
    "kernel": None,               # host kernel
    "os_version": None,           # Unraid OS version (if mounted)
    "telemetry": {},              # live GPU stats {util,temp,power,clock,...}
    "streams_per_watt": None,     # streams of throughput per watt
    "power_estimated": False,     # True when power is the iGPU package-delta estimate
    "result": None,               # structured result payload (for share/submit)
    "submit_url_set": False,      # is a leaderboard endpoint configured?
    "submitted": False,           # has THIS result been submitted (button becomes a checkmark)?
    "busy_load": None,            # pre-test: GPU engine load if already in use
    "busy_apps": [],              # pre-test: names of apps using the GPU (if knowable)
    "busy_named": False,          # can we name the apps (host-pid visibility / nvidia)?
    "encoder": None,              # VAAPI | NVENC
    "bitrate": OUTPUT_BITRATE,
    "threshold": PASS_THRESHOLD,
    "hold_seconds": HOLD_SECONDS,
    "settle_seconds": SETTLE_SECONDS,  # so the UI can show a per-level ETA
    "history": [],                # past runs of the SAME device+mode+profile (newest first)
    "history_delta": None,        # headline change vs history[0] ({metric,prev,cur,pct})
    "batch": False,               # test-all-devices run in progress / just finished
    "batch_queue": [],            # device names queued for the batch
    "batch_done": 0,              # completed batch runs so far
    "batch_results": [],          # per-device summary rows for the comparison table
    "batch_skipped": [],          # devices left out of the batch: [{gpu, reason}]
    "stream_count": 0,
    "streams": [],                # [{id, speed, fps, pass}]
    "min_speed": None,            # worst per-stream inst speed (headline gauge)
    "avg_speed": None,
    "combined_speed": None,       # sum, informational only
    "last_passing": 0,
    "confirmed_max": None,
    "single_stream_speed": None,  # 1-stream speed (informational)
    "conv_levels": [],            # convert mode: [{n, combined}] per COMPLETED level (live curve)
    "conv_testing_n": None,       # convert mode: worker count currently being measured, or None
    "vram_note": None,            # live dGPU VRAM slope + predicted wall ("each stream ~1.4 GB…")
    "projected": None,            # live running estimate = peak combined throughput so far
    "cap_reason": None,           # "session" when an encode-session cap stopped the ramp
    "projected_uncapped": None,   # estimated streams if the session cap were lifted
    "gpu": None,                  # {engine_name: busy%}  best-effort (Intel)
    "ts": 0.0,
}
STATE_LOCK = threading.Lock()


def publish(**kw):
    """Update shared state and atomically write state.json."""
    with STATE_LOCK:
        STATE.update(kw)
        STATE["ts"] = time.time()
        data = json.dumps(STATE)
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            f.write(data)
        os.replace(tmp, STATE_FILE)
    except Exception:
        pass


# --------------------------------------------------------- system/GPU detection
def cpu_model():
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return None


def _dmidecode_speeds(text):
    cfg, rated, mtype = [], [], None
    for line in text.splitlines():
        line = line.strip()
        m = re.match(r"Configured Memory Speed:\s*(\d+)\s*MT/s", line)
        if m:
            cfg.append(int(m.group(1)))
            continue
        m = re.match(r"Speed:\s*(\d+)\s*MT/s", line)
        if m:
            rated.append(int(m.group(1)))
            continue
        m = re.match(r"Type:\s*(DDR\d|LPDDR\d?)", line)
        if m and not mtype:
            mtype = m.group(1)
    return (max(cfg) if cfg else None, max(rated) if rated else None, mtype)


def ram_info():
    """(configured_mts, rated_mts, type) best-effort. Tries dmidecode (works where the
    host exposes /sys/firmware/dmi/tables), then a raw SMBIOS table mounted at DMI_TABLE."""
    try:
        r = subprocess.run(["dmidecode", "-t", "memory"],
                           capture_output=True, text=True, timeout=6)
        if r.returncode == 0:
            cfg, rated, mtype = _dmidecode_speeds(r.stdout)
            if cfg:
                return (cfg, rated, mtype)
    except Exception:
        pass
    try:
        with open(DMI_TABLE, "rb") as f:
            data = f.read()
        cfg, rated = smbios_mem_speeds(data)
        return (cfg, rated, smbios_mem_type(data))
    except Exception:
        return (None, None, None)


def kernel_version():
    try:
        return os.uname().release
    except Exception:
        return None


def parse_os_version(txt):
    """Pull the OS version out of an /etc/unraid-version-style file. Handles Unraid's numeric
    version="7.3.2" AND non-numeric ones from other OSes the container runs on — e.g. MOS
    reports version="MOS 0.5.0". The old regex anchored the value to a leading digit, so a
    non-numeric version fell through and dumped the whole raw line (version="MOS 0.5.0" instead
    of MOS 0.5.0). Returns the value inside version="...", or a bare version=..., capped to a
    sane length; None when there's nothing usable."""
    t = txt or ""
    m = re.search(r'version="([^"]+)"', t) or re.search(r'version=([^\s"]+)', t)
    return ((m.group(1) if m else t).strip())[:60] or None


def unraid_version():
    """Read the OS version from an optional RO-mounted version file (Unraid's
    /etc/unraid-version, or another OS's equivalent like MOS)."""
    try:
        with open(UNRAID_VER_FILE) as f:
            return parse_os_version(f.read())
    except Exception:
        return None


def parse_display_unit(text):
    """Temperature unit ("C"/"F") from Unraid's dynamix.cfg ([display] unit="F").
    Anything missing or unrecognised falls back to Celsius — this only sets the UI
    default; all stored/submitted temperatures stay Celsius regardless."""
    m = re.search(r'^unit="([CF])"', text or "", re.M)
    return m.group(1) if m else "C"


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return None


def _f(s):
    """Parse a float, tolerating None / 'N/A' / '[N/A]'."""
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def read_cpu_package_temp():
    """CPU-package temperature in °C (the iGPU shares this die). Intel: x86_pkg_temp thermal
    zone / coretemp Package sensor; AMD: k10temp (Tctl). None if unreadable."""
    for zone in glob.glob("/sys/class/thermal/thermal_zone*"):
        if (_read(zone + "/type") or "") == "x86_pkg_temp":
            t = millideg_to_c(_read(zone + "/temp"))
            if t is not None:
                return t
    for hw in glob.glob("/sys/class/hwmon/hwmon*"):
        if (_read(hw + "/name") or "") != "coretemp":
            continue
        for lbl in glob.glob(hw + "/temp*_label"):
            if "Package" in (_read(lbl) or ""):
                t = millideg_to_c(_read(lbl.replace("_label", "_input")))
                if t is not None:
                    return t
    for hw in glob.glob("/sys/class/hwmon/hwmon*"):      # AMD CPUs: k10temp, Tctl preferred
        if (_read(hw + "/name") or "") != "k10temp":
            continue
        for lbl in glob.glob(hw + "/temp*_label"):
            if (_read(lbl) or "") in ("Tctl", "Tdie"):
                t = millideg_to_c(_read(lbl.replace("_label", "_input")))
                if t is not None:
                    return t
        t = millideg_to_c(_read(hw + "/temp1_input"))
        if t is not None:
            return t
    return None


def _pci_name(pci_addr):
    """Friendly device name via lspci -mm; None if lspci/addr unavailable."""
    if not pci_addr:
        return None
    try:
        out = subprocess.run(["lspci", "-mm", "-s", pci_addr],
                             capture_output=True, text=True, timeout=5).stdout
        parts = shlex.split(out)
        if len(parts) >= 4:
            return parts[3]          # the device string
    except Exception:
        pass
    return None


def _nvidia_smi_gpus():
    """List NVIDIA GPUs visible to the container (runtime present). [] otherwise."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,name,driver_version", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=8)
        if out.returncode != 0:
            return []
        gpus = []
        for line in out.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = [p.strip() for p in line.split(",")]
            idx, name = int(parts[0]), parts[1]
            drv = parts[2] if len(parts) > 2 else None
            gpus.append({"index": idx, "name": name, "driver": drv})
        return gpus
    except Exception:
        return []


ENCODERS = {
    "vaapi": {"h264": "h264_vaapi", "hevc": "hevc_vaapi", "av1": "av1_vaapi"},
    "nvenc": {"h264": "h264_nvenc", "hevc": "hevc_nvenc", "av1": "av1_nvenc"},
}


def enc_name(api, codec):
    return ENCODERS[api][codec]


def codec_supported(gpu, codec, ten_bit=False):
    """Quick 1-frame probe: can this GPU's encoder actually do this codec? With ten_bit, probes
    10-bit (p010/main10) encode — hardware H.264 is 8-bit only so H.264 10-bit always fails."""
    if ten_bit and codec == "h264":
        return False                         # no hardware 10-bit H.264 encode exists
    enc = enc_name(gpu["api"], codec)
    fmt = "p010le" if ten_bit else "nv12"
    prof = ["-profile:v", "main10"] if (ten_bit and codec == "hevc") else []
    # 1280x720 (not tiny) — AV1 encoders reject sub-minimum resolutions ("no capable devices")
    common = [FFMPEG, "-hide_banner", "-loglevel", "error",
              "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=1", "-frames:v", "1"]
    if gpu["api"] == "vaapi":
        cmd = (common[:1] + ["-vaapi_device", gpu["device"]] + common[1:]
               + ["-vf", f"format={fmt},hwupload", "-c:v", enc] + prof + ["-f", "null", "-"])
    else:
        dev = ["-gpu", str(gpu["index"])] if gpu.get("index") is not None else []
        cmd = common + ["-vf", f"format={fmt}", "-c:v", enc] + prof + dev + ["-f", "null", "-"]
    try:
        r = subprocess.run(cmd, env=stream_env(gpu),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        return r.returncode == 0
    except Exception:
        return False


# ---- resolution / bit-depth matrix -----------------------------------------
RES_DIMS   = {"4k": (3840, 2160), "1080p": (1920, 1080), "720p": (1280, 720)}
RES_LABEL  = {"4k": "4K", "1080p": "1080p", "720p": "720p"}
SOURCE_RES = ("4k", "1080p")             # shippable source resolutions
TARGET_RES = ("4k", "1080p", "720p")     # selectable output resolutions
# which source codecs ship at each source resolution (4K = full set + the HDR10 master; 1080p =
# the "modernise my old H.264 library" case only, keeping the image small). "hdr" is a pseudo
# codec: the HDR10 HEVC clip, offered only to devices whose tone-map probe passes.
SOURCE_CODECS_BY_RES = {"4k": ("h264", "hevc", "av1", "hdr"), "1080p": ("h264",)}


def target_res_options(source_res):
    """Output resolutions no larger than the source (never upscale)."""
    sh = RES_DIMS[source_res][1]
    return [r for r in TARGET_RES if RES_DIMS[r][1] <= sh]


def source_is_10bit(source_codec):
    """The shipped 10-bit masters are HEVC/AV1; H.264 clips are 8-bit."""
    return source_codec in ("hevc", "av1")


def ten_bit_output(source_res, target_res, source_codec, out_codec):
    """Preserve 10-bit only when KEEPING resolution (archival intent) from a 10-bit source to an
    HEVC/AV1 encoder — hardware H.264 encode is 8-bit only, and a downscale accepts a lighter
    version so it goes 8-bit."""
    return (source_is_10bit(source_codec) and target_res == source_res
            and out_codec in ("hevc", "av1"))


def is_comparable(source_res, target_res):
    """Only the canonical 4K -> 1080p resolution profile is leaderboard-comparable; every other
    resolution pair is a local-only run (like a custom file)."""
    return source_res == "4k" and target_res == "1080p"


SATURATION_GAIN = 0.05     # <5% more combined throughput ⇒ the media engine has plateaued


def throughput_saturated(combined, best, thresh=SATURATION_GAIN):
    """True once adding a stream no longer meaningfully raises COMBINED throughput (< thresh gain
    over the best seen) — the conversion ramp's stop rule (there is NO ≥realtime rule for batch)."""
    return best > 0 and combined <= best * (1 + thresh)


def recommended_workers(levels, peak, cap=None):
    """Best concurrent-worker count for batch tools (Tdarr/Unmanic): the worker count with the
    HIGHEST measured combined throughput — the absolute fastest way to drain the library. On a GPU
    whose one media engine is already saturated (e.g. the UHD 770) adding a stream LOWERS combined
    throughput, so this correctly lands on 1. Clamped to a driver session cap if one was hit.
    `levels` = [{n, combined}, ...]."""
    if not levels or not peak:
        return 1
    # fastest = max combined; ties break to the FEWER workers (sort by n ascending, keep first max)
    n = max(sorted(levels, key=lambda x: x["n"]), key=lambda L: L["combined"])["n"]
    return min(n, cap) if cap else n


# ---------------------------------------------- CPU baseline & efficiency comparison (pure logic)
def cpu_baseline_key(mode, profile):
    """The per-(mode, profile) key a CPU baseline is stored/looked-up under."""
    return f"{mode}|{profile}"


def baseline_valid(entry, cpu_model, tool_version):
    """A stored CPU baseline is comparable only if it was measured on the SAME CPU and a
    tool version with the same MAJOR (a major bump may change the clip/methodology → stale)."""
    if not entry:
        return False
    if entry.get("cpu_model") != cpu_model:
        return False
    def major(v):
        return str(v or "").split(".")[0]
    return major(entry.get("tool_version")) == major(tool_version)


def efficiency_ratio(cpu_wps, gpu_wps):
    """How many times more power-efficient the GPU is: CPU watts/stream ÷ GPU watts/stream."""
    if not cpu_wps or not gpu_wps:
        return None
    return round(cpu_wps / gpu_wps, 1)


def speed_ratio(gpu_speed, cpu_speed):
    """How many times faster the GPU is (streams sustained, or ×realtime per file)."""
    if not gpu_speed or not cpu_speed:
        return None
    return round(gpu_speed / cpu_speed, 1)


def compute_vs_cpu(mode, gpu, cpu, is_dgpu):
    """Assemble the 'vs CPU' comparison payload from a GPU result and a CPU baseline entry.
    `gpu`/`cpu` carry single_stream, max_sustained, watts_per_stream, peak_power_w (+ cpu preset/
    encoder). Speed metric is streams (streaming) or per-file × (conversion). All ratios guard
    against zero (CPU may sustain 0 streams / have no power reading)."""
    streaming = mode == "streaming"
    gpu_speed = gpu.get("max_sustained") if streaming else gpu.get("single_stream")
    cpu_speed = cpu.get("max_sustained") if streaming else cpu.get("single_stream")
    return {
        "efficiency": efficiency_ratio(cpu.get("watts_per_stream"), gpu.get("watts_per_stream")),
        "speed": speed_ratio(gpu_speed, cpu_speed),
        "speed_kind": "streams" if streaming else "perfile",
        "gpu_speed": gpu_speed, "cpu_speed": cpu_speed,
        "cpu_could_sustain": (cpu.get("max_sustained") or 0) >= 1 if streaming else True,
        # watts_per_stream = watts ÷ combined ×realtime = Wh per HOUR OF VIDEO already —
        # dividing by speed again double-counts (the ratio would wrongly become eff × speed)
        "energy_gpu": gpu.get("watts_per_stream"),
        "energy_cpu": cpu.get("watts_per_stream"),
        "watts_gpu": gpu.get("peak_power_w"), "watts_cpu": cpu.get("peak_power_w"),
        "cpu_preset": cpu.get("preset"), "cpu_encoder": cpu.get("encoder"),
        "dgpu_caveat": bool(is_dgpu),
    }


def rapl_delta_uj(prev_uj, cur_uj, max_range_uj):
    """Energy consumed (µJ) between two reads of a RAPL counter that wraps at
    max_energy_range_uj. None when inputs are missing or it wrapped with an unknown range."""
    if prev_uj is None or cur_uj is None:
        return None
    if cur_uj >= prev_uj:
        return cur_uj - prev_uj
    if not max_range_uj:
        return None
    return cur_uj - prev_uj + max_range_uj


def rapl_watts(delta_uj, dt_s):
    """µJ over a window → watts (1 W = 1e6 µJ/s)."""
    if delta_uj is None or not dt_s or dt_s <= 0:
        return None
    return round(delta_uj / dt_s / 1e6, 1)


def rapl_package_paths(root=POWERCAP_DIR):
    """Top-level RAPL PACKAGE domains under a mounted powercap tree. Matches
    <root>/intel-rapl/intel-rapl:<n> whose `name` starts with 'package' — the single-colon glob
    naturally skips nested core/uncore subdomains (intel-rapl:0:0) and the intel-rapl-mmio
    duplicates (which would double-count the same silicon). [] when unmounted/absent."""
    paths = []
    for d in sorted(glob.glob(os.path.join(root, "intel-rapl", "intel-rapl:[0-9]*"))
                    + glob.glob(os.path.join(root, "intel-rapl:[0-9]*"))):
        base = os.path.basename(d)
        if ":" in base.replace("intel-rapl:", "", 1):     # intel-rapl:0:0 → nested subdomain
            continue
        if (_read(os.path.join(d, "name")) or "").startswith("package"):
            paths.append(d)
    return paths


def default_selection(avail):
    """Which device to pre-select at boot / bare-/start. The CPU device is ALWAYS present, so
    'exactly one available device' would never match on a GPU box — prefer the single hardware
    GPU when there is exactly one; a CPU-only box pre-selects the CPU. None ⇒ user must pick."""
    hw = [g for g in avail if g.get("vendor") != "cpu"]
    if len(hw) == 1:
        return hw[0]
    if not hw and len(avail) == 1:
        return avail[0]
    return None


def cpu_load_pct(load1, ncpu):
    """Normalised CPU busy%: 1-min loadavg ÷ logical cores × 100, clamped [0,100].
    None when inputs are missing (can't tell)."""
    if load1 is None or not ncpu:
        return None
    return round(max(0.0, min(100.0, load1 / ncpu * 100.0)), 1)


def parse_proc_stat(text):
    """(busy, total) jiffies from the aggregate `cpu ` line of /proc/stat, or None.
    Host-wide even inside a container (the kernel is shared) — the same figure the
    Unraid dashboard shows. Idle time = idle + iowait."""
    for line in (text or "").splitlines():
        if line.startswith("cpu "):
            try:
                vals = [int(x) for x in line.split()[1:]]
            except ValueError:
                return None
            if len(vals) < 5:
                return None
            return sum(vals) - vals[3] - vals[4], sum(vals)
    return None


def cpu_stat_pct(prev, cur):
    """Instantaneous CPU busy% between two parse_proc_stat samples ([0,100] or None).
    loadavg is a 1-minute average — far too sluggish for the 1 Hz live tile."""
    if not prev or not cur:
        return None
    dtotal = cur[1] - prev[1]
    if dtotal <= 0:
        return None
    return round(max(0.0, min(100.0, (cur[0] - prev[0]) / dtotal * 100.0)), 1)


def load_baseline(path=BASELINE_FILE):
    """Read the CPU-baseline map ({key: entry}); {} if missing/unreadable."""
    try:
        with open(path) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_baseline(path, key, entry):
    """Merge one baseline entry into the map and write it back atomically. Returns True on
    success, False if the location isn't writable (e.g. /config not mounted) — never raises."""
    try:
        data = load_baseline(path)
        data[key] = entry
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


# ------------------------------------------------------- run history (persisted, /config)
def load_history(path=HISTORY_FILE):
    """Read the run-history list; [] if missing/unreadable."""
    try:
        with open(path) as f:
            d = json.load(f)
        return d if isinstance(d, list) else []
    except Exception:
        return []


def append_history(path, entry, cap=HISTORY_CAP):
    """Append one run to the history (newest LAST on disk), trimming to `cap`. Atomic write;
    returns False instead of raising when /config isn't writable."""
    try:
        data = load_history(path)
        data.append(entry)
        data = data[-cap:]
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def history_for(entries, gpu_name, mode, profile, limit=5):
    """Past runs of the SAME device+mode+profile, newest first — the only fair comparison set."""
    got = [e for e in entries
           if e.get("gpu") == gpu_name and e.get("mode") == mode and e.get("profile") == profile]
    got.sort(key=lambda e: e.get("ts") or 0, reverse=True)
    return got[:limit]


def run_delta(prev, cur):
    """Headline change vs a previous run of the same profile: streaming compares max_sustained
    ('streams'), convert compares single_stream ('perfile'). pct=None when prev headline is 0."""
    if not prev or not cur:
        return None
    streaming = cur.get("mode") == "streaming"
    key = "max_sustained" if streaming else "single_stream"
    p, c = prev.get(key), cur.get(key)
    if p is None or c is None:
        return None
    pct = round((c / p - 1) * 100) if p else None
    return {"metric": "streams" if streaming else "perfile", "prev": p, "cur": c, "pct": pct}


def batch_eligible(gpu, input_codec, codec):
    """Can this device take part in a test-all batch for the chosen codec pair? (Capability-gated
    exactly like the pickers: must hw-decode the input and encode the output.)"""
    return bool(gpu.get("available")
                and input_codec in gpu.get("decodes", [])
                and codec in gpu.get("codecs", []))


def batch_skip_reason(gpu, input_codec, codec):
    """Why a device sits out a test-all batch (None ⇒ eligible) — shown in the comparison table
    so exclusions are visible instead of devices silently missing."""
    if not gpu.get("available"):
        return "not testable"
    if input_codec not in gpu.get("decodes", []):
        # "hdr" in decodes means the tone-map probe passed, not plain HEVC decode
        return "can't tone-map HDR" if input_codec == "hdr" else f"can't decode {input_codec.upper()}"
    if codec not in gpu.get("codecs", []):
        return f"can't encode {codec.upper()}"
    return None


def build_batch_jobs(devices, kind, output_codec, subs_on, selected_input, sources=None):
    """Expand a batch request into sequential jobs [{gpu, input_codec, subs}] + skipped rows.
    kind "sweep" = every supported 4K source per device at the chosen output (device-grouped,
    shipped order) — the "how does this GPU handle every kind of source?" batch. kind
    "current" = the selected (source, subs) on each device — the classic cross-device compare.
    The subtitles toggle applies to every job EXCEPT hdr (the combined chain is unsupported in
    v1), so a subs sweep shows HDR as visibly skipped rather than silently missing. Parallel
    execution is deliberately not offered: concurrent devices contend for CPU feeding, PCIe and
    RAM bandwidth and destroy power attribution (iGPU+CPU literally share silicon)."""
    jobs, skipped = [], []
    for gpu in devices:
        if not gpu.get("available"):
            skipped.append({"gpu": gpu["name"], "reason": "not testable"})
            continue
        if output_codec not in gpu.get("codecs", []):
            skipped.append({"gpu": gpu["name"], "reason": f"can't encode {output_codec.upper()}"})
            continue
        if kind == "sweep":
            # sources = the panel's multi-selected subset (shipped order preserved); None = all
            want = [c for c in SOURCE_CODECS_BY_RES["4k"]
                    if sources is None or c in sources]
            for src in want:
                if src not in gpu.get("decodes", []):
                    skipped.append({"gpu": gpu["name"],
                                    "reason": batch_skip_reason(gpu, src, output_codec)})
                    continue
                if src == "hdr" and subs_on:
                    skipped.append({"gpu": gpu["name"],
                                    "reason": "HDR can't combine with subtitle burn-in"})
                    continue
                jobs.append({"gpu": gpu, "input_codec": src, "subs": subs_on})
        else:  # current selection across devices
            reason = batch_skip_reason(gpu, selected_input, output_codec)
            if reason:
                skipped.append({"gpu": gpu["name"], "reason": reason})
                continue
            jobs.append({"gpu": gpu, "input_codec": selected_input,
                         "subs": subs_on and selected_input != "hdr"})
    return jobs, skipped


def profile_label(source_res, input_codec, target_res, codec, custom_source, subs, ten_bit):
    """The human/leaderboard profile string, e.g. "4K HEVC -> 1080p H264 + subs". Every distinct
    workload (input codec, HDR, burn-in, resolutions, bit depth) is its own profile so the board
    buckets like-for-like automatically."""
    src = (f"{input_codec.upper()} (your file)" if custom_source
           else f"{RES_LABEL[source_res]} {input_codec.upper()}")
    tgt = f"{RES_LABEL[target_res]} {codec.upper()}" + (" 10-bit" if ten_bit else "")
    return f"{src} -> {tgt}" + (" + subs" if subs else "")


def clip_master(source_res, input_codec):
    """Path to the shipped canonical source clip for a (resolution, codec) pair."""
    return os.path.join(CLIPS_DIR, f"source_{source_res}_{input_codec}.mkv")


def sha256_file(path):
    """Full SHA-256 hex digest of a file (constant memory), or None on error."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def verify_file_hash(path, sha256_hex):
    """SHA-256 the whole file and compare (used at download time; cached clips are trusted by
    exact size afterwards — spinning appdata arrays shouldn't re-read 1.85 GB every boot)."""
    return sha256_file(path) == sha256_hex


def copy_with_sha256(src, dst):
    """Copy src → dst while hashing the bytes in the SAME pass — the clip is read once anyway
    when it's hot-loaded into the ramdisk, so the SHA-256 is effectively free (no extra disk
    read). Returns the hex digest of what was actually written to `dst`. This makes the staged
    hash a fact about the bytes ffmpeg will decode, not about some earlier cache check."""
    h = hashlib.sha256()
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        for chunk in iter(lambda: fi.read(1024 * 1024), b""):
            fo.write(chunk)
            h.update(chunk)
    return h.hexdigest()


def cached_ok(path, size):
    """A cached clip is valid iff it exists at EXACTLY the manifest size (hash was verified
    when it was downloaded; a size mismatch means a torn/corrupt file → re-download)."""
    try:
        return os.path.getsize(path) == size
    except Exception:
        return False


def resolve_clip(name):
    """Where is this canonical clip? → (path, status): shipped (baked into a transition-era
    image), cached (downloaded + verified earlier), or (None, 'missing') = needs download."""
    shipped = os.path.join(CLIPS_DIR, name)
    if os.path.exists(shipped):
        return shipped, "shipped"
    cache = os.path.join(CLIPS_CACHE_DIR, name)
    if name in CLIP_MANIFEST and cached_ok(cache, CLIP_MANIFEST[name][1]):
        return cache, "cached"
    return None, "missing"


def clips_status():
    """Per-clip cache state for the UI. shipped=True ⇒ the whole panel is moot (image has
    everything baked in — transition images)."""
    out = []
    for name, (_sha, size) in CLIP_MANIFEST.items():
        path, status = resolve_clip(name)
        out.append({"name": name, "status": status, "size_mb": round(size / 1e6)})
    return out


_CLIP_DL_LOCK = threading.Lock()   # serialize downloads (fetch-all vs an on-demand run)


def download_clip(name, dest_dir, progress_cb=None, abort_event=None):
    """Stream one pinned release asset → dest_dir/name. SHA-256 is computed WHILE streaming;
    atomic .part → rename only after the hash matches the manifest. Returns the final path.
    Raises RuntimeError on network failure / hash mismatch / abort (the .part never survives).
    Serialized: a fetch-all and an on-demand download of the same file can't collide, and the
    second caller finds the finished file instead of re-downloading."""
    sha, size = CLIP_MANIFEST[name]
    os.makedirs(dest_dir, exist_ok=True)
    part = os.path.join(dest_dir, f".{name}.part")
    final = os.path.join(dest_dir, name)
    with _CLIP_DL_LOCK:
        if cached_ok(final, size):
            return final
        return _download_clip_locked(name, sha, size, part, final, progress_cb, abort_event)


def _download_clip_locked(name, sha, size, part, final, progress_cb, abort_event):
    req = urllib.request.Request(CLIPS_BASE_URL + name,
                                 headers={"User-Agent": f"gpu-benchmark/{TOOL_VERSION}"})
    h = hashlib.sha256()
    done = 0
    try:
        with urllib.request.urlopen(req, timeout=30) as r, open(part, "wb") as f:
            while True:
                if abort_event is not None and abort_event.is_set():
                    raise RuntimeError("download cancelled")
                chunk = r.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if done > size:      # oversized stream — stop NOW, don't fill the disk
                    raise RuntimeError("downloaded clip failed verification (size/hash mismatch)")
                if progress_cb:
                    progress_cb(done, size)
        if done != size or h.hexdigest() != sha:
            raise RuntimeError("downloaded clip failed verification (size/hash mismatch)")
        os.replace(part, final)   # atomic: a valid file appears all-at-once or never
        return final
    except Exception:
        try:
            os.remove(part)
        except Exception:
            pass
        raise


def _publish_clip_dl(name, done, total):
    publish(clip_dl={"name": name, "pct": round(done * 100.0 / total, 1),
                     "mb": round(done / 1e6), "total_mb": round(total / 1e6)},
            message=f"Downloading {name} — one-time, {round(total / 1e6)} MB… "
                    f"{done * 100.0 / total:.0f}%")


_FETCHING = threading.Event()   # a download-all is in flight (one at a time)


def fetch_all_clips():
    """Background download of every missing canonical clip (the '⬇ Download all' button).
    Sequential — the appdata share is usually one spinning array. Publishes per-clip progress;
    a failure reports and keeps going (partial success still saves the user time later)."""
    try:
        failed = []
        for name in CLIP_MANIFEST:
            if resolve_clip(name)[0] is not None:
                continue
            try:
                os.makedirs(CLIPS_CACHE_DIR, exist_ok=True)
                download_clip(name, CLIPS_CACHE_DIR,
                              progress_cb=lambda d, t, n=name: _publish_clip_dl(n, d, t))
                publish(clips=clips_status())
            except Exception as e:
                failed.append(name)
                publish(message=f"Download of {name} failed: {e}")
        publish(clip_dl=None, clips=clips_status(),
                message=("All test clips downloaded — they're kept in appdata, this was a "
                         "one-time download." if not failed else
                         f"Done with errors — {len(failed)} clip(s) failed; try again later."))
    finally:
        _FETCHING.clear()


def start_fetch_clips():
    """POST /fetchclips — idle only, one at a time; no-op when nothing is missing."""
    with RUN_LOCK:
        with STATE_LOCK:
            cur = STATE.get("ui")
        if cur != "idle" or _FETCHING.is_set():
            return False
        if all(resolve_clip(n)[0] is not None for n in CLIP_MANIFEST):
            return False
        _FETCHING.set()
        threading.Thread(target=fetch_all_clips, daemon=True).start()
        return True


def generate_canonical_clip(name, dest):
    """Offline fallback: mint the clip locally with the EXACT canonical recipe (same params the
    clips-v1 assets were minted with). The bitstream is encoder-threading-dependent, so the
    result usually WON'T hash-match the manifest on other machines — the caller hash-checks and
    marks the run non-comparable on mismatch. Slow (minutes): CPU x265/svt encode."""
    recipes = {
        "source_4k_hevc.mkv": ["-f", "lavfi", "-i", "mandelbrot=size=3840x2160:rate=24", "-t", "60",
            "-pix_fmt", "yuv420p10le", "-c:v", "libx265", "-preset", "medium", "-b:v", "50M",
            "-x265-params", "keyint=240:min-keyint=240:log-level=none"],
        "source_4k_av1.mkv": ["-f", "lavfi", "-i", "mandelbrot=size=3840x2160:rate=24", "-t", "60",
            "-pix_fmt", "yuv420p10le", "-c:v", "libsvtav1", "-preset", "6", "-b:v", "50M",
            "-g", "240", "-svtav1-params", "tune=0:film-grain=0"],
        "source_4k_h264.mkv": ["-f", "lavfi", "-i", "mandelbrot=size=3840x2160:rate=24", "-t", "60",
            "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "medium", "-b:v", "50M",
            "-x264-params", "keyint=240:min-keyint=240"],
        "source_4k_hdr.mkv": ["-f", "lavfi", "-i", "mandelbrot=size=3840x2160:rate=24", "-t", "60",
            "-pix_fmt", "yuv420p10le", "-c:v", "libx265", "-preset", "medium", "-b:v", "50M",
            "-x265-params", "keyint=240:min-keyint=240:log-level=none:hdr10=1:repeat-headers=1:"
            "colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc:"
            "master-display=G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,1):"
            "max-cll=1000,400"],
        "source_1080p_h264.mkv": ["-f", "lavfi", "-i", "mandelbrot=size=1920x1080:rate=24", "-t", "60",
            "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "medium", "-b:v", "10M",
            "-x264-params", "keyint=240:min-keyint=240"],
    }
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error"] + recipes[name] + [dest]
    # Popen + poll (not run(timeout=3600)): a blocking run() made Cancel a no-op for up to an
    # hour on the one path where the wait is minutes — the ≤1–2 s cancel rule applies here too
    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 3600
    while p.poll() is None:
        if _ABORT.is_set() or time.monotonic() > deadline:
            p.kill()
            p.wait(timeout=10)
            try:
                os.remove(dest)
            except Exception:
                pass
            raise RuntimeError("cancelled" if _ABORT.is_set()
                               else "local clip generation timed out")
        _ABORT.wait(0.5)
    if p.returncode != 0 or not os.path.exists(dest):
        raise RuntimeError("local clip generation failed")
    return dest


def ensure_clip(source_res, input_codec, abort_event=None):
    """Make the canonical clip for (res, codec) available; returns (path, verified).
    shipped/cached ⇒ verified. Missing ⇒ download (to appdata cache, or straight to the
    ramdisk when /config isn't writable) with live progress; on network failure fall back to
    local generation and hash-check it (match ⇒ still verified/comparable)."""
    name = os.path.basename(clip_master(source_res, input_codec))
    path, status = resolve_clip(name)
    if path:
        return path, True
    sha, size = CLIP_MANIFEST[name]
    # pick a destination: persistent cache if /config is writable, else the ramdisk (one-shot)
    dest_dir, oneshot = CLIPS_CACHE_DIR, False
    try:
        os.makedirs(CLIPS_CACHE_DIR, exist_ok=True)
    except Exception:
        dest_dir, oneshot = RAMDISK, True
    try:
        p = download_clip(name, dest_dir, progress_cb=lambda d, t: _publish_clip_dl(name, d, t),
                          abort_event=abort_event)
        publish(clip_dl=None, clips=clips_status())
        if oneshot:
            publish(message="Clip downloaded to RAM only — map /config (appdata) to keep it.")
        return p, True
    except Exception as e:
        if abort_event is not None and abort_event.is_set():
            raise
        publish(clip_dl=None,
                message=f"Download failed ({e}) — generating the clip locally instead "
                        f"(slower; result will be local-only unless it matches the pinned hash).")
        dest = os.path.join(dest_dir, name)
        generate_canonical_clip(name, dest)
        ok = verify_file_hash(dest, sha)
        publish(clips=clips_status())
        return dest, ok


def is_run_comparable(mode, source_res, target_res, custom_source, is_cpu,
                      threshold, hold, settle, clip_verified):
    """The single leaderboard-eligibility gate: canonical streaming 4K→1080p, strict 1.0×
    rule, standard hold/settle, and a VERIFIED canonical clip (shipped or hash-matched — a
    locally generated variant bitstream is never comparable). CPU software runs joined the
    board 2026-07-18: identical clips + rules + a code-locked veryfast preset make them as
    comparable as GPU runs (the server additionally enforces preset/encoder); is_cpu is kept
    in the signature for call-site clarity and future policy."""
    return (mode == "streaming" and is_comparable(source_res, target_res)
            and not custom_source and threshold == 1.0
            and hold >= 25 and settle >= 5 and clip_verified)


VIDEO_EXTS = (".mkv", ".mp4", ".mov", ".ts", ".m4v", ".avi", ".webm", ".m2ts", ".mpg", ".wmv")


def is_video_file(name):
    """True if a filename looks like a video container (and isn't a hidden/dotfile)."""
    if not name or name.startswith("."):
        return False
    return name.lower().endswith(VIDEO_EXTS)


def fit_seconds(bitrate_bps, budget_bytes, want=60.0):
    """How many seconds of a stream at `bitrate_bps` fit in `budget_bytes` of ramdisk — so a
    high-bitrate custom file's sample never overflows the RAM disk. Unknown bitrate → keep the
    full `want` (the caller size-checks that case). Returns the true fit, which may be well
    below `want` for a very high bitrate; the caller refuses when it's too small to be useful."""
    if not bitrate_bps or bitrate_bps <= 0:
        return float(want)
    return float(min(want, round((budget_bytes * 8.0) / bitrate_bps, 1)))


def sample_window(duration, want=60.0):
    """Pick a representative slice of a custom source file: `want` seconds centred on the
    MIDDLE (movies open with easy-to-decode logos/credits — the middle is the real workload).
    Short files use the whole thing. Returns (start_s, length_s); length None ⇒ whole file."""
    if duration is None or duration <= want + 5:
        return (0.0, None)
    return (round(duration / 2.0 - want / 2.0, 3), want)


def probe_clip(codec):
    """The clip capability probes decode: a tiny 1 s probe file (shipped precisely for this —
    full clips are downloaded on demand and may not exist yet at detect time), falling back to
    the full clip on transition-era images that still bake clips in."""
    p = os.path.join(PROBES_DIR, f"probe_4k_{codec}.mkv")
    if os.path.exists(p):
        return p
    full = clip_master("4k", codec)
    return full if os.path.exists(full) else None


def decode_supported(gpu, codec):
    """Can this GPU HARDWARE-decode this source codec? Decodes 1 frame of the probe clip on
    the GPU; if the hw path is missing ffmpeg would fall to CPU and this returns False — which
    is exactly what keeps an AV1-decode-incapable card from silently benchmarking the CPU."""
    master = probe_clip(codec)          # decode capability is resolution-independent
    if not master:
        return codec == "hevc"          # no probe clip at all (dev build): assume only HEVC
    if gpu["api"] == "vaapi":
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error",
               "-hwaccel", "vaapi", "-hwaccel_device", gpu["device"],
               "-hwaccel_output_format", "vaapi", "-i", master,
               "-frames:v", "1", "-an", "-f", "null", "-"]
    else:  # nvenc/cuda
        dev = ["-hwaccel_device", str(gpu["index"])] if gpu.get("index") is not None else []
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error",
               "-hwaccel", "cuda", "-hwaccel_output_format", "cuda"] + dev + \
              ["-i", master, "-frames:v", "1", "-an", "-f", "null", "-"]
    try:
        r = subprocess.run(cmd, env=stream_env(gpu),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        return r.returncode == 0
    except Exception:
        return False


def tonemap_supported(gpu):
    """Can this device HDR10→SDR tone-map in hardware (CPU: via the SIMD tonemapx filter)?
    1-frame decode + tone-map + encode of the HDR probe clip — the same never-silently-
    fall-back philosophy as decode_supported. tonemap_vaapi is Intel-iHD-only in practice,
    so AMD/Mesa is expected to fail here and simply not be offered the HDR source."""
    master = probe_clip("hdr")
    if not master:
        return False                    # no HDR probe clip at all (dev build)
    if gpu.get("vendor") == "cpu":
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-i", master,
               "-frames:v", "1", "-an",
               "-vf", "tonemapx=tonemap=bt2390:t=bt709:m=bt709:p=bt709:format=yuv420p",
               "-f", "null", "-"]
    elif gpu["api"] == "vaapi":
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error",
               "-hwaccel", "vaapi", "-hwaccel_device", gpu["device"],
               "-hwaccel_output_format", "vaapi", "-i", master, "-frames:v", "1", "-an",
               "-noautoscale",
               "-vf", "tonemap_vaapi=format=nv12:t=bt709:m=bt709:p=bt709,"
                      "scale_vaapi=w=1920:h=1080",
               "-c:v", "h264_vaapi", "-f", "null", "-"]
    else:  # nvenc/cuda
        dev = ["-hwaccel_device", str(gpu["index"])] if gpu.get("index") is not None else []
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error",
               "-hwaccel", "cuda", "-hwaccel_output_format", "cuda"] + dev + \
              ["-i", master, "-frames:v", "1", "-an", "-noautoscale",
               "-vf", "tonemap_cuda=format=nv12:tonemap=bt2390:p=bt709:t=bt709:m=bt709,"
                      "scale_cuda=1920:1080",
               "-c:v", "h264_nvenc", "-f", "null", "-"]
    try:
        r = subprocess.run(cmd, env=stream_env(gpu),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        return r.returncode == 0
    except Exception:
        return False


def _nvidia_present_on_host():
    """True if an NVIDIA display GPU exists on the host (even without the runtime)."""
    try:
        out = subprocess.run(["lspci", "-nn"], capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines():
            low = line.lower()
            if "10de:" in low and ("vga" in low or "3d controller" in low or "display" in low):
                return True
    except Exception:
        pass
    return False


def detect_gpus():
    """Enumerate testable GPUs. Returns list of dicts with idx assigned."""
    gpus = []

    # Intel / AMD via VAAPI render nodes
    for node in sorted(glob.glob("/dev/dri/renderD*")):
        base = os.path.basename(node)
        vendor_id = (_read(f"/sys/class/drm/{base}/device/vendor") or "").lower()
        dev_link = f"/sys/class/drm/{base}/device"
        pci = os.path.basename(os.path.realpath(dev_link)) if os.path.exists(dev_link) else None
        if vendor_id == "0x8086":
            vendor = "intel"
        elif vendor_id == "0x1002":
            vendor = "amd"
        else:
            continue  # NVIDIA / other render nodes are handled via nvidia-smi, not VAAPI
        name = _pci_name(pci) or ("Intel GPU" if vendor == "intel" else "AMD GPU")
        is_igpu = vendor == "intel" and bool(pci) and pci.startswith("0000:00:")
        # kernel DRM driver (i915 / xe / amdgpu) from the device's driver symlink
        kdrv = None
        dl = f"/sys/class/drm/{base}/device/driver"
        if os.path.islink(dl):
            kdrv = os.path.basename(os.path.realpath(dl))
        gpus.append({"vendor": vendor, "api": "vaapi", "name": name, "device": node,
                     "pci": pci, "driver": "iHD" if vendor == "intel" else "radeonsi",
                     "kernel_driver": kdrv,
                     "available": True, "note": None, "is_igpu": is_igpu})

    # NVIDIA via NVENC
    nv = _nvidia_smi_gpus()
    if nv:
        sigs = load_nvenc_sigs()
        for g in nv:
            gpus.append({"vendor": "nvidia", "api": "nvenc", "name": g["name"],
                         "index": g["index"], "driver": g.get("driver"),
                         "kernel_driver": "nvidia",
                         # is the driver's NVENC session cap patched out? (True/False/None)
                         "nvenc_unlocked": detect_nvenc_unlocked(g.get("driver"), sigs),
                         "available": True, "note": None, "is_igpu": False})
    elif _nvidia_present_on_host():
        gpus.append({"vendor": "nvidia", "api": "nvenc", "name": "NVIDIA GPU (runtime not active)",
                     "index": 0, "driver": None, "kernel_driver": "nvidia", "available": False,
                     "note": "Add --runtime=nvidia to Extra Parameters to test this GPU.",
                     "is_igpu": False})

    # CPU software transcoding — always present; the power/speed baseline the GPUs are measured against
    gpus.append({"vendor": "cpu", "api": "software", "name": cpu_model() or "CPU (software)",
                 "device": None, "driver": "libx264/x265/svt-av1", "kernel_driver": None,
                 "available": True, "note": None, "is_igpu": False})

    # probe which output codecs each available GPU can actually ENCODE (h264 assumed)
    # and which source codecs it can hardware-DECODE (these are independent capabilities)
    for g in gpus:
        if g["vendor"] == "cpu":
            # software encoders/decoders are universal — no probe needed (libx264 is 8-bit only);
            # HDR tone-map (tonemapx) is still probed like everything else
            g["codecs"] = ["h264", "hevc", "av1"]
            g["decodes"] = ["h264", "hevc", "av1"]
            g["codecs10"] = ["hevc", "av1"]
            if tonemap_supported(g):
                g["decodes"].append("hdr")
        elif g["available"]:
            g["codecs"] = ["h264"] + [c for c in ("hevc", "av1") if codec_supported(g, c)]
            g["decodes"] = [c for c in INPUT_CODECS if decode_supported(g, c)] or ["hevc"]
            # "hdr" = the HDR10 source option; offered only when the tone-map pipeline works
            # (tonemap_vaapi is Intel-iHD-only in practice — AMD/Mesa is expected to sit out)
            if "hevc" in g["decodes"] and tonemap_supported(g):
                g["decodes"].append("hdr")
            # which output codecs can this GPU encode in 10-bit (for the 4K->4K archival case)
            g["codecs10"] = [c for c in ("hevc", "av1")
                             if c in g["codecs"] and codec_supported(g, c, ten_bit=True)]
        else:
            g["codecs"] = ["h264"]
            g["decodes"] = ["hevc"]
            g["codecs10"] = []

    for i, g in enumerate(gpus):
        g["idx"] = i
    return gpus


def public_gpus(gpus):
    """Trimmed view for the UI / state.json."""
    return [{"idx": g["idx"], "name": g["name"], "vendor": g["vendor"],
             "api": g["api"], "available": g["available"], "note": g["note"],
             "is_igpu": g["is_igpu"], "driver": g.get("driver"),
             "kernel_driver": g.get("kernel_driver"),
             "codecs": g.get("codecs", ["h264"]),
             "decodes": g.get("decodes", ["hevc"]),
             "codecs10": g.get("codecs10", [])} for g in gpus]


# --------------------------------------------------------------- ffmpeg stream
_ALL_STREAMS = []          # registry for clean shutdown
_REG_LOCK = threading.Lock()


class Stream:
    def __init__(self, sid, cmd, env=None):
        self.id = sid
        self.cmd = cmd
        self.env = env
        self.proc = None
        self.lock = threading.Lock()
        self.out_us = 0
        self.wall = 0.0
        self.fps = 0.0
        self.err_lines = []

    def start(self):
        self.proc = subprocess.Popen(
            self.cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, env=self.env,
        )
        with _REG_LOCK:
            _ALL_STREAMS.append(self)
        threading.Thread(target=self._read_out, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()

    def _read_out(self):
        for line in self.proc.stdout:
            kv = parse_progress_kv(line)
            if not kv:
                continue
            k, v = kv
            if k == "out_time_us":
                try:
                    ov = int(v)
                except ValueError:
                    continue
                with self.lock:
                    self.out_us = ov
                    self.wall = time.monotonic()
            elif k == "fps":
                try:
                    fv = float(v)
                except ValueError:
                    continue
                with self.lock:
                    self.fps = fv

    def _read_err(self):
        try:
            for line in self.proc.stderr:
                with self.lock:
                    self.err_lines.append(line)
                    if len(self.err_lines) > 40:
                        self.err_lines = self.err_lines[-40:]
        except Exception:
            pass

    def snapshot(self):
        with self.lock:
            return self.out_us, self.wall, self.fps

    def err_tail(self):
        with self.lock:
            return "".join(self.err_lines[-40:])

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def stop(self):
        p = self.proc
        if p and p.poll() is None:
            try:
                p.terminate()
                p.wait(timeout=5)
            except Exception:
                try:
                    p.kill()
                    p.wait(timeout=5)   # reap — kill() without wait() leaves a zombie
                except Exception:
                    pass
        with _REG_LOCK:
            if self in _ALL_STREAMS:
                _ALL_STREAMS.remove(self)


def stop_all():
    with _REG_LOCK:
        streams = list(_ALL_STREAMS)
    for s in streams:
        s.stop()


# ------------------------------------------------------------ ffmpeg commands
def stream_env(gpu):
    """Per-vendor environment (VAAPI driver selection)."""
    env = os.environ.copy()
    if gpu["api"] == "vaapi":
        env["LIBVA_DRIVER_NAME"] = "radeonsi" if gpu["vendor"] == "amd" else "iHD"
    return env


OUTPUT_BITRATE_BY_RES = {"4k": _env("OUTPUT_BITRATE_4K", "25M"),
                         "1080p": OUTPUT_BITRATE,          # canonical 8M
                         "720p": _env("OUTPUT_BITRATE_720", "4M")}


def cpu_preset_arg(codec, preset):
    """libx264/libx265 take a named preset; libsvtav1 takes a NUMBER — map the named tier across."""
    if codec == "av1":
        return {"veryfast": "10", "medium": "6", "slow": "4"}.get(preset, "8")
    return preset


def transcode_cmd(gpu, src, codec="h264", target_res="1080p", ten_bit=False, preset="veryfast",
                  hdr=False, subs=False):
    """Full transcode (decode + scale + encode), flat-out, looped, to null. On a GPU everything
    runs on the GPU; on the CPU device it's pure software (libx264/x265/svt-av1 at `preset`).
    Output resolution + bit depth selectable; 10-bit uses p010 surfaces (HEVC gets main10).
    `hdr` adds the HDR10→SDR tone-map stage (per-vendor filter, output always SDR 8-bit);
    `subs` burns the shipped .ass subtitles in (hw download → CPU libass render → encoder — the
    realistic Plex/Jellyfin burn-in cost). Chains validated live incl. the -stream_loop seam."""
    if hdr and subs:
        raise ValueError("HDR + subtitle burn-in combined is not supported")
    if hdr:
        ten_bit = False                          # tone-mapped output is SDR 8-bit by definition
    w, h = RES_DIMS.get(target_res, (1920, 1080))
    bitrate = OUTPUT_BITRATE_BY_RES.get(target_res, OUTPUT_BITRATE)

    if gpu.get("vendor") == "cpu":
        base = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostats", "-stream_loop", "-1"]
        if hdr:
            vf = (f"tonemapx=tonemap=bt2390:t=bt709:m=bt709:p=bt709:format=yuv420p,"
                  f"scale={w}:{h}")
        elif subs:
            vf = f"scale={w}:{h},subtitles=filename={BURNIN_ASS}"
        else:
            vf = f"scale={w}:{h}"
        pix = ["-pix_fmt", "yuv420p10le" if (ten_bit and codec in ("hevc", "av1")) else "yuv420p"]
        enc = ["-c:v", CPU_ENCODERS[codec], "-preset", cpu_preset_arg(codec, preset)]
        if ten_bit and codec == "hevc":
            enc += ["-profile:v", "main10"]
        enc += ["-b:v", bitrate]
        return (base + ["-i", src, "-an", "-vf", vf] + pix + enc
                + ["-f", "null", "-progress", "pipe:1", "-"])

    fmt = "p010le" if ten_bit else "nv12"
    base = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostats", "-stream_loop", "-1"]
    # -noautoscale: stop ffmpeg auto-inserting a scale filter after scale_vaapi/scale_cuda. That
    # auto filter fails to re-initialise at the -stream_loop seam ("Impossible to convert between
    # the formats…"), killing the stream on its 2nd pass — bites fast-looping sources like the
    # 8-bit H.264 clip, on BOTH VAAPI and CUDA. Our scale filter already emits exactly the target
    # format, so the auto filter is never needed. It's an OUTPUT option → must sit AFTER -i.
    out_pre = ["-noautoscale"]
    if gpu["api"] == "vaapi":
        dec = ["-hwaccel", "vaapi", "-hwaccel_device", gpu["device"],
               "-hwaccel_output_format", "vaapi"]
        if hdr:
            # tone-map at full 4K (matches Jellyfin's order), then scale — emits SDR nv12
            vf = (f"tonemap_vaapi=format=nv12:t=bt709:m=bt709:p=bt709,"
                  f"scale_vaapi=w={w}:h={h}")
        elif subs:
            # burn-in = the real cost: download, CPU scale + libass render, re-upload for encode
            vf = (f"hwdownload,format=nv12,scale={w}:{h},"
                  f"subtitles=filename={BURNIN_ASS},hwupload")
        else:
            vf = f"scale_vaapi=w={w}:h={h}:format={fmt}"
    else:  # nvenc
        if subs:
            # NO -hwaccel_output_format: frames auto-download after NVDEC, sw scale + libass,
            # and NVENC uploads system-memory frames itself. The trailing format=nv12 pin is
            # REQUIRED — without it the graph negotiates a format nvenc rejects
            # ("CreateInputBuffer failed: invalid param (8)", found live on the 5090).
            dec = ["-hwaccel", "cuda"]
            if gpu.get("index") is not None:
                dec += ["-hwaccel_device", str(gpu["index"])]
            vf = f"scale={w}:{h},subtitles=filename={BURNIN_ASS},format=nv12"
            out_pre = []                         # validated without -noautoscale (sw chain)
        else:
            dec = ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
            if gpu.get("index") is not None:
                dec += ["-hwaccel_device", str(gpu["index"])]
            if hdr:
                vf = (f"tonemap_cuda=format=nv12:tonemap=bt2390:p=bt709:t=bt709:m=bt709,"
                      f"scale_cuda={w}:{h}")
            else:
                vf = f"scale_cuda={w}:{h}:format={fmt}"
    enc = ["-c:v", enc_name(gpu["api"], codec)]
    if ten_bit and codec == "hevc":
        enc += ["-profile:v", "main10"]          # AV1 Main handles 10-bit from p010 with no flag
    enc += ["-b:v", OUTPUT_BITRATE_BY_RES.get(target_res, OUTPUT_BITRATE)]
    return (base + dec + ["-i", src, "-an"] + out_pre + ["-vf", vf] + enc
            + ["-f", "null", "-progress", "pipe:1", "-"])


_STAGED = None   # which input codec is currently copied into the ramdisk (None = empty)


def clear_ramdisk():
    """Drop the staged clip from RAM (called when a run ends / returns to the picker)."""
    global _STAGED
    try:
        if os.path.exists(SOURCE):
            os.remove(SOURCE)
    except Exception:
        pass
    _STAGED = None


def ramdisk_budget():
    """Usable bytes in the ramdisk (90 % of free), for sizing a custom-file sample."""
    try:
        s = os.statvfs(RAMDISK)
        return int(s.f_bavail * s.f_frsize * 0.9)
    except Exception:
        return 450 * 1024 * 1024


def ffprobe_meta(path):
    """First video stream's {codec,width,height,duration,bitrate} via ffprobe, or {}."""
    try:
        out = subprocess.run(
            [FFPROBE, "-v", "error", "-select_streams", "v:0", "-of", "json",
             "-show_entries", "stream=codec_name,width,height:format=duration,bit_rate", path],
            capture_output=True, text=True, timeout=15).stdout
        d = json.loads(out or "{}")
        st = (d.get("streams") or [{}])[0]
        fmt = d.get("format") or {}
        def _f(x):
            try:
                return float(x)
            except (TypeError, ValueError):
                return None
        return {"codec": st.get("codec_name"), "width": st.get("width"),
                "height": st.get("height"), "duration": _f(fmt.get("duration")),
                "bitrate": _f(fmt.get("bit_rate"))}
    except Exception:
        return {}


def list_custom_files():
    """Scan the TOP LEVEL of the BYO-clip drop folder (never recurse into a media tree).
    {"files":[{name,path,codec,res}], "library": bool} — `library` True when there are more
    than MAX_CUSTOM_FILES (probably a whole library) so we refuse to enumerate it."""
    try:
        names = sorted(n for n in os.listdir(INPUT_DIR)
                       if is_video_file(n) and os.path.isfile(os.path.join(INPUT_DIR, n)))
    except Exception:
        return {"files": [], "library": False}
    if len(names) > MAX_CUSTOM_FILES:
        return {"files": [], "library": True}
    files = []
    for n in names:
        p = os.path.join(INPUT_DIR, n)
        m = ffprobe_meta(p)
        files.append({"name": n, "path": p, "codec": m.get("codec"),
                      "res": (f"{m.get('width')}x{m.get('height')}" if m.get("width") else None)})
    return {"files": files, "library": False}


def sample_custom(path):
    """Copy a representative ~60 s slice — CENTRED ON THE MIDDLE (movies open with easy logos/
    credits) — of a user's file into the ramdisk; whole file if short. No re-encode (`-c copy`),
    so it's the user's real bitstream; trimmed to fit the ramdisk for very high-bitrate files."""
    global _STAGED
    os.makedirs(RAMDISK, exist_ok=True)
    clear_ramdisk()
    budget = ramdisk_budget()
    if budget < 64 * 1024 * 1024:      # nearly-full ramdisk: no room for a usable sample
        raise RuntimeError("the RAM disk is almost full — free up space and try again")
    m = ffprobe_meta(path)
    size = os.path.getsize(path)
    dur = m.get("duration")
    # a stream bitrate is best; fall back to the average from size/duration so an unusually
    # high-bitrate file is still bounded rather than copied whole
    br = m.get("bitrate") or (size * 8.0 / dur if dur and dur > 0 else None)
    # size the window against 60% of the budget: bitrate is an AVERAGE, and a variable-bitrate
    # window can run well above it, so leave headroom rather than fill the disk to the brim
    want = fit_seconds(br, int(budget * 0.6), want=60.0)
    if br and want < 3.0:              # even a few seconds won't fit — too high-bitrate to sample
        raise RuntimeError("this file's bitrate is too high to sample into the RAM disk")
    if dur is not None:
        start, length = sample_window(dur, want=want)
        if length is None and size > budget:   # "short" file that's still bigger than the disk
            start, length = 0.0, want          # bound it by time so it can't overflow
    elif size <= budget:
        start, length = 0.0, None              # no duration, but the whole file fits anyway
    else:
        raise RuntimeError("couldn't read this file's length — it may be damaged")
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y"]
    if start > 0:
        cmd += ["-ss", str(start)]              # FAST input seek (before -i): jumps via index
    cmd += ["-i", path, "-map", "0:v:0"]
    if length is not None:
        cmd += ["-t", str(length)]              # bounded by TIME → a valid, playable sample
    cmd += ["-c", "copy", "-an", SOURCE]
    publish(phase="preparing", message="Loading a 60 s sample of your file into RAM…")
    # Popen + poll rather than a blocking run: a slow mount or damaged file must stay
    # cancellable within ~1 s (same pattern as generate_canonical_clip).
    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 120
    while p.poll() is None:
        if _ABORT.is_set() or time.monotonic() > deadline:
            p.kill()
            p.wait(timeout=10)                  # reap; never leave a zombie or a partial file
            try:
                os.remove(SOURCE)
            except Exception:
                pass
            raise RuntimeError("cancelled" if _ABORT.is_set()
                               else "sampling timed out — is the file readable/seekable?")
        _ABORT.wait(0.5)
    # a clean time-bounded copy exits 0; a non-zero code means a real failure (a ramdisk
    # overflow from an extreme VBR peak beyond the headroom shows up here as ffmpeg's ENOSPC),
    # and the ffprobe check confirms the staged file is actually readable video.
    if (p.returncode != 0 or not os.path.exists(SOURCE) or os.path.getsize(SOURCE) == 0
            or not ffprobe_meta(SOURCE).get("codec")):
        try:
            os.remove(SOURCE)
        except Exception:
            pass
        raise RuntimeError("could not sample the chosen file")
    return SOURCE


_STAGED_VERIFIED = True   # was the last staged canonical clip the pinned bitstream?
_STAGED_SHA = None        # SHA-256 of the clip CURRENTLY in the ramdisk (canonical clips only)


def stage_clip(source_res, input_codec, custom_path=None):
    """Get the source into the ramdisk and return its path. Custom file → a middle-60 s sample;
    otherwise the canonical clip (shipped in transition images / cached in appdata / downloaded
    on demand with live progress / generated offline as a last resort — see ensure_clip).
    Clear-before-stage keeps exactly ONE clip in RAM so a 512 MiB ramdisk always fits.

    The staged clip is SHA-256'd against the pinned manifest (during the copy — the bytes are
    read anyway, so it's ~free) and, crucially, RE-hashed on the reuse path: a previously staged
    clip is trusted only if its bytes STILL match. That closes the swap-after-stage hole (run
    once → swap the staged file in the RAM-disk mount → run again on the easy clip). It binds the
    submission to a correctly staged pinned bitstream — it does NOT prove what ffmpeg consumed on
    hardware the user controls; that tier is handled statistically + by moderation (by design)."""
    global _STAGED, _STAGED_VERIFIED, _STAGED_SHA
    if custom_path:
        _STAGED = None
        _STAGED_SHA = None
        return sample_custom(custom_path)
    key = (source_res, input_codec)
    name = os.path.basename(clip_master(source_res, input_codec))
    expected_sha = CLIP_MANIFEST.get(name, (None, None))[0]
    os.makedirs(RAMDISK, exist_ok=True)
    # REUSE PATH — a clip is already in RAM. Re-hash it every run (cheap vs a transcode) so a
    # file swapped into the ramdisk between runs cannot ride a stale "verified" flag. Bytes still
    # match ⇒ trust; else fall through and re-stage from the master (self-heals).
    if _STAGED == key and os.path.exists(SOURCE):
        if expected_sha is None:
            return SOURCE                   # non-manifest clip (dev build): nothing to verify
        cur = sha256_file(SOURCE)
        if cur == expected_sha:
            _STAGED_SHA, _STAGED_VERIFIED = cur, True
            return SOURCE
    clear_ramdisk()
    for attempt in (0, 1):
        master, verified = ensure_clip(source_res, input_codec, abort_event=_ABORT)
        if master.startswith(RAMDISK):
            # one-shot download straight into the ramdisk (no /config): RENAME, never copy —
            # two 455 MB copies would overflow the 512 MiB tmpfs (download already hash-verified)
            os.replace(master, SOURCE)
            _STAGED_SHA = sha256_file(SOURCE) if expected_sha else None
        else:
            publish(phase="preparing", message=f"Loading {source_res} {input_codec.upper()} test clip into RAM…")
            _STAGED_SHA = copy_with_sha256(master, SOURCE)   # hash the staged bytes for free
        # the staged clip is verified iff its ACTUAL bytes match the manifest (subsumes the
        # download/generate checks); ensure_clip's verdict applies only with no manifest hash
        _STAGED_VERIFIED = (_STAGED_SHA == expected_sha) if expected_sha else verified
        if _STAGED_VERIFIED or expected_sha is None or attempt == 1:
            break
        # CACHED clip whose bytes no longer match the manifest (bit-rot keeps the size, so the
        # cheap size check passed): delete it and re-fetch ONCE — an honest user's runs must not
        # silently become local-only forever over a corrupt cache file. Shipped/generated
        # mismatches don't retry (re-fetching can't improve them).
        cached = os.path.join(CLIPS_CACHE_DIR, name)
        if master != cached or not os.path.exists(cached):
            break
        publish(message=f"Cached {input_codec.upper()} clip failed verification — re-downloading it…")
        try:
            os.remove(cached)
        except Exception:
            break
    _STAGED = key
    return SOURCE


def probe_gpu(gpu, src, codec="h264", target_res="1080p", ten_bit=False, preset="veryfast",
              hdr=False, subs=False):
    """Quick 1-stream check the chosen pipeline actually transcodes. (ok, err_tail)."""
    s = Stream(0, transcode_cmd(gpu, src, codec, target_res, ten_bit, preset, hdr, subs),
               env=stream_env(gpu))
    try:
        s.start()
    except Exception as e:
        return False, str(e)
    ok = False
    t0 = time.monotonic()
    while time.monotonic() - t0 < 10:
        if not s.alive():
            break
        if s.snapshot()[0] > 0:
            ok = True
            break
        time.sleep(0.3)
    err = s.err_tail()
    s.stop()
    return ok, err


# ------------------------------------------------------------------- the ramp
def _sample_once(streams, prev):
    """Compute current inst speed per stream, update prev snapshots.
    Returns list of (id, inst_speed, fps, alive)."""
    res = []
    for s in streams:
        out_us, wall, fps = s.snapshot()
        p = prev.get(s.id)
        sp = 0.0
        if p and wall > p[1]:
            sp = compute_inst_speed(p[0], p[1], out_us, wall)
        prev[s.id] = (out_us, wall)
        res.append((s.id, sp, fps, s.alive()))
    return res


def _publish_level(n, samp, last_passing, phase, mode="streaming"):
    cards, speeds = [], []
    for sid, sp, fps, _alive in samp:
        speeds.append(sp)
        # convert mode has NO 1.0×-realtime rule: a stream is healthy as long as it moves
        # (a 0.7× CPU convert is fine, not "failing" — don't paint the tiles red)
        ok = (sp >= 1.0) if mode == "streaming" else (sp > 0)
        cards.append({"id": sid, "speed": round(sp, 2),
                      "fps": round(fps, 1), "pass": ok})
    mn = min(speeds) if speeds else 0.0
    av = sum(speeds) / len(speeds) if speeds else 0.0
    publish(phase=phase, stream_count=n, streams=cards,
            min_speed=round(mn, 2), avg_speed=round(av, 2),
            combined_speed=round(sum(speeds), 2), last_passing=last_passing,
            message=f"{n} stream(s) — worst {mn:.2f}× / avg {av:.2f}×")


def _aborted_level(streams):
    """Stop this level's streams and return the abort sentinel (caller bails out of the ramp)."""
    for s in streams:
        s.stop()
    return {"worst": 0.0, "combined": 0.0, "cap": False, "power": None,
            "power_pkg": None, "aborted": True}


def run_level(gpu, src, codec, n, last_passing, target_res="1080p", ten_bit=False, preset="veryfast",
              mode="streaming", hdr=False, subs=False):
    """Run n fresh streams from the clip start; hold; measure.
    Returns dict: {worst, combined, cap (bool), power (mean W during hold or None)}."""
    env = stream_env(gpu)
    streams = [Stream(i + 1, transcode_cmd(gpu, src, codec, target_res, ten_bit, preset, hdr, subs),
                      env=env)
               for i in range(n)]
    for s in streams:
        s.start()
    prev = {}
    for s in streams:
        out_us, wall, _ = s.snapshot()
        prev[s.id] = (out_us, wall)

    # settle: live display, no accumulation (discard startup jitter)
    t_end = time.monotonic() + SETTLE_SECONDS
    while time.monotonic() < t_end:
        if _ABORT.wait(SAMPLE_INTERVAL):           # returns instantly when cancelled
            return _aborted_level(streams)
        _publish_level(n, _sample_once(streams, prev), last_passing, "settling", mode)

    # after settle, detect streams that died on startup without ever producing output —
    # a RESOURCE ceiling (driver session cap, VRAM wall, or something new), never a
    # throughput failure. The stderr signature says which; unknown signatures still stop
    # the ramp cleanly (classification refines them against the VRAM prediction later).
    cap_kind = None
    for s in streams:
        if (not s.alive()) and s.snapshot()[0] == 0:
            cap_kind = death_reason(s.err_tail()) or "unrecognised"
            break
    if cap_kind:
        for s in streams:
            s.stop()
        return {"worst": 0.0, "combined": 0.0, "cap": True, "cap_kind": cap_kind,
                "power": None, "power_pkg": None}


    # hold: accumulate per-stream inst-speed samples (+ GPU/package power) for the decision
    acc = {s.id: [] for s in streams}
    powers, powers_pkg = [], []
    t_end = time.monotonic() + HOLD_SECONDS
    while time.monotonic() < t_end:
        if _ABORT.wait(SAMPLE_INTERVAL):           # returns instantly when cancelled
            return _aborted_level(streams)
        samp = _sample_once(streams, prev)
        for sid, sp, _fps, alive in samp:
            if alive:
                acc[sid].append(sp)
        pw = telemetry_power()
        if pw:
            powers.append(pw)
        pk = telemetry_power_pkg()
        if pk:
            powers_pkg.append(pk)
        _publish_level(n, samp, last_passing, "holding", mode)

    means = {sid: (sum(v) / len(v) if v else 0.0) for sid, v in acc.items()}
    mean_power = round(sum(powers) / len(powers), 1) if powers else None
    mean_pkg = round(sum(powers_pkg) / len(powers_pkg), 1) if powers_pkg else None

    # LATE death check: a stream still allocating at settle-end can die DURING the hold with
    # zero output — that is a resource ceiling, not a 0.0x throughput failure (validated on the
    # patched 5090: at N=19 the VRAM-starved stream outlived a short settle, then died).
    # A stream that produced output and THEN died mid-hold is the same ceiling when its stderr
    # matches a known resource pattern; otherwise it simply cannot claim the level — samples
    # only accumulate while alive, so without this its mean would be computed over the living
    # part and a level with a DEAD stream could pass the "strict worst-stream" rule.
    for s in streams:
        if s.alive():
            continue
        kind = death_reason(s.err_tail())
        if s.snapshot()[0] == 0 or kind:
            kind = kind or "unrecognised"
            for t in streams:
                t.stop()
            return {"worst": 0.0, "combined": 0.0, "cap": True, "cap_kind": kind,
                    "power": None, "power_pkg": None}
        means[s.id] = 0.0                # died mid-hold, unrecognised cause → level fails
    worst = min(means.values()) if means else 0.0
    combined = sum(means.values())

    # dGPU VRAM accounting at END of hold — allocations are fully settled here (sampling at
    # hold-START catches sessions mid-allocation at high N and garbles the slope)
    vram_ours = vram_free = None
    if gpu.get("vendor") in ("nvidia", "amd"):
        vram_ours = gpu_mem_ours(gpu)
        g = gpu_mem_global(gpu)
        if g:
            vram_free = round(g[1] - g[0], 1)

    for s in streams:
        s.stop()
    return {"worst": worst, "combined": combined, "cap": False,
            "vram_ours": vram_ours, "vram_free": vram_free,
            "power": mean_power, "power_pkg": mean_pkg}


def _build_result(gpu, codec, confirmed_max, single, peak_combined, per_level,
                  capped, projected, pwr, input_codec="hevc", custom_source=False,
                  source_res="4k", target_res="1080p", ten_bit=False,
                  mode="streaming", rec_workers=None, preset="veryfast", subs=False,
                  clip_verified=True, clip_sha256=None):
    """Assemble the structured result payload + efficiency.
    `pwr` = {idle, idle_pkg, one, one_pkg, peak, peak_pkg} (board + package watts).
    Returns (result, spw, power_estimated)."""
    # pick the relevant power series: iGPU AND the CPU device use CPU-package (delta estimate) —
    # same sensor on both sides makes the CPU-vs-iGPU efficiency comparison exact. dGPU uses board.
    is_cpu = gpu["vendor"] == "cpu"
    if gpu["is_igpu"] or is_cpu:
        idle_w, one_w, load_w = pwr.get("idle_pkg"), pwr.get("one_pkg"), pwr.get("peak_pkg")
        eff_power = estimate_igpu_power(load_w, idle_w)     # dynamic = load − idle
        power_estimated = True
    else:
        idle_w, one_w, load_w = pwr.get("idle"), pwr.get("one"), pwr.get("peak")
        eff_power = load_w                                  # total board power at full load
        power_estimated = False

    spw = wps = None
    if eff_power and peak_combined:
        spw = round(peak_combined / eff_power, 3)           # throughput-streams per watt
        wps = round(eff_power / peak_combined, 2)           # watts per stream

    # adaptive plain-language efficiency insight (also baked into the PNG card)
    insight = ""
    if is_cpu:
        insight = ""                                        # the "vs CPU" block tells the story
    elif gpu["is_igpu"] and eff_power is not None:
        insight = (f"Adds only ~{eff_power:.0f} W under load (est.) — ideal for light or "
                   f"always-on transcoding, far less power than a discrete GPU.")
    elif not gpu["is_igpu"] and one_w and idle_w is not None:
        # convert mode has no "streams" — say file/workers or the reader hears per-worker watts
        if mode == "convert":
            insight = (f"Draws ~{one_w:.0f} W converting even ONE file at a time "
                       f"(idle ~{idle_w:.0f} W) — most of that is fixed overhead, so running the "
                       f"recommended workers gets the same power much more done.")
        else:
            insight = (f"Draws ~{one_w:.0f} W to transcode even ONE stream (idle ~{idle_w:.0f} W) — "
                       f"power-efficient only under heavy load. For a few streams an iGPU uses a "
                       f"fraction of the power (unless the card is already busy with other work).")

    with TELE_LOCK:
        tele = dict(TELEMETRY)
    profile = profile_label(source_res, input_codec, target_res, codec,
                            custom_source, subs, ten_bit)
    # only the canonical 4K->1080p STREAMING run on a real GPU (non-custom) at the STRICT
    # 1.0x threshold, decoding the VERIFIED pinned bitstream, is leaderboard-comparable
    comparable = is_run_comparable(mode, source_res, target_res, custom_source, is_cpu,
                                   PASS_THRESHOLD, HOLD_SECONDS, SETTLE_SECONDS, clip_verified)
    result = {
        "tool_version": TOOL_VERSION, "mode": mode, "rec_workers": rec_workers,
        "is_cpu": is_cpu, "cpu_preset": (preset if is_cpu else None),
        "cpu_encoder": (CPU_ENCODERS.get(codec) if is_cpu else None),
        "cpu_threads": (os.cpu_count() if is_cpu else None),
        # package power via RAPL = a real measurement (drop the "(est.)" hedge for the CPU);
        # via intel_gpu_top it stays an estimate. Only meaningful on the package-delta paths.
        "power_rapl": bool(_RAPL_ACTIVE) if (is_cpu or gpu["is_igpu"]) else False,
        "vs_cpu": None, "cpu_baseline_missing": False,
        "profile": profile, "codec": codec, "input_codec": input_codec,
        "source_res": source_res, "target_res": target_res, "ten_bit": ten_bit,
        "subs_burn": subs, "clip_verified": clip_verified, "clip_sha256": clip_sha256,
        "comparable": comparable,
        "bitrate": OUTPUT_BITRATE_BY_RES.get(target_res, OUTPUT_BITRATE),
        "threshold": PASS_THRESHOLD,
        "vendor": gpu["vendor"], "gpu": gpu["name"], "api": gpu["api"],
        "custom_source": custom_source,   # local-only: never leaderboard-eligible
        "driver": gpu.get("driver"), "kernel_driver": gpu.get("kernel_driver"),
        # NVIDIA session-cap patch state (True/False/None) — the locked/unlocked board split
        "nvenc_unlocked": gpu.get("nvenc_unlocked"),
        "max_sustained": confirmed_max, "capped": capped, "projected": projected,
        "single_stream": single, "peak_combined": round(peak_combined, 2),
        "per_level": per_level,
        "streams_per_watt": spw, "watts_per_stream": wps, "power_estimated": power_estimated,
        "idle_power_w": idle_w, "one_stream_power_w": one_w, "load_power_w": load_w,
        "peak_power_w": eff_power, "power_insight": insight, "temp_c": tele.get("temp"),
        "cpu": cpu_model(), "ram": STATE.get("ram_speed"), "ram_type": STATE.get("ram_type"),
        "kernel": kernel_version(), "os_version": unraid_version(),
    }
    return result, spw, power_estimated


def _publish_cancelled():
    """Tear down a cancelled run and return the UI to the picker (no verdict recorded)."""
    stop_all()
    publish(ui="idle", phase="idle", message="Run cancelled.",
            stream_count=0, streams=[], min_speed=None, avg_speed=None,
            combined_speed=None, last_passing=0, confirmed_max=None,
            single_stream_speed=None, conv_levels=[], conv_testing_n=None, vram_note=None,
            projected=None, telemetry={},
            streams_per_watt=None, power_estimated=False, result=None,
            batch=False, batch_queue=[], batch_done=0, batch_results=[], batch_skipped=[], submitted=False)


def benchmark(gpu, codec="h264", input_codec="hevc", custom_path=None,
              source_res="4k", target_res="1080p", ten_bit=False, mode="streaming",
              skip_busy_warn=False, announce_done=True, subs=False):
    """One full run. `skip_busy_warn` bypasses the interactive busy gate (test-all batches must
    not stall waiting for a click); `announce_done=False` keeps ui='running' at finalize so a
    batch doesn't flash the single-run verdict between devices (result is still published)."""
    # GPU + system info readouts (CPU + RAM only for the Intel iGPU)
    info = {"selected_name": gpu["name"], "vendor": gpu["vendor"], "driver": gpu.get("driver"),
            "kernel_driver": gpu.get("kernel_driver"),
            "is_igpu": gpu["is_igpu"], "encoder": gpu["api"].upper(),
            "selected_codec": codec, "selected_input": input_codec,
            "selected_source_res": source_res, "selected_target_res": target_res,
            "selected_mode": mode,
            "kernel": kernel_version(), "os_version": unraid_version(),
            "cpu": None, "ram_speed": None, "ram_type": None, "ram_hint": None,
            "streams_per_watt": None, "result": None}
    # RAM speed/XMP matter whenever the transcode runs through SYSTEM memory: the iGPU (shares
    # DRAM as video memory) and the CPU software path (4K decode is memory-bound). A dedicated
    # GPU uses its own VRAM, so system RAM is irrelevant there.
    if gpu["is_igpu"] or gpu["vendor"] == "cpu":
        info["cpu"] = cpu_model()
        cfg, rated, mtype = ram_info()
        info["ram_type"] = mtype
        if cfg:
            info["ram_speed"] = f"{mtype}-{cfg}" if mtype else f"{cfg} MT/s"
            if rated and cfg < rated:
                info["ram_hint"] = f"below rated {rated} MT/s — XMP/EXPO off?"
    publish(**info)

    start_telemetry(gpu)
    try:
        # pre-test active-apps check: let telemetry warm up, then see if the GPU is busy
        # (_ABORT is cleared by start_run/start_batch BEFORE ui="preparing" — clearing it here
        # swallowed a cancel clicked during the info-gathering window above)
        _CONTINUE.clear()
        _CANCEL.clear()
        time.sleep(2)
        busy, load = gpu_busy(gpu)
        pre_busy_load = load          # travels in the payload — the clean-run predicate's input
        if busy and not skip_busy_warn:
            apps = gpu_clients(gpu)
            publish(ui="warn", phase="warn", busy_load=load, busy_apps=apps,
                    busy_named=(gpu["vendor"] == "nvidia" or host_pid_visible()),
                    message=f"This GPU looks busy ({load:.0f}% load) before the test.")
            while not (_CONTINUE.is_set() or _CANCEL.is_set()):
                time.sleep(0.2)
            if _CANCEL.is_set():
                publish(ui="idle", phase="idle", busy_load=None, busy_apps=[],
                        message="Cancelled. Stop the other GPU apps, then start again.")
                return                       # finally: stop_telemetry
            publish(ui="running", phase="preparing", message="Continuing…")

        # idle power baseline (all vendors) — measured FIRST, while the GPU is truly idle
        # (before clip-gen/probe spin it up; a dGPU stays boosted for a while after activity).
        publish(phase="preparing", message="Measuring idle power baseline…")
        b_idle, p_idle = [], []
        t_end = time.monotonic() + 5.0
        while time.monotonic() < t_end:
            if _ABORT.wait(0.5):                 # cancel during the idle baseline
                _publish_cancelled()
                return
            v = telemetry_power()
            if v:
                b_idle.append(v)
            pk = telemetry_power_pkg()
            if pk:
                p_idle.append(pk)
        pwr = {"idle": round(sum(b_idle) / len(b_idle), 1) if b_idle else None,
               "idle_pkg": round(sum(p_idle) / len(p_idle), 1) if p_idle else None,
               "one": None, "one_pkg": None, "peak": None, "peak_pkg": None}

        try:
            src = stage_clip(source_res, input_codec, custom_path)
        except Exception as e:
            if _ABORT.is_set():          # user cancelled mid-download — that's not an error,
                _publish_cancelled()     # return to the picker like every other abort path
                return
            publish(ui="error", phase="error", message=f"Could not load the test clip: {e}")
            return

        preset = CPU_PRESETS.get(mode, "veryfast")   # only used by the CPU (software) device
        hdr = input_codec == "hdr"                   # HDR source ⇒ the tone-map pipeline
        publish(phase="preparing", message=f"Probing {gpu['name']} ({gpu['api'].upper()}, {codec})…")
        ok, err = probe_gpu(gpu, src, codec, target_res, ten_bit, preset, hdr, subs)
        if not ok:
            tail = (err or "").strip().splitlines()[-1:] or [""]
            publish(ui="error", phase="error",
                    message=f"{gpu['name']} pipeline did not start. {tail[0]}")
            return

        if _ABORT.is_set():                      # cancel during clip-gen / probe
            _publish_cancelled()
            return

        publish(ui="running", phase="ramping", conv_levels=[], conv_testing_n=None,
                message=f"Testing {gpu['name']} ({gpu['api'].upper()}, {codec.upper()}). Starting ramp…")

        last_passing = 0
        single = None
        peak_combined = 0.0      # highest aggregate throughput seen — the GPU's ceiling
        per_level = []
        capped = False
        cap_kind = None          # which resource ceiling stopped a hard-death ramp
        cap_level = None         # the level whose stream failed to start
        vram_samples = []        # (n, MB held by OUR streams) per level — the slope source
        vram_free_last = None
        vram_total = None
        wall_pred = None         # live VRAM-wall prediction once the slope stabilises
        if gpu.get("vendor") in ("nvidia", "amd"):
            g0 = gpu_mem_global(gpu)
            if g0:
                vram_total = round(g0[1], 1)
        vram_free_start = round(g0[1] - g0[0], 1) if (gpu.get("vendor") in ("nvidia", "amd") and g0) else None
        ramp_max = MAX_STREAMS if mode == "streaming" else CONV_MAX_STREAMS
        for n in range(1, ramp_max + 1):
            if _ABORT.is_set():
                break
            if mode == "convert":
                publish(conv_testing_n=n)        # live curve: mark this level "measuring…"
            res = run_level(gpu, src, codec, n, last_passing, target_res, ten_bit, preset, mode,
                            hdr, subs)
            if res.get("aborted"):
                break
            worst, combined, cap = res["worst"], res["combined"], res["cap"]
            power, power_pkg = res.get("power"), res.get("power_pkg")

            if cap:
                capped = True
                cap_kind = res.get("cap_kind")
                cap_level = n
                print(f"[ramp] N={n} RESOURCE CEILING ({cap_kind}) -> MAX concurrent = "
                      f"{last_passing}, engine throughput ≈{peak_combined:.2f}x", flush=True)
                break

            if n == 1:
                single = round(worst, 2)
                pwr["one"], pwr["one_pkg"] = power, power_pkg     # power to do ONE stream
                publish(single_stream_speed=single)
                print(f"[ramp] single-stream speed = {worst:.3f}x", flush=True)

            per_level.append({"n": n, "worst": round(worst, 3), "combined": round(combined, 2)})

            # VRAM slope + early wall prediction (dGPU): "on track to wall around ~18"
            if res.get("vram_ours") is not None:
                vram_samples.append((n, res["vram_ours"]))
                vram_free_last = res.get("vram_free")
                slope = vram_slope(vram_samples)
                if slope and vram_free_last is not None:
                    wall_pred = predict_wall(n, vram_free_last, slope)
                    if wall_pred:
                        publish(vram_note=f"each stream ~{slope/1024:.1f} GB VRAM — "
                                          f"on track to wall near ~{wall_pred} streams")
                        print(f"[vram] N={n} ours={res['vram_ours']:.0f}MB free={vram_free_last:.0f}MB "
                              f"slope={slope:.0f}MB/stream predicted_wall≈{wall_pred}", flush=True)

            if mode == "streaming":
                # STREAMING: strict ≥1.0×-realtime worst-stream rule → headline max N
                if worst >= PASS_THRESHOLD:
                    last_passing = n
                    if combined > peak_combined:
                        peak_combined = combined
                        pwr["peak"], pwr["peak_pkg"] = power, power_pkg
                    print(f"[ramp] N={n} PASS  worst-mean={worst:.3f}x  combined={combined:.2f}x"
                          f"  power={power}", flush=True)
                    publish(last_passing=n, projected=round(peak_combined),
                            message=f"{n} sustained (worst {worst:.2f}×). Adding one more…")
                else:
                    print(f"[ramp] N={n} FAIL  worst-mean={worst:.3f}x  -> MAX SUSTAINED = {last_passing}",
                          flush=True)
                    break
            else:
                # CONVERSION: NO realtime rule — ramp until COMBINED throughput plateaus.
                sat = (n >= 2 and throughput_saturated(combined, peak_combined))
                if combined > peak_combined:
                    peak_combined = combined
                    pwr["peak"], pwr["peak_pkg"] = power, power_pkg
                last_passing = n
                print(f"[conv] N={n} combined={combined:.2f}x worst={worst:.2f}x power={power}"
                      f"{'  SATURATED' if sat else ''}", flush=True)
                conv_levels = [{"n": L["n"], "combined": L["combined"]} for L in per_level]
                publish(last_passing=n, projected=round(peak_combined),
                        conv_levels=conv_levels, conv_testing_n=None,
                        message=f"{n} parallel → {combined:.1f}× combined throughput…")
                if sat:
                    break

        # ---- cancelled mid-run: drop everything and go back to the picker, no verdict ----
        if _ABORT.is_set():
            _publish_cancelled()
            return                           # finally: stop_telemetry + stop_all

        # ---- finalize (single exit) ----
        publish(conv_testing_n=None, vram_note=None)   # clear live markers
        confirmed_max = last_passing
        # which limit stopped the ramp (the four-state taxonomy; None if ramp_max was reached)
        slope = vram_slope(vram_samples)
        limit_reason = None
        if capped:
            limit_reason = classify_stop(True, (None if cap_kind == "unrecognised" else cap_kind),
                                         cap_level, wall_pred)
        elif per_level and per_level[-1]["worst"] < PASS_THRESHOLD and mode == "streaming":
            limit_reason = "throughput"
        vram_extra = {
            "limit_reason": limit_reason,
            # run-integrity fields: a short hold submits burst performance as "sustained";
            # busy_load is the clean-run predicate's input (free VRAM alone isn't an idle card)
            "hold_seconds": HOLD_SECONDS,
            "settle_seconds": SETTLE_SECONDS,
            "busy_load": pre_busy_load,
            "is_igpu": gpu["is_igpu"],
            "vram_per_session_mb": round(slope, 1) if slope else None,
            "vram_total_mb": vram_total,
            "vram_free_start_mb": vram_free_start,
            # upper-bound estimate: what an otherwise-EMPTY card could reach (driver reserves
            # and display use make the true number a little lower — labelled as such in the UI)
            "vram_clean_ceiling": (int(vram_total // slope) if (slope and vram_total) else None),
        }
        projected = round(peak_combined) if (capped and peak_combined) else None
        # conversion: recommended parallel workers = the fastest measured level (peak throughput)
        rec_workers = (recommended_workers(per_level, peak_combined, cap=(last_passing if capped else None))
                       if mode == "convert" else None)
        result, spw, power_estimated = _build_result(
            gpu, codec, confirmed_max, single, peak_combined, per_level,
            capped, projected, pwr, input_codec, custom_source=bool(custom_path),
            source_res=source_res, target_res=target_res, ten_bit=ten_bit,
            mode=mode, rec_workers=rec_workers, preset=preset, subs=subs,
            clip_verified=(_STAGED_VERIFIED if not custom_path else True),
            clip_sha256=(_STAGED_SHA if not custom_path else None))
        result.update(vram_extra)

        # CPU baseline persistence & the GPU "vs CPU" efficiency comparison (skip for custom clips —
        # a BYO file has no reproducible per-profile baseline to compare against)
        if not custom_path:
            key = cpu_baseline_key(mode, result["profile"])
            if gpu["vendor"] == "cpu":
                result["baseline_saved"] = save_baseline(BASELINE_FILE, key, {
                    "mode": mode, "profile": result["profile"],
                    "single_stream": result["single_stream"], "peak_combined": result["peak_combined"],
                    "max_sustained": result["max_sustained"], "watts_per_stream": result["watts_per_stream"],
                    "peak_power_w": result["peak_power_w"], "preset": preset,
                    "encoder": CPU_ENCODERS.get(codec), "cpu_model": cpu_model(),
                    "tool_version": TOOL_VERSION, "ts": time.time()})
            else:
                base = load_baseline(BASELINE_FILE).get(key)
                if baseline_valid(base, cpu_model(), TOOL_VERSION):
                    result["vs_cpu"] = compute_vs_cpu(mode, result, base, is_dgpu=not gpu["is_igpu"])
                else:
                    result["cpu_baseline_missing"] = True

        # run history: the strip/delta compare against runs BEFORE this one, then this run is
        # appended (batch runs are normal runs — they land in history too). /config-unmounted
        # degrades to an empty strip, never errors.
        past = history_for(load_history(), gpu["name"], mode, result["profile"])
        entry = {k: result.get(k) for k in
                 ("mode", "profile", "custom_source", "max_sustained", "capped", "projected",
                  "single_stream", "peak_combined", "rec_workers", "watts_per_stream",
                  "peak_power_w", "power_estimated", "cpu_preset", "tool_version")}
        entry.update({"ts": time.time(), "gpu": gpu["name"], "vendor": gpu["vendor"],
                      "is_cpu": result["is_cpu"], "ram_speed": STATE.get("ram_speed"),
                      "driver": gpu.get("driver")})
        append_history(HISTORY_FILE, entry)
        hist_kw = {"history": past, "history_delta": run_delta(past[0] if past else None, entry),
                   "submitted": False}

        ui_kw = {"ui": "done", "phase": "done"} if announce_done else {}
        if mode == "convert":
            msg = ((f"Conversion: {single}× per file, ~{round(peak_combined)}× library "
                    f"throughput, fastest at ~{rec_workers} parallel.")
                   if single else "Conversion run ended without a measurable speed.")
            publish(confirmed_max=confirmed_max, cap_reason=None,
                    streams_per_watt=spw, power_estimated=power_estimated, result=result,
                    message=msg, **hist_kw, **ui_kw)
        elif capped:
            kind_msg = {"session": "the driver's NVENC session limit",
                        "memory": "its VRAM ceiling",
                        "unknown": "an unrecognised resource limit"}.get(limit_reason,
                                                                         "a resource limit")
            msg = (f"Hit {kind_msg} at {confirmed_max} streams"
                   + (f" — engine throughput ≈{projected}× realtime." if projected else "."))
            publish(confirmed_max=confirmed_max,
                    cap_reason=(limit_reason or "session"),
                    projected_uncapped=projected, projected=projected,
                    streams_per_watt=spw, power_estimated=power_estimated, result=result,
                    message=msg, **hist_kw, **ui_kw)
        else:
            msg = f"MAX SUSTAINED = {confirmed_max} simultaneous streams."
            publish(confirmed_max=confirmed_max, cap_reason=None,
                    streams_per_watt=spw, power_estimated=power_estimated, result=result,
                    message=msg, **hist_kw, **ui_kw)
    except Exception as e:
        # never leave the UI frozen mid-run — surface the error instead
        print(f"[benchmark] error: {e}", flush=True)
        publish(ui="error", phase="error", message=f"Benchmark error: {e}")
    finally:
        stop_telemetry()
        stop_all()
        clear_ramdisk()        # hot-unload the clip from RAM once the run is over


# ----------------------------------------------------------- run control (UI)
RUN_LOCK = threading.Lock()
_DETECTED = []                 # list of full GPU dicts
_CONTINUE = threading.Event()  # user clicked "Continue anyway" on the busy warning
_CANCEL = threading.Event()    # user clicked "Cancel" on the busy warning
_ABORT = threading.Event()     # user clicked "Cancel run" mid-run (prep or ramp)


def source_ready():
    # Start is near-instant when the default clip is staged, shipped, or cached (a missing one
    # triggers a one-time download with progress instead)
    return os.path.exists(SOURCE) or resolve_clip("source_4k_hevc.mkv")[0] is not None


def _available(gpus):
    return [g for g in gpus if g["available"]]


def _gpu_by_idx(idx):
    m = [g for g in _DETECTED if g["idx"] == idx and g["available"]]
    return m[0] if m else None


def select_gpu(idx):
    """Pick which detected GPU to test. Only when idle and the GPU is available. Resets the
    input/output codecs to defaults (HEVC in / H.264 out — always valid for the new GPU)."""
    with RUN_LOCK:
        with STATE_LOCK:
            cur = STATE.get("ui")
        if cur != "idle":
            return False
        g = _gpu_by_idx(idx)
        if not g:
            return False
        publish(selected_idx=idx, selected_name=g["name"],
                selected_input="hevc", selected_codec="h264", selected_subs=False,
                selected_source_res="4k", selected_target_res="1080p", message="Ready.")
        return True


def select_codec(codec):
    """Pick the output codec. Only when idle and ENCODE-supported by the selected GPU."""
    with RUN_LOCK:
        with STATE_LOCK:
            cur = STATE.get("ui")
            idx = STATE.get("selected_idx")
        if cur != "idle":
            return False
        g = _gpu_by_idx(idx)
        if not g or codec not in g.get("codecs", ["h264"]):
            return False
        publish(selected_codec=codec)
        return True


def select_input(codec):
    """Pick the source/input codec. Only when idle and hardware-DECODE-supported by the GPU
    (the gate that prevents a non-AV1-decoding card from silently falling back to the CPU)."""
    with RUN_LOCK:
        with STATE_LOCK:
            cur = STATE.get("ui")
            idx = STATE.get("selected_idx")
            sr = STATE.get("selected_source_res", "4k")
        if cur != "idle":
            return False
        g = _gpu_by_idx(idx)
        if (not g or codec not in g.get("decodes", ["hevc"])
                or codec not in SOURCE_CODECS_BY_RES.get(sr, ("h264",))):
            return False
        if codec == "hdr":
            publish(selected_input=codec, selected_subs=False)  # HDR + burn-in unsupported (v1)
        else:
            publish(selected_input=codec)
        return True


def select_subs(on):
    """Toggle subtitle burn-in (streaming-mode realism modifier). Not combinable with the HDR
    source (untested combined chain — v1 restriction) and cleared when leaving streaming mode."""
    with RUN_LOCK:
        with STATE_LOCK:
            cur = STATE.get("ui")
            mode = STATE.get("selected_mode", "streaming")
            inp = STATE.get("selected_input", "hevc")
        if cur != "idle":
            return False
        if on and (mode != "streaming" or inp == "hdr"):
            return False
        publish(selected_subs=bool(on))
        return True


def select_source_res(res):
    """Pick the source resolution (advanced). Resets input codec + target res to valid defaults."""
    with RUN_LOCK:
        with STATE_LOCK:
            cur = STATE.get("ui")
        if cur != "idle" or res not in SOURCE_RES:
            return False
        codecs = SOURCE_CODECS_BY_RES[res]
        new_input = "hevc" if (res == "4k" and "hevc" in codecs) else codecs[0]
        tgts = target_res_options(res)
        new_target = "1080p" if "1080p" in tgts else tgts[0]
        publish(selected_source_res=res, selected_input=new_input, selected_target_res=new_target)
        return True


def select_target_res(res):
    """Pick the output resolution (advanced). Only resolutions <= the source are valid."""
    with RUN_LOCK:
        with STATE_LOCK:
            cur = STATE.get("ui")
            sr = STATE.get("selected_source_res", "4k")
        if cur != "idle" or res not in target_res_options(sr):
            return False
        publish(selected_target_res=res)
        return True


def select_mode(mode):
    """Switch test mode. streaming = 'how many at once' (locks 4K->1080p); convert = 'how fast'
    (full resolution matrix). Switching to streaming resets resolution to the canonical."""
    with RUN_LOCK:
        with STATE_LOCK:
            cur = STATE.get("ui")
        if cur != "idle" or mode not in ("streaming", "convert"):
            return False
        if mode == "streaming":
            publish(selected_mode=mode, selected_source_res="4k", selected_target_res="1080p")
        else:
            publish(selected_mode=mode, selected_subs=False)   # burn-in is streaming-only
        return True


def _custom_by_name(name):
    with STATE_LOCK:
        for f in STATE.get("custom_files", []):
            if f["name"] == name:
                return f
    return None


def select_custom(name):
    """Pick a file from the BYO drop folder as the source (or clear it with ''/None to go back to
    the shipped clips). Only when idle, the file exists, and the GPU can hardware-decode it."""
    with RUN_LOCK:
        with STATE_LOCK:
            cur = STATE.get("ui")
            idx = STATE.get("selected_idx")
        if cur != "idle":
            return False
        if not name:
            publish(selected_custom=None)
            return True
        f = _custom_by_name(name)
        g = _gpu_by_idx(idx)
        if not f or not g:
            return False
        if f.get("codec") not in g.get("decodes", ["hevc"]):
            return False    # GPU can't hw-decode it — or ffprobe couldn't identify the codec
                            # (an unidentifiable file can't be decode-gated, so it can't run;
                            # a permissive default here + a strict one in start_run used to
                            # silently swap the shipped clip in for the user's file)
        publish(selected_custom=name)
        return True


def start_run():
    """Kick off a benchmark on the selected GPU + input/output codecs + resolutions. Idle only."""
    with RUN_LOCK:
        with STATE_LOCK:
            cur = STATE.get("ui")
            idx = STATE.get("selected_idx")
            codec = STATE.get("selected_codec", "h264")
            input_codec = STATE.get("selected_input", "hevc")
            custom_name = STATE.get("selected_custom")
            source_res = STATE.get("selected_source_res", "4k")
            target_res = STATE.get("selected_target_res", "1080p")
            mode = STATE.get("selected_mode", "streaming")
            subs = bool(STATE.get("selected_subs"))
        if cur != "idle":
            return False
        if mode == "streaming":
            source_res, target_res = "4k", "1080p"   # streaming is always the canonical resolution
        else:
            subs = False                             # burn-in is a streaming-only modifier
        gpu = _gpu_by_idx(idx)
        if not gpu:
            gpu = default_selection(_available(_DETECTED))   # single hw GPU (or CPU-only box)
            if not gpu:
                return False
        # a chosen custom file overrides the shipped-clip source (codec + resolution come from it)
        custom_path = None
        f = _custom_by_name(custom_name) if custom_name else None
        if f:
            if f.get("codec") not in gpu.get("decodes", ["hevc"]):
                return False    # refuse — never silently run the shipped clip while the UI
                                # says the user's file was tested
            custom_path, input_codec = f["path"], f["codec"]
        # safety net: never start a combo the hardware / shipped clips can't actually do
        if source_res not in SOURCE_RES:
            source_res = "4k"
        if not custom_path and input_codec not in SOURCE_CODECS_BY_RES.get(source_res, ("h264",)):
            input_codec = "h264" if source_res == "1080p" else "hevc"
        if target_res not in target_res_options(source_res):
            target_res = "1080p" if "1080p" in target_res_options(source_res) else source_res
        if codec not in gpu.get("codecs", ["h264"]):
            codec = "h264"
        if not custom_path and input_codec not in gpu.get("decodes", ["hevc"]):
            input_codec = "hevc"
        if input_codec == "hdr":
            subs = False                             # HDR + burn-in combined is unsupported (v1)
        # auto 10-bit only when preserving resolution from a 10-bit source AND the GPU can do it
        ten_bit = (ten_bit_output(source_res, target_res, input_codec, codec)
                   and codec in gpu.get("codecs10", []) and not custom_path)
        # clear the abort flag BEFORE ui="preparing" goes out — abort_run() accepts as soon as
        # the ui flips, so clearing later (inside benchmark()) swallowed early cancels
        _ABORT.clear()
        publish(ui="preparing", phase="preparing", selected_idx=gpu["idx"],
                selected_name=gpu["name"], selected_codec=codec, selected_input=input_codec,
                selected_source_res=source_res, selected_target_res=target_res,
                selected_subs=subs, message="Starting…")
        threading.Thread(target=benchmark,
                         args=(gpu, codec, input_codec, custom_path, source_res, target_res,
                               ten_bit, mode),
                         kwargs={"subs": subs},
                         daemon=True).start()
        return True


BATCH_COOLDOWN = float(_env("BATCH_COOLDOWN", "15"))   # seconds between batch jobs (dGPUs stay
                                                       # boosted after a ramp — the NEXT run's
                                                       # idle-power baseline needs a breather)


def _batch_row(gpu, result, job=None):
    """Per-run summary for the batch table + the FULL result (tab detail views + submit-all)."""
    return {"gpu": gpu["name"], "vendor": gpu["vendor"], "is_cpu": result.get("is_cpu", False),
            "error": False, "max_sustained": result.get("max_sustained"),
            "capped": result.get("capped"), "projected": result.get("projected"),
            "single_stream": result.get("single_stream"),
            "peak_combined": result.get("peak_combined"),
            "rec_workers": result.get("rec_workers"),
            "watts_per_stream": result.get("watts_per_stream"),
            "peak_power_w": result.get("peak_power_w"),
            "power_estimated": result.get("power_estimated"),
            "ten_bit": result.get("ten_bit"),
            "profile": result.get("profile"), "input_codec": result.get("input_codec"),
            "subs": bool(result.get("subs_burn")),
            "comparable": bool(result.get("comparable")),
            "submitted": False, "full": result}


def _job_label(job, codec, source_res, target_res, mode):
    p = profile_label(source_res, job["input_codec"], target_res, codec, False,
                      job["subs"], False)
    return f"{job['gpu']['name']} · {p}"


def batch_run(jobs, codec, source_res, target_res, mode):
    """Run a batch of jobs [{gpu, input_codec, subs}] STRICTLY sequentially with a cool-down
    between runs (concurrent devices contend for CPU feeding, PCIe and RAM bandwidth and ruin
    power attribution — sequential is the only honest mode). Skips the interactive busy gate
    (would stall a batch); a mid-run cancel aborts the whole batch; a per-job error records a
    row and moves on."""
    rows = []
    try:
        for i, job in enumerate(jobs):
            # an abort set between jobs (after the previous finalize, before the next clear)
            # used to be swallowed by benchmark()'s entry clear — catch it here instead
            if _ABORT.is_set():
                _publish_cancelled()
                publish(batch=False, batch_queue=[], batch_done=0, batch_results=[],
                        batch_skipped=[], submitted=False)
                return
            label = _job_label(job, codec, source_res, target_res, mode)
            if i > 0 and BATCH_COOLDOWN > 0:
                publish(message=f"Cooling down {BATCH_COOLDOWN:.0f}s before run "
                                f"{i + 1} of {len(jobs)}…")
                if _ABORT.wait(BATCH_COOLDOWN):
                    _publish_cancelled()
                    publish(batch=False, batch_queue=[], batch_done=0, batch_results=[],
                            batch_skipped=[], submitted=False)
                    return
            publish(batch_done=i, message=f"Run {i + 1} of {len(jobs)}: {label}…")
            gpu = job["gpu"]
            ten_bit = (ten_bit_output(source_res, target_res, job["input_codec"], codec)
                       and codec in gpu.get("codecs10", []))
            benchmark(gpu, codec, job["input_codec"], None, source_res, target_res, ten_bit,
                      mode, skip_busy_warn=True, announce_done=False, subs=job["subs"])
            with STATE_LOCK:
                ui, result = STATE.get("ui"), STATE.get("result")
            if ui == "idle":                       # cancelled mid-batch → back to the picker
                publish(batch=False, batch_queue=[], batch_done=0, batch_results=[],
                        batch_skipped=[], submitted=False)
                return
            if ui == "error" or not result:
                rows.append({"gpu": gpu["name"], "vendor": gpu["vendor"], "error": True,
                             "profile": label.split(" · ", 1)[-1]})
                publish(ui="running", phase="preparing")   # keep the batch going
            else:
                rows.append(_batch_row(gpu, result, job))
            publish(batch_results=list(rows), batch_done=i + 1)
        publish(ui="done", phase="done", batch_results=rows,
                message=f"Batch complete — {len(rows)} run(s), summary below.")
    except Exception as e:
        print(f"[batch] error: {e}", flush=True)
        publish(ui="error", phase="error", batch=False, message=f"Batch error: {e}")


def start_batch(device_idxs=None, kind="current", codec_override=None, sources=None):
    """Kick off a batch: kind "sweep" = every supported 4K source per selected device at an
    output the PANEL chooses (codec_override — the sweep's output is deliberately independent
    of the main selection, whose dropdown is gated by the currently selected card; the batch
    group may well include cards that can encode more). kind "current" = the current selection
    across the selected devices (override ignored — "current" means current). device_idxs
    None ⇒ all available. Idle only; shipped clips only (a custom file can't be decoded
    everywhere so cross-device comparison would be apples-to-oranges)."""
    with RUN_LOCK:
        with STATE_LOCK:
            cur = STATE.get("ui")
            codec = STATE.get("selected_codec", "h264")
            input_codec = STATE.get("selected_input", "hevc")
            source_res = STATE.get("selected_source_res", "4k")
            target_res = STATE.get("selected_target_res", "1080p")
            mode = STATE.get("selected_mode", "streaming")
            custom = STATE.get("selected_custom")
            subs = bool(STATE.get("selected_subs"))
        if cur != "idle" or custom:
            return False
        if mode == "streaming":
            source_res, target_res = "4k", "1080p"
        else:
            subs = False
        if kind == "sweep":
            source_res = "4k"                     # sweeps cover the 4K (leaderboard) source set
            if target_res not in target_res_options("4k"):
                target_res = "1080p"
            if codec_override in ("h264", "hevc", "av1"):
                codec = codec_override
            if sources:
                sources = [c for c in sources if c in SOURCE_CODECS_BY_RES["4k"]] or None
        avail = _available(_DETECTED)
        if device_idxs is not None:
            avail = [g for g in avail if g["idx"] in device_idxs]
        jobs, skipped = build_batch_jobs(avail, kind, codec, subs, input_codec,
                                         sources if kind == "sweep" else None)
        if not jobs:
            return False
        labels = [_job_label(j, codec, source_res, target_res, mode) for j in jobs]
        _ABORT.clear()      # before ui flips — same early-cancel window as start_run
        publish(ui="preparing", phase="preparing", batch=True,
                batch_queue=labels, batch_done=0, batch_results=[],
                batch_skipped=skipped, submitted=False,
                message=f"Batch: {len(jobs)} run(s) queued…")
        threading.Thread(target=batch_run,
                         args=(jobs, codec, source_res, target_res, mode),
                         daemon=True).start()
        return True


def reset_run():
    """Return to Ready/idle and clear the previous run. Only from done/error."""
    with RUN_LOCK:
        with STATE_LOCK:
            cur = STATE.get("ui")
        if cur not in ("done", "error"):
            return False
        stop_all()
        clear_ramdisk()
        publish(ui="idle", phase="idle", message="Ready.",
                source_ready=source_ready(), encoder=None,
                selected_custom=None,
                selected_name=None, selected_input="hevc", selected_codec="h264",
                selected_source_res="4k", selected_target_res="1080p",
                vendor=None, driver=None,
                kernel_driver=None,
                is_igpu=False, cpu=None, ram_speed=None, ram_type=None, ram_hint=None,
                telemetry={}, streams_per_watt=None, power_estimated=False, result=None,
                busy_load=None, busy_apps=[], busy_named=False,
                stream_count=0, streams=[], min_speed=None, avg_speed=None,
                combined_speed=None, last_passing=0, confirmed_max=None,
                single_stream_speed=None, conv_levels=[], conv_testing_n=None, vram_note=None,
                projected=None, cap_reason=None,
                projected_uncapped=None,
                history=[], history_delta=None,
                batch=False, batch_queue=[], batch_done=0, batch_results=[], batch_skipped=[], submitted=False)
    # re-scan the drop folder OUTSIDE the lock — ffprobe over a slow/spun-down network mount
    # can take minutes, and every POST endpoint waits on RUN_LOCK (the UI froze on Run Again)
    scan = list_custom_files()
    publish(custom_files=scan["files"], custom_library=scan["library"])
    return True


def continue_run():
    """Proceed past the busy-GPU warning. Only valid while in the warn state."""
    with STATE_LOCK:
        if STATE.get("ui") != "warn":
            return False
    _CONTINUE.set()
    return True


def cancel_run():
    """Abort at the busy-GPU warning and return to the picker."""
    with STATE_LOCK:
        if STATE.get("ui") != "warn":
            return False
    _CANCEL.set()
    return True


def abort_run():
    """Cancel an in-progress run (during clip prep or the ramp). Only valid while
    preparing/running; the run thread sees the flag within ~1s and returns to the picker."""
    with STATE_LOCK:
        if STATE.get("ui") not in ("preparing", "running"):
            return False
    _ABORT.set()
    return True


def load_or_create_install_id(path=INSTALL_ID_FILE):
    """Random per-install UUID for submission dedup (resubmits UPDATE instead of stacking).
    Deliberately random — never derived from hardware (no fingerprinting). Persisted best-effort;
    an unwritable /config degrades to a session-ephemeral id, never raises."""
    try:
        with open(path) as f:
            val = f.read().strip()
        if val:
            return val
    except Exception:
        pass
    val = str(uuid.uuid4())
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            f.write(val)
        os.replace(tmp, path)
    except Exception:
        pass
    return val


def submission_envelope(result, install_id, ts=None):
    """The versioned envelope the leaderboard server accepts — see
    docs/superpowers/specs/2026-07-07-leaderboard-submission-contract.md (server validates against
    the SAME document). The result dict goes in verbatim."""
    return {"schema": SUBMIT_SCHEMA, "install_id": install_id,
            "submitted_at": (ts if ts is not None else int(time.time())), "result": result}


def _post_submission(result):
    """POST one result's envelope. Returns (ok, reason) — reason is the SERVER'S rejection
    message when available (a bare "HTTP 400" told the user, and us, nothing)."""
    if not result.get("comparable"):
        return False, "not comparable"    # never submit local-only runs, whatever the UI says
    try:
        data = json.dumps(submission_envelope(result, load_or_create_install_id())).encode()
        req = urllib.request.Request(SUBMIT_URL, data=data,
                                     headers={"Content-Type": "application/json",
                                              # Cloudflare's browser-integrity check 403s the
                                              # default Python-urllib UA — identify honestly
                                              "User-Agent": f"gpu-benchmark/{TOOL_VERSION}"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                return True, ""
        return False, "rejected by the server"
    except urllib.error.HTTPError as e:
        try:
            reason = json.loads(e.read()[:1024]).get("error", "")
        except Exception:
            reason = ""
        return False, reason or str(e)
    except Exception as e:
        return False, str(e)


def submit_result():
    """Submit the current single-run result (UI gates the button on comparable)."""
    with STATE_LOCK:
        result = STATE.get("result")
        already = STATE.get("submitted")
        if SUBMIT_URL and result and not already:
            # mark inside the lock — two rapid /submit posts both saw submitted=False and
            # both POSTed (the server upsert masked it, but the race is ours to close)
            STATE["submitted"] = True
        else:
            return False
    ok, reason = _post_submission(result)
    if ok:
        publish(submitted=True,
                message="Submitted — thank you! New results appear on the leaderboard within about an hour.")
        return True
    publish(submitted=False,
            message=f"Submit rejected: {reason}" if reason else "Submit failed.")
    return False


_SUBMITTING_BATCH = threading.Event()


def _submit_batch_thread():
    """Submit every retained comparable batch result, one per second, updating each row's
    submitted/submit_error so the table shows per-row ✓/✗ live."""
    try:
        with STATE_LOCK:
            rows = [dict(r) for r in STATE.get("batch_results", [])]
        sent = 0
        for i, row in enumerate(rows):
            with STATE_LOCK:
                if STATE.get("ui") != "done":
                    return                # /reset mid-flight — stop; don't resurrect the
                                          # cleared batch rows onto the fresh picker state
            full = row.get("full")
            if not full or not full.get("comparable") or row.get("submitted"):
                continue
            ok, reason = _post_submission(full)
            row["submitted"] = ok
            if not ok:
                row["submit_error"] = reason
            else:
                sent += 1
            rows[i] = row
            with STATE_LOCK:
                still_done = STATE.get("ui") == "done"
            if not still_done:
                return
            publish(batch_results=list(rows))
            time.sleep(1)                 # gentle on the shared rate limit
        with STATE_LOCK:
            if STATE.get("ui") != "done":
                return
        publish(batch_results=rows,
                message=(f"Submitted {sent} result(s) — thank you! They appear on the "
                         f"leaderboard within about an hour." if sent else "Nothing new to submit."))
    finally:
        _SUBMITTING_BATCH.clear()


def submit_batch():
    """POST every comparable result from the finished batch (button under the summary table)."""
    with STATE_LOCK:
        cur = STATE.get("ui")
        rows = STATE.get("batch_results", [])
    if cur != "done" or not SUBMIT_URL or _SUBMITTING_BATCH.is_set():
        return False
    if not any(r.get("full", {}).get("comparable") and not r.get("submitted") for r in rows):
        return False
    _SUBMITTING_BATCH.set()
    threading.Thread(target=_submit_batch_thread, daemon=True).start()
    return True


# --------------------------------------------------------------- gpu telemetry
# latest live telemetry for the GPU under test: {util, enc, temp, power, clock, engines}
TELEMETRY = {}
TELE_LOCK = threading.Lock()
_TELE_STOP = threading.Event()
_TELE_GEN = 0          # bumped by start_telemetry; a sampler from a previous run that was
                       # mid-sample when the stop flag flipped (nvidia-smi can block ~6 s)
                       # must not write stale readings into the NEXT run's telemetry
_RAPL_ACTIVE = False   # RAPL owns power_pkg this run (intel_gpu_top must not fight it)


def _tele_set(gen=None, **kw):
    with TELE_LOCK:
        if gen is not None and gen != _TELE_GEN:
            return                     # stale sampler from a previous run — drop the write
        TELEMETRY.update(kw)
        snap = dict(TELEMETRY)
    publish(telemetry=snap)


def telemetry_power():
    """Latest GPU power reading (watts) or None — for streams-per-watt accumulation."""
    with TELE_LOCK:
        return TELEMETRY.get("power")


def telemetry_power_pkg():
    """Latest CPU-package power (watts) or None — basis for the iGPU power estimate."""
    with TELE_LOCK:
        return TELEMETRY.get("power_pkg")


def _rapl_telemetry(paths, gen, with_temp=False):
    """Vendor-agnostic CPU package power from mounted RAPL counters (Intel + AMD Zen 2+):
    sample energy_uj per package at 1 Hz, sum the deltas → watts → power_pkg. Same field the
    intel_gpu_top path fills, so everything downstream (idle-delta estimate, watts_per_stream,
    CPU baseline, vs-CPU block) is untouched — just a better sensor source."""
    def _int(s):
        try:
            return int(s)
        except (TypeError, ValueError):
            return None
    maxes = {p: _int(_read(os.path.join(p, "max_energy_range_uj"))) for p in paths}
    prev, prev_t = {}, None
    while not _TELE_STOP.is_set() and gen == _TELE_GEN:
        now = time.monotonic()
        total, ok = 0, False
        for p in paths:
            cur = _int(_read(os.path.join(p, "energy_uj")))
            d = rapl_delta_uj(prev.get(p), cur, maxes.get(p))
            if d is not None:
                total += d
                ok = True
            prev[p] = cur
        if ok and prev_t is not None:
            w = rapl_watts(total, now - prev_t)
            if w is not None:
                kw = {"power_pkg": w}
                if with_temp:                      # CPU device on a non-Intel box has no
                    kw["temp"] = read_cpu_package_temp()   # other temp source (k10temp path)
                _tele_set(gen, **kw)
        prev_t = now
        _TELE_STOP.wait(1.0)


def _nvidia_telemetry(gpu, gen):
    """Full telemetry via `nvidia-smi -q -x`: util, enc/dec %, temp, power, clock, throttle,
    and the process list (stashed under TELEMETRY['_procs'] for the active-apps check)."""
    idx = str(gpu.get("index", 0))
    while not _TELE_STOP.is_set() and gen == _TELE_GEN:
        try:
            xml = subprocess.run(["nvidia-smi", "-i", idx, "-q", "-x"],
                                 capture_output=True, text=True, timeout=6).stdout
            d = parse_nvidia_xml(xml)
            if d:
                with TELE_LOCK:
                    if gen != _TELE_GEN:
                        break
                    TELEMETRY["_procs"] = d.pop("procs", [])
                _tele_set(gen, **{k: v for k, v in d.items() if v is not None})
        except Exception:
            pass
        _TELE_STOP.wait(1.0)


def _amd_engine_totals(pci):
    """Sum VCN encode/decode ns across our own ffmpeg streams' DRM clients on this card.
    Reads /proc/<pid>/fdinfo (our own children — no --pid=host needed), dedupes by
    drm-client-id (a client can hold many fds), and filters to this GPU's PCI address.
    Returns (enc_ns, dec_ns, comp_ns, enc_cap, dec_cap)."""
    with _REG_LOCK:
        pids = [s.proc.pid for s in _ALL_STREAMS if s.proc is not None]
    enc = dec = comp = 0
    enc_cap = dec_cap = 1
    seen = set()
    for pid in pids:
        for fi in glob.glob(f"/proc/{pid}/fdinfo/*"):
            d = parse_amd_engines(_read(fi) or "")
            if not d or (pci and d["pdev"] and d["pdev"] != pci):
                continue
            cid = (pid, d["client"])
            if cid in seen:
                continue                       # same client via a duplicate fd
            seen.add(cid)
            enc += d["enc_ns"]
            dec += d["dec_ns"]
            comp += d["comp_ns"]
            enc_cap = max(enc_cap, d["enc_cap"])
            dec_cap = max(dec_cap, d["dec_cap"])
    return enc, dec, comp, enc_cap, dec_cap


def gpu_mem_ours(gpu):
    """VRAM (MB) held by OUR ffmpeg streams on this GPU — the slope source. Per-process
    attribution so co-tenant allocations (ollama etc.) can't pollute the per-session cost.
    NVIDIA: nvidia-smi per-compute-app; AMD: drm-memory-vram from our children's fdinfo."""
    with _REG_LOCK:
        pids = {s.proc.pid for s in _ALL_STREAMS if s.proc is not None}
    if not pids:
        return None
    try:
        if gpu["vendor"] == "nvidia":
            args = ["nvidia-smi"]
            if gpu.get("index") is not None:
                args += ["-i", str(gpu["index"])]
            out = subprocess.run(args + ["--query-compute-apps=pid,used_memory",
                                         "--format=csv,noheader,nounits"],
                                 capture_output=True, text=True, timeout=8).stdout
            total = 0.0
            for line in out.splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 2 and parts[0].isdigit() and int(parts[0]) in pids:
                    total += float(parts[1])
            return round(total, 1)
        if gpu["vendor"] == "amd":
            total, seen = 0.0, set()
            for pid in pids:
                for fi in glob.glob(f"/proc/{pid}/fdinfo/*"):
                    txt = _read(fi) or ""
                    if "drm-driver" not in txt:
                        continue
                    kv = {}
                    for line in txt.splitlines():
                        if ":" in line:
                            k, _, v = line.partition(":")
                            kv[k.strip()] = v.strip()
                    if kv.get("drm-driver") != "amdgpu":
                        continue
                    if gpu.get("pci") and kv.get("drm-pdev") and kv["drm-pdev"] != gpu["pci"]:
                        continue
                    cid = (pid, kv.get("drm-client-id"))
                    if cid in seen:
                        continue
                    seen.add(cid)
                    m = re.search(r"(\d+)", kv.get("drm-memory-vram", "0"))
                    if m:
                        total += int(m.group(1)) / 1024.0      # KiB -> MB
            return round(total, 1)
    except Exception:
        pass
    return None


def gpu_mem_global(gpu):
    """(used_mb, total_mb) for the whole card — the HEADROOM side, where co-tenant usage
    SHOULD count (it genuinely eats the ceiling). None when unavailable."""
    try:
        if gpu["vendor"] == "nvidia":
            args = ["nvidia-smi"]
            if gpu.get("index") is not None:
                args += ["-i", str(gpu["index"])]
            out = subprocess.run(args + ["--query-gpu=memory.used,memory.total",
                                         "--format=csv,noheader,nounits"],
                                 capture_output=True, text=True, timeout=8).stdout
            parts = [p.strip() for p in out.strip().splitlines()[0].split(",")]
            return float(parts[0]), float(parts[1])
        if gpu["vendor"] == "amd":
            base = os.path.basename(gpu["device"])
            used = _read(f"/sys/class/drm/{base}/device/mem_info_vram_used")
            total = _read(f"/sys/class/drm/{base}/device/mem_info_vram_total")
            if used and total:
                return int(used) / 1048576.0, int(total) / 1048576.0
    except Exception:
        pass
    return None


def _amd_telemetry(gpu, gen):
    pci = gpu.get("pci")
    busy = f"/sys/class/drm/{os.path.basename(gpu['device'])}/device/gpu_busy_percent"
    hwmon = glob.glob(f"/sys/bus/pci/devices/{pci}/hwmon/*/") if pci else []
    hw = hwmon[0] if hwmon else None
    prev_enc = prev_dec = prev_comp = None
    prev_t = None
    while not _TELE_STOP.is_set() and gen == _TELE_GEN:
        try:
            util = _f(_read(busy))                            # GFX pipe (kept for the busy gate)
            power = temp = pmax = None
            if hw:
                pw = _read(hw + "power1_average") or _read(hw + "power1_input")
                if pw is not None:
                    power = round(_f(pw) / 1_000_000.0, 1)   # µW -> W
                pc = _read(hw + "power1_cap")
                if pc is not None:
                    pmax = round(_f(pc) / 1_000_000.0, 1)
                tp = _read(hw + "temp1_input")
                if tp is not None:
                    temp = round(_f(tp) / 1000.0, 1)         # m°C -> °C
            # Real media load from our own ffmpeg's VCN fdinfo counters, sampled as a delta.
            # This driver lumps HEVC decode + H.264 encode onto the single `enc` ring (no
            # separate `dec`), with scale_vaapi on `compute` — so, like Intel, we surface one
            # combined "Video" engine plus a "Video Scaler", not split encoder/decoder.
            engines = None
            e_ns, d_ns, c_ns, e_cap, d_cap = _amd_engine_totals(pci)
            now = time.monotonic()
            if prev_t is not None:
                dt = now - prev_t
                vcn = engine_pct(max(e_ns - prev_enc, d_ns - prev_dec), dt, max(e_cap, d_cap))
                scaler = engine_pct(c_ns - prev_comp, dt, 1)
                if vcn is not None:
                    engines = {"Video": vcn}
                    if scaler is not None:
                        engines["Video Scaler"] = scaler
            prev_enc, prev_dec, prev_comp, prev_t = e_ns, d_ns, c_ns, now
            _tele_set(gen, util=util, power=power, power_max=pmax, temp=temp, engines=engines)
        except Exception:
            pass
        _TELE_STOP.wait(1.0)


def _cpu_telemetry(gpu, gen):
    """CPU-device sampler: whole-box CPU busy% from /proc/stat deltas at 1 Hz, plus package
    temp. The iGPU engine tiles are meaningless for a software transcode (0% by definition) —
    the load that matters IS the CPU. Package power comes from the RAPL thread when /powercap
    is mounted (it also owns temp then), else the intel_gpu_top power-only fallback."""
    prev = None
    while not _TELE_STOP.is_set() and gen == _TELE_GEN:
        cur = parse_proc_stat(_read("/proc/stat"))
        kw = {"cpu_load": cpu_stat_pct(prev, cur)}
        prev = cur
        if not _RAPL_ACTIVE:
            kw["temp"] = read_cpu_package_temp()
        _tele_set(gen, **kw)
        _TELE_STOP.wait(1.0)


def _intel_telemetry(gpu, gen, power_only=False):
    """Parse intel_gpu_top -J: engine %, frequency, GPU/Package power; temp from CPU package.
    No -d (the slot form is malformed for the shipped build and suppresses all output);
    intel_gpu_top defaults to the Intel GPU, which is what we want. power_only publishes just
    power_pkg — the CPU-device fallback on Intel boxes without the /powercap mount, where the
    iGPU engines/clock would be noise on a software-transcode run."""
    cmd = ["intel_gpu_top", "-J", "-s", "1000"]
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    except Exception:
        return
    buf, depth, started = [], 0, False
    try:
        # line-based brace-depth scan (intel_gpu_top -J pretty-prints one token per line);
        # a char-at-a-time Python loop burns measurable CPU on the box being benchmarked.
        for line in p.stdout:
            if _TELE_STOP.is_set() or gen != _TELE_GEN:
                break
            opens, closes = line.count("{"), line.count("}")
            if opens:
                started = True
            if started:
                buf.append(line)
            depth += opens - closes
            if started and depth == 0:
                try:
                    text = "".join(buf)
                    # trim array/comma framing around the object ("[{", "},") to bare {...}
                    obj = json.loads(text[text.find("{"):text.rfind("}") + 1])
                    eng = {n: round(v.get("busy", 0.0), 1)
                           for n, v in obj.get("engines", {}).items()}
                    pw = obj.get("power", {})
                    freq = obj.get("frequency", {})
                    kw = dict(engines=eng,
                              power=(round(pw.get("GPU"), 1) if pw.get("GPU") else None),
                              clock=(round(freq.get("actual"), 0) if freq.get("actual") else None),
                              temp=read_cpu_package_temp())
                    if not _RAPL_ACTIVE:   # RAPL owns power_pkg when the mount is present
                        kw["power_pkg"] = (round(pw.get("Package"), 1)
                                           if pw.get("Package") else None)
                    if power_only:
                        kw = {"power_pkg": kw.get("power_pkg")}
                    _tele_set(gen, **kw)
                except Exception:
                    pass
                buf, started = [], False
    except Exception:
        pass
    finally:
        try:
            p.terminate()
            p.wait(timeout=5)           # reap intel_gpu_top — no zombie per run
        except Exception:
            pass


# ----------------------------------------------------- active-apps detection
BUSY_THRESHOLD = float(_env("BUSY_THRESHOLD", "12"))   # % engine load that counts as "in use"


def host_pid_visible():
    """True if /proc shows host processes (i.e. the container has --pid=host)."""
    try:
        return len(glob.glob("/proc/[0-9]*")) > 50
    except Exception:
        return False


def gpu_clients(gpu):
    """Names of OTHER processes currently using this GPU ([] if none/can't tell).
    VAAPI uses /proc/<pid>/fdinfo (needs host-PID visibility); NVIDIA uses the
    telemetry process list. Our own ffmpeg/python are filtered out."""
    names = []
    if gpu["vendor"] == "nvidia":
        with TELE_LOCK:
            names = list(TELEMETRY.get("_procs", []))
    elif host_pid_visible():
        pdev = gpu.get("pci")
        for d in glob.glob("/proc/[0-9]*"):
            try:
                for fi in glob.glob(d + "/fdinfo/*"):
                    info = parse_fdinfo(_read(fi) or "")
                    if info and (pdev is None or info.get("pdev") == pdev):
                        names.append(_read(d + "/comm") or "?")
                        raise StopIteration
            except StopIteration:
                continue
            except Exception:
                continue
    # the busy check runs BEFORE we launch any of our own ffmpeg, so any ffmpeg seen
    # here is a contending app (Emby/Plex/Jellyfin all transcode via ffmpeg) — report it.
    # Only filter our own telemetry helpers.
    bad = ("intel_gpu_top", "nvidia-smi")
    return sorted({n for n in names if n and not any(b in n.lower() for b in bad)})


def gpu_busy(gpu):
    """(is_busy, load%) from current telemetry engine/util before the ramp."""
    with TELE_LOCK:
        tel = dict(TELEMETRY)
    if gpu["vendor"] == "cpu":
        # the CPU baseline is skewed by OTHER CPU work, not GPU engines — check loadavg
        try:
            load = cpu_load_pct(os.getloadavg()[0], os.cpu_count())
        except Exception:
            load = None
        return ((load or 0.0) >= BUSY_THRESHOLD, round(load or 0.0, 1))
    if gpu["vendor"] == "intel":
        eng = tel.get("engines", {})
        load = max([v for n, v in eng.items() if "Video" in n] or [0.0])
    elif gpu["vendor"] == "nvidia":
        load = max(tel.get("enc") or 0.0, tel.get("util") or 0.0)
    else:  # amd — VCN "Video" engine occupancy, falling back to GFX busy% (util)
        eng = tel.get("engines") or {}
        load = max([v for n, v in eng.items() if "Video" in n] + [tel.get("util") or 0.0])
    return (load >= BUSY_THRESHOLD, round(load, 1))


def start_telemetry(gpu):
    """Launch the per-vendor telemetry sampler for the GPU under test, plus the vendor-agnostic
    RAPL CPU-power sampler whenever the /powercap mount is present (it then owns power_pkg)."""
    global _RAPL_ACTIVE, _TELE_GEN
    _TELE_STOP.clear()
    with TELE_LOCK:
        _TELE_GEN += 1                 # orphan any sampler still draining from the last run
        gen = _TELE_GEN
        TELEMETRY.clear()
    rapl = rapl_package_paths()
    _RAPL_ACTIVE = bool(rapl)
    target = {"nvidia": _nvidia_telemetry, "amd": _amd_telemetry,
              "cpu": _cpu_telemetry}.get(gpu["vendor"], _intel_telemetry)
    threading.Thread(target=target, args=(gpu, gen), daemon=True).start()
    if gpu["vendor"] == "cpu" and not rapl:
        # package-power fallback: Intel box with PERFMON but no /powercap mount
        threading.Thread(target=_intel_telemetry, args=(gpu, gen, True), daemon=True).start()
    if rapl:
        threading.Thread(target=_rapl_telemetry, args=(rapl, gen, gpu["vendor"] == "cpu"),
                         daemon=True).start()


def stop_telemetry():
    _TELE_STOP.set()


# ------------------------------------------------------------------ web server
def origin_ok(origin, host):
    """CSRF guard for mutating POSTs. A malicious page open in a LAN browser can fire a
    cross-origin POST at this box; the browser stamps such a request with an Origin header whose
    host won't match ours. Same-origin page requests either omit Origin or match it, and
    non-browser clients (curl) omit it entirely. So: reject ONLY when Origin is present and its
    host differs from our Host. (Our POST endpoints take query params with no body — the
    'simple request' class CORS never preflights — so this header check is the real defence.)"""
    if not origin:
        return True
    try:
        oh = urllib.parse.urlparse(origin).netloc
    except Exception:
        return False
    return bool(oh) and oh == host


def _query_str(path, key):
    """Query param value, URL-DECODED — real media filenames carry spaces/parens, which the
    browser sends percent-encoded; matching them un-decoded silently rejects every selection."""
    if "?" not in path:
        return None
    for pair in path.split("?", 1)[1].split("&"):
        if "=" in pair:
            k, v = pair.split("=", 1)
            if k == key:
                return urllib.parse.unquote(v)
    return None


def _query_int(path, key):
    v = _query_str(path, key)
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


class Handler(BaseHTTPRequestHandler):
    # idle-socket timeout: without one, a client that connects and sends nothing pins a
    # daemon thread forever (ThreadingHTTPServer spawns one per connection)
    timeout = 30

    def log_message(self, *a):
        pass

    # defence-in-depth headers for the local UI (no known XSS — strings are escaped — but these
    # cost nothing). CSP allows inline script/style because scoreboard.html is self-contained;
    # frame-ancestors none + X-Frame-Options block clickjacking (pairs with the CSRF guard).
    _SEC_HEADERS = {
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "X-Frame-Options": "DENY",
        "Content-Security-Policy": (
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"),
    }

    def _send(self, body, ctype, code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        for k, v in self._SEC_HEADERS.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/state.json":
            with STATE_LOCK:
                body = json.dumps(STATE).encode()
            self._send(body, "application/json")
        else:
            try:
                with open(SCOREBOARD, "rb") as f:
                    body = f.read()
                self._send(body, "text/html; charset=utf-8")
            except Exception:
                self._send(b"scoreboard.html missing", "text/plain", 500)

    def _dispatch(self, path):
        """Exact-path POST routing (no startswith aliasing / order dependence). Returns ok, or
        None if the route emitted its own response already."""
        p = self.path                                       # full path incl. query
        if path == "/select":
            idx = _query_int(p, "i"); return select_gpu(idx) if idx is not None else False
        if path == "/codec":
            c = _query_str(p, "c"); return select_codec(c) if c else False
        if path == "/input":
            c = _query_str(p, "c"); return select_input(c) if c else False
        if path == "/sres":
            r = _query_str(p, "r"); return select_source_res(r) if r else False
        if path == "/tres":
            r = _query_str(p, "r"); return select_target_res(r) if r else False
        if path == "/mode":
            m = _query_str(p, "m"); return select_mode(m) if m else False
        if path == "/custom":
            return select_custom(_query_str(p, "f") or "")
        if path == "/subs":
            return select_subs(_query_str(p, "b") == "1")
        if path == "/fetchclips":
            return start_fetch_clips()
        if path == "/startall":                             # legacy route — current-kind batch
            return start_batch(None, "current")
        if path == "/batch":
            d = _query_str(p, "d")
            idxs = None
            if d:
                try:
                    idxs = [int(x) for x in d.split(",") if x != ""]
                except ValueError:
                    self._send(json.dumps({"ok": False}).encode(), "application/json")
                    return None                             # a garbled device list ≠ all devices
            srcs = _query_str(p, "srcs")
            return start_batch(idxs, _query_str(p, "kind") or "current",
                               _query_str(p, "codec"), srcs.split(",") if srcs else None)
        if path == "/submitbatch":
            return submit_batch()
        if path == "/start":
            return start_run()
        if path == "/continue":
            return continue_run()
        if path == "/cancel":
            return cancel_run()
        if path == "/abort":
            return abort_run()
        if path == "/reset":
            return reset_run()
        if path == "/submit":
            return submit_result()
        self._send(b'{"error":"not found"}', "application/json", 404)
        return None

    def do_POST(self):
        # CSRF: reject cross-origin browser POSTs (see origin_ok). Non-browser clients unaffected.
        if not origin_ok(self.headers.get("Origin"), self.headers.get("Host")):
            self._send(b'{"error":"cross-origin request refused"}', "application/json", 403)
            return
        path = urllib.parse.urlparse(self.path).path
        ok = self._dispatch(path)
        if ok is None:
            return                                          # the route sent its own response
        with STATE_LOCK:
            ui = STATE.get("ui")
        self._send(json.dumps({"ok": ok, "ui": ui}).encode(), "application/json")


def main():
    global _DETECTED
    signal.signal(signal.SIGTERM, lambda *_: (stop_all(), sys.exit(0)))
    _DETECTED = detect_gpus()
    avail = _available(_DETECTED)
    msg = "Ready." if avail else ("No testable GPU found. Pass /dev/dri (Intel/AMD) "
                                  "and/or --runtime=nvidia (NVIDIA).")
    # Boot to Ready/idle — user picks a GPU and triggers the run; nothing auto-starts.
    scan = list_custom_files()       # enumerate any BYO clips in the /input drop folder
    clips = clips_status()
    publish(ui="idle", phase="idle", message=msg, source_ready=source_ready(),
            temp_unit=parse_display_unit(_read(DYNAMIX_CFG)),
            clips=clips, clips_shipped=all(c["status"] == "shipped" for c in clips),
            gpus=public_gpus(_DETECTED), submit_url_set=bool(SUBMIT_URL),
            # public leaderboard page = the submit endpoint's origin (for the View button)
            board_url=(SUBMIT_URL.split("/api/")[0] if SUBMIT_URL else None),
            custom_files=scan["files"], custom_library=scan["library"],
            source_res_options=list(SOURCE_RES),
            source_codecs_by_res={r: list(c) for r, c in SOURCE_CODECS_BY_RES.items()},
            target_res_by_source={r: target_res_options(r) for r in SOURCE_RES},
            selected_idx=(default_selection(avail) or {}).get("idx"),
            selected_name=(default_selection(avail) or {}).get("name"))
    # telemetry is started per-run (it needs the chosen GPU); nothing to start here.
    httpd = ThreadingHTTPServer(("0.0.0.0", WEB_PORT), Handler)
    print(f"[scoreboard] serving on :{WEB_PORT}  (detected GPUs: "
          f"{', '.join(g['name'] for g in _DETECTED) or 'none'})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_all()


if __name__ == "__main__":
    main()
