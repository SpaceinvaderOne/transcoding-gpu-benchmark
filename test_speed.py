#!/usr/bin/env python3
"""Unit tests for the correctness-critical pure logic: progress parsing and the
instantaneous-speed calculation (the thing that must NOT use ffmpeg's cumulative
speed= field). Run: python3 test_speed.py"""
import os
import unittest
import benchmark
from benchmark import (parse_progress_kv, compute_inst_speed, is_session_cap_error,
                       smbios_mem_speeds, millideg_to_c, estimate_igpu_power,
                       parse_nvidia_xml, parse_fdinfo, gpu_busy,
                       parse_amd_engines, engine_pct, clip_master, select_input,
                       is_video_file, fit_seconds, sample_window,
                       target_res_options, source_is_10bit, ten_bit_output, is_comparable,
                       recommended_workers, throughput_saturated,
                       cpu_baseline_key, baseline_valid, efficiency_ratio, speed_ratio,
                       compute_vs_cpu, load_baseline, save_baseline,
                       default_selection, cpu_load_pct,
                       rapl_delta_uj, rapl_watts, rapl_package_paths,
                       history_for, run_delta, append_history, load_history, batch_eligible,
                       batch_skip_reason, _query_str,
                       load_or_create_install_id, submission_envelope,
                       death_reason, vram_slope, predict_wall, classify_stop)


class TestParse(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(parse_progress_kv("out_time_us=5120000"), ("out_time_us", "5120000"))
        self.assertEqual(parse_progress_kv("fps=110.5"), ("fps", "110.5"))
        self.assertEqual(parse_progress_kv("speed=4.27x"), ("speed", "4.27x"))

    def test_whitespace_and_newline(self):
        self.assertEqual(parse_progress_kv("  out_time_us = 42 \n"), ("out_time_us", "42"))

    def test_value_with_equals(self):
        # only split on the first '='
        self.assertEqual(parse_progress_kv("tag=a=b"), ("tag", "a=b"))

    def test_rejects(self):
        self.assertIsNone(parse_progress_kv(""))
        self.assertIsNone(parse_progress_kv("   "))
        self.assertIsNone(parse_progress_kv("noequalsign"))


class TestSpeed(unittest.TestCase):
    def test_realtime(self):
        # 1.0s of output produced in 1.0s of wall time => 1.0x
        self.assertAlmostEqual(compute_inst_speed(0, 0.0, 1_000_000, 1.0), 1.0)

    def test_fast(self):
        # 4.0s of output in 1.0s wall => 4.0x (race-ahead, model B)
        self.assertAlmostEqual(compute_inst_speed(0, 10.0, 4_000_000, 11.0), 4.0)

    def test_below_realtime(self):
        # 0.5s of output in 1.0s wall => 0.5x (a failing stream)
        self.assertAlmostEqual(compute_inst_speed(2_000_000, 100.0, 2_500_000, 101.0), 0.5)

    def test_zero_wall_delta_is_safe(self):
        # no wall time elapsed -> 0.0, never divides by zero
        self.assertEqual(compute_inst_speed(0, 5.0, 1_000_000, 5.0), 0.0)

    def test_negative_wall_delta_is_safe(self):
        self.assertEqual(compute_inst_speed(0, 5.0, 1_000_000, 4.0), 0.0)


class TestSessionCap(unittest.TestCase):
    def test_detects_nvenc_session_error(self):
        self.assertTrue(is_session_cap_error(
            "[h264_nvenc @ 0x..] OpenEncodeSessionEx failed: out of memory (10)"))
        self.assertTrue(is_session_cap_error("Failed to open encode session"))

    def test_ignores_normal_output(self):
        self.assertFalse(is_session_cap_error(""))
        self.assertFalse(is_session_cap_error(None))
        self.assertFalse(is_session_cap_error("frame= 100 fps=120 speed=4.2x"))


class TestSmbios(unittest.TestCase):
    def _type17(self, speed, configured, length=0x22):
        b = bytearray(length)
        b[0] = 17               # Memory Device
        b[1] = length
        b[0x15] = speed & 0xFF
        b[0x16] = (speed >> 8) & 0xFF
        b[0x20] = configured & 0xFF
        b[0x21] = (configured >> 8) & 0xFF
        return bytes(b) + b"\x00\x00"          # empty string-set

    def test_parses_configured_and_rated(self):
        end = b"\x7f\x04\x00\x00\x00\x00"       # end-of-table
        data = self._type17(2667, 2133) + end
        self.assertEqual(smbios_mem_speeds(data), (2133, 2667))

    def test_takes_max_across_dimms(self):
        end = b"\x7f\x04\x00\x00\x00\x00"
        data = self._type17(2667, 2133) + self._type17(2667, 2133) + end
        self.assertEqual(smbios_mem_speeds(data), (2133, 2667))

    def test_empty(self):
        self.assertEqual(smbios_mem_speeds(b""), (None, None))

    def _type17_typed(self, mem_type, length=0x22):
        b = bytearray(length)
        b[0] = 17
        b[1] = length
        b[0x12] = mem_type
        return bytes(b) + b"\x00\x00"

    def test_mem_type_ddr4_and_ddr5(self):
        # smbios_mem_type mints the "(DDR4)"/"(DDR5)" leaderboard ENTITY suffix — a silent
        # regression here would mis-bucket every iGPU/CPU submission on the board
        end = b"\x7f\x04\x00\x00\x00\x00"
        self.assertEqual(benchmark.smbios_mem_type(self._type17_typed(0x1A) + end), "DDR4")
        self.assertEqual(benchmark.smbios_mem_type(self._type17_typed(0x22) + end), "DDR5")

    def test_mem_type_unknown_code_and_empty(self):
        end = b"\x7f\x04\x00\x00\x00\x00"
        self.assertIsNone(benchmark.smbios_mem_type(self._type17_typed(0x99) + end))
        self.assertIsNone(benchmark.smbios_mem_type(b""))

    def test_mem_type_skips_non_17_records(self):
        end = b"\x7f\x04\x00\x00\x00\x00"
        other = bytes([4, 0x20]) + bytes(0x1E) + b"\x00\x00"   # a Type 4 (CPU) record first
        self.assertEqual(benchmark.smbios_mem_type(other + self._type17_typed(0x22) + end), "DDR5")


class TestTemp(unittest.TestCase):
    def test_millideg(self):
        self.assertEqual(millideg_to_c("36000"), 36.0)
        self.assertEqual(millideg_to_c(45500), 45.5)
    def test_millideg_bad(self):
        self.assertIsNone(millideg_to_c(None))
        self.assertIsNone(millideg_to_c("N/A"))


class TestPowerEst(unittest.TestCase):
    def test_delta(self):
        self.assertEqual(estimate_igpu_power(42.0, 5.0), 37.0)
    def test_clamps_zero(self):
        self.assertEqual(estimate_igpu_power(4.0, 5.0), 0.0)
    def test_none(self):
        self.assertIsNone(estimate_igpu_power(None, 5.0))
        self.assertIsNone(estimate_igpu_power(42.0, None))


NV_XML = """<?xml version="1.0" ?><nvidia_smi_log><gpu>
 <utilization><gpu_util>55 %</gpu_util><encoder_util>96 %</encoder_util><decoder_util>40 %</decoder_util></utilization>
 <temperature><gpu_temp>46 C</gpu_temp></temperature>
 <gpu_power_readings><power_draw>149.7 W</power_draw><current_power_limit>575.0 W</current_power_limit></gpu_power_readings>
 <clocks><sm_clock>2850 MHz</sm_clock></clocks>
 <clocks_event_reasons><clocks_event_reason_sw_thermal_slowdown>Not Active</clocks_event_reason_sw_thermal_slowdown></clocks_event_reasons>
 <processes><process_info><pid>1</pid><process_name>/usr/bin/ffmpeg</process_name></process_info>
  <process_info><pid>2</pid><process_name>emby</process_name></process_info></processes>
</gpu></nvidia_smi_log>"""


class TestNvXml(unittest.TestCase):
    def test_parse(self):
        d = parse_nvidia_xml(NV_XML)
        self.assertEqual(d["util"], 55.0)
        self.assertEqual(d["enc"], 96.0)
        self.assertEqual(d["dec"], 40.0)
        self.assertEqual(d["temp"], 46.0)
        self.assertAlmostEqual(d["power"], 149.7)
        self.assertEqual(d["clock"], 2850.0)
        self.assertFalse(d["throttle"])
        self.assertIn("emby", d["procs"])
    def test_parse_empty(self):
        self.assertEqual(parse_nvidia_xml("not xml"), {})


FDINFO = ("pos:\t0\nflags:\t02\ndrm-driver:\ti915\ndrm-pdev:\t0000:00:02.0\n"
          "drm-client-id:\t12\ndrm-engine-video:\t4902815164 ns\n")


class TestFdinfo(unittest.TestCase):
    def test_parse(self):
        d = parse_fdinfo(FDINFO)
        self.assertEqual(d["driver"], "i915")
        self.assertEqual(d["pdev"], "0000:00:02.0")
        self.assertGreater(d["video_ns"], 0)
    def test_not_drm(self):
        self.assertEqual(parse_fdinfo("pos:\t0\nflags:\t02\n"), {})


class TestBusy(unittest.TestCase):
    def test_busy_from_engines(self):
        with benchmark.TELE_LOCK:
            benchmark.TELEMETRY.clear()
            benchmark.TELEMETRY["engines"] = {"Video": 40.0, "Render/3D": 0.0}
        busy, load = gpu_busy({"vendor": "intel"})
        self.assertTrue(busy)
        self.assertEqual(load, 40.0)
    def test_idle_not_busy(self):
        with benchmark.TELE_LOCK:
            benchmark.TELEMETRY.clear()
            benchmark.TELEMETRY["engines"] = {"Video": 2.0}
        busy, load = gpu_busy({"vendor": "intel"})
        self.assertFalse(busy)


# Real fdinfo of a transcoding ffmpeg on an RX 9070 XT (Navi 48), captured live.
AMD_FD = ("drm-driver:\tamdgpu\n"
          "drm-pdev:\t0000:03:00.0\n"
          "drm-client-id:\t94\n"
          "drm-engine-compute:\t257845131 ns\n"
          "drm-engine-enc:\t2973192506 ns\n"
          "drm-engine-dec:\t1000000000 ns\n"
          "drm-engine-capacity-enc:\t1\n")


class TestAmdEngines(unittest.TestCase):
    def test_parse(self):
        d = parse_amd_engines(AMD_FD)
        self.assertEqual(d["enc_ns"], 2973192506)
        self.assertEqual(d["dec_ns"], 1000000000)
        self.assertEqual(d["comp_ns"], 257845131)   # scale_vaapi VPP runs on the compute ring
        self.assertEqual(d["enc_cap"], 1)
        self.assertEqual(d["pdev"], "0000:03:00.0")
        self.assertEqual(d["client"], "94")

    def test_not_amdgpu(self):
        # an i915 (Intel) DRM fd is not an amdgpu client
        self.assertEqual(parse_amd_engines("drm-driver:\ti915\ndrm-engine-video:\t5 ns\n"), {})

    def test_not_drm(self):
        self.assertEqual(parse_amd_engines("pos:\t0\nflags:\t02\n"), {})

    def test_missing_decode_defaults_zero(self):
        # an encode-only client has no drm-engine-dec line
        d = parse_amd_engines("drm-driver:\tamdgpu\ndrm-engine-enc:\t5 ns\n")
        self.assertEqual(d["dec_ns"], 0)
        self.assertEqual(d["enc_cap"], 1)  # capacity defaults to 1 when absent


class TestEnginePct(unittest.TestCase):
    def test_full_busy(self):
        # 1.0s of engine work over a 1.0s wall window on a single-instance engine = 100%
        self.assertEqual(engine_pct(1_000_000_000, 1.0, 1), 100.0)

    def test_half_busy(self):
        self.assertEqual(engine_pct(500_000_000, 1.0, 1), 50.0)

    def test_clamps_to_100(self):
        # summed work can momentarily exceed the window; occupancy never exceeds 100%
        self.assertEqual(engine_pct(3_000_000_000, 1.0, 1), 100.0)

    def test_capacity_halves(self):
        # two engine instances → the same ns is half the aggregate occupancy
        self.assertEqual(engine_pct(1_000_000_000, 1.0, 2), 50.0)

    def test_zero_and_negative_dt_safe(self):
        self.assertIsNone(engine_pct(100, 0.0, 1))
        self.assertIsNone(engine_pct(100, -1.0, 1))


class TestAbort(unittest.TestCase):
    """abort_run() must only fire mid-run (preparing/running) and must set the abort flag."""
    def _set_ui(self, ui):
        with benchmark.STATE_LOCK:
            benchmark.STATE["ui"] = ui

    def test_no_abort_when_idle(self):
        benchmark._ABORT.clear()
        self._set_ui("idle")
        self.assertFalse(benchmark.abort_run())
        self.assertFalse(benchmark._ABORT.is_set())

    def test_no_abort_when_done(self):
        benchmark._ABORT.clear()
        self._set_ui("done")
        self.assertFalse(benchmark.abort_run())
        self.assertFalse(benchmark._ABORT.is_set())

    def test_abort_when_running(self):
        benchmark._ABORT.clear()
        self._set_ui("running")
        self.assertTrue(benchmark.abort_run())
        self.assertTrue(benchmark._ABORT.is_set())

    def test_abort_when_preparing(self):
        benchmark._ABORT.clear()
        self._set_ui("preparing")
        self.assertTrue(benchmark.abort_run())
        self.assertTrue(benchmark._ABORT.is_set())


class TestClipMaster(unittest.TestCase):
    def test_paths(self):
        self.assertTrue(clip_master("4k", "hevc").endswith("source_4k_hevc.mkv"))
        self.assertTrue(clip_master("1080p", "h264").endswith("source_1080p_h264.mkv"))


class TestInputGating(unittest.TestCase):
    """select_input() must only accept a source codec the selected GPU can hardware-decode,
    and only while idle — this is the gate that stops a CPU-fallback benchmark."""
    def _setup(self, decodes, ui="idle"):
        benchmark._DETECTED = [{"idx": 0, "available": True, "name": "X",
                                "decodes": decodes, "codecs": ["h264"]}]
        with benchmark.STATE_LOCK:
            benchmark.STATE["ui"] = ui
            benchmark.STATE["selected_idx"] = 0
            benchmark.STATE["selected_input"] = "hevc"

    def test_accepts_decodable(self):
        self._setup(["hevc", "av1"])
        self.assertTrue(select_input("av1"))
        with benchmark.STATE_LOCK:
            self.assertEqual(benchmark.STATE["selected_input"], "av1")

    def test_rejects_undecodable(self):
        self._setup(["hevc"])                 # GPU can't hw-decode AV1
        self.assertFalse(select_input("av1"))
        with benchmark.STATE_LOCK:
            self.assertEqual(benchmark.STATE["selected_input"], "hevc")  # unchanged

    def test_rejects_when_not_idle(self):
        self._setup(["hevc", "av1"], ui="running")
        self.assertFalse(select_input("av1"))


class TestVideoFile(unittest.TestCase):
    def test_accepts_video(self):
        for n in ("a.mkv", "B.MP4", "show.mov", "x.ts", "y.m4v", "z.webm", "w.avi"):
            self.assertTrue(is_video_file(n), n)

    def test_rejects_nonvideo(self):
        for n in (".hidden.mkv", "poster.jpg", "notes.txt", "a.nfo", "subs.srt", "folder"):
            self.assertFalse(is_video_file(n), n)


class TestFitSeconds(unittest.TestCase):
    BUDGET = 480 * 1024 * 1024   # ~480 MB usable of a 512 MiB ramdisk

    def test_low_bitrate_keeps_full_window(self):
        # 40 Mbit · 60 s = 300 MB < budget → full 60 s
        self.assertEqual(fit_seconds(40_000_000, self.BUDGET, want=60), 60.0)

    def test_high_bitrate_trims(self):
        # 100 Mbit → 60 s would be 750 MB; must trim below 60
        self.assertLess(fit_seconds(100_000_000, self.BUDGET, want=60), 60.0)

    def test_extreme_bitrate_returns_tiny_not_floored(self):
        # a 10 Gbit stream fits almost nothing; fit_seconds returns the true (tiny) value so
        # the caller can refuse, rather than forcing a floor that would overflow the RAM disk
        self.assertLess(fit_seconds(10_000_000_000, self.BUDGET, want=60), 1.0)

    def test_unknown_bitrate_defaults_full(self):
        self.assertEqual(fit_seconds(None, self.BUDGET, want=60), 60.0)
        self.assertEqual(fit_seconds(0, self.BUDGET, want=60), 60.0)


class TestSampleWindow(unittest.TestCase):
    def test_long_file_centres(self):
        # 2-hour movie → 60 s centred on the midpoint (3600 s), i.e. start at 3570
        start, length = sample_window(7200.0, want=60)
        self.assertEqual(length, 60)
        self.assertAlmostEqual(start, 3570.0, places=1)

    def test_short_file_uses_whole(self):
        start, length = sample_window(45.0, want=60)
        self.assertEqual(start, 0.0)
        self.assertIsNone(length)             # None length ⇒ copy the whole file

    def test_none_duration_uses_whole(self):
        self.assertEqual(sample_window(None, want=60), (0.0, None))


class TestTranscodeCmd(unittest.TestCase):
    """BOTH the CUDA and VAAPI paths need -noautoscale or scale_cuda/scale_vaapi fails to re-init
    at the -stream_loop seam (an 8-bit H.264 source dies on the 2nd pass otherwise — verified live
    on the RTX 5090 and the UHD 770 iGPU). It's an OUTPUT option, so it must come after -i."""
    def test_nvenc_noautoscale_after_input(self):
        cmd = benchmark.transcode_cmd({"api": "nvenc", "index": 0}, "/x.mkv", "hevc")
        self.assertIn("-noautoscale", cmd)
        self.assertGreater(cmd.index("-noautoscale"), cmd.index("-i"))

    def test_vaapi_noautoscale_after_input(self):
        cmd = benchmark.transcode_cmd(
            {"api": "vaapi", "device": "/dev/dri/renderD129", "vendor": "intel"}, "/x.mkv", "h264")
        self.assertIn("scale_vaapi=w=1920:h=1080:format=nv12", cmd)
        self.assertIn("h264_vaapi", cmd)
        self.assertIn("-noautoscale", cmd)
        self.assertGreater(cmd.index("-noautoscale"), cmd.index("-i"))

    def test_cpu_software_path(self):
        # CPU device: pure software (no -hwaccel), libx265, named preset, software scale
        cmd = benchmark.transcode_cmd(
            {"api": "software", "vendor": "cpu"}, "/x.mkv", "hevc", "1080p", False, "medium")
        self.assertNotIn("-hwaccel", cmd)
        self.assertNotIn("-noautoscale", cmd)          # loop-seam bug is hwaccel-only
        self.assertIn("libx265", cmd)
        self.assertIn("scale=1920:1080", cmd)
        self.assertEqual(cmd[cmd.index("-preset") + 1], "medium")

    def test_cpu_av1_numeric_preset(self):
        # libsvtav1 needs a NUMBER, not a named preset
        cmd = benchmark.transcode_cmd(
            {"api": "software", "vendor": "cpu"}, "/x.mkv", "av1", "1080p", False, "veryfast")
        self.assertIn("libsvtav1", cmd)
        self.assertEqual(cmd[cmd.index("-preset") + 1], "10")   # veryfast → svt 10


class TestResolution(unittest.TestCase):
    def test_no_upscaling(self):
        # target must be <= source resolution
        self.assertEqual(target_res_options("4k"), ["4k", "1080p", "720p"])
        self.assertEqual(target_res_options("1080p"), ["1080p", "720p"])

    def test_source_is_10bit(self):
        # the shipped 10-bit clips are the HEVC/AV1 masters; H.264 is 8-bit
        self.assertTrue(source_is_10bit("hevc"))
        self.assertTrue(source_is_10bit("av1"))
        self.assertFalse(source_is_10bit("h264"))

    def test_ten_bit_output_only_when_preserving(self):
        # keep 4K + 10-bit source + HEVC/AV1 out ⇒ preserve 10-bit
        self.assertTrue(ten_bit_output("4k", "4k", "hevc", "av1"))
        self.assertTrue(ten_bit_output("4k", "4k", "av1", "hevc"))
        # downscale ⇒ 8-bit (accepting a lighter version)
        self.assertFalse(ten_bit_output("4k", "1080p", "hevc", "av1"))
        # H.264 output has no hardware 10-bit ⇒ 8-bit
        self.assertFalse(ten_bit_output("4k", "4k", "hevc", "h264"))
        # 8-bit source (H.264) ⇒ nothing to preserve
        self.assertFalse(ten_bit_output("4k", "4k", "h264", "av1"))

    def test_is_comparable_only_canonical(self):
        # only 4K -> 1080p is leaderboard-comparable; everything else is local-only
        self.assertTrue(is_comparable("4k", "1080p"))
        self.assertFalse(is_comparable("4k", "4k"))
        self.assertFalse(is_comparable("1080p", "1080p"))
        self.assertFalse(is_comparable("1080p", "720p"))


class TestConversionAdvice(unittest.TestCase):
    LEVELS = [{"n": 1, "combined": 6.0}, {"n": 2, "combined": 9.5},
              {"n": 3, "combined": 10.0}, {"n": 4, "combined": 10.1}]

    def test_recommended_workers_fastest(self):
        # recommend the worker count with the HIGHEST combined throughput (peak, n=4)
        self.assertEqual(recommended_workers(self.LEVELS, 10.1), 4)

    def test_recommended_workers_single_when_flat(self):
        # already saturated at 1 stream → recommend 1
        self.assertEqual(recommended_workers([{"n": 1, "combined": 8.0}], 8.0), 1)

    def test_recommended_workers_single_when_declining(self):
        # the UHD 770 case: 2 streams do LESS combined work than 1 (one engine, already
        # saturated + context-switch overhead) → the fastest is 1 worker
        self.assertEqual(
            recommended_workers([{"n": 1, "combined": 4.61}, {"n": 2, "combined": 3.76}], 4.61), 1)

    def test_recommended_workers_capped(self):
        # a driver session cap below the fastest level clamps the recommendation
        self.assertEqual(recommended_workers(self.LEVELS, 10.1, cap=2), 2)

    def test_throughput_saturated(self):
        # a level gaining <5% over the best-so-far means the engine has plateaued
        self.assertTrue(throughput_saturated(10.2, 10.0))     # +2% → saturated
        self.assertFalse(throughput_saturated(9.5, 6.0))      # +58% → keep ramping
        self.assertTrue(throughput_saturated(9.9, 10.0))      # declined → saturated


class TestCpuBaseline(unittest.TestCase):
    def test_key(self):
        self.assertEqual(cpu_baseline_key("convert", "1080p H264 -> 1080p HEVC"),
                         "convert|1080p H264 -> 1080p HEVC")

    def test_baseline_valid(self):
        entry = {"cpu_model": "i9-13900K", "tool_version": "1.0"}
        self.assertTrue(baseline_valid(entry, "i9-13900K", "1.0"))
        self.assertTrue(baseline_valid(entry, "i9-13900K", "1.4"))   # same major → compatible
        self.assertFalse(baseline_valid(entry, "Ryzen 9", "1.0"))    # different CPU
        self.assertFalse(baseline_valid(entry, "i9-13900K", "2.0"))  # major bump → stale
        self.assertFalse(baseline_valid(None, "i9-13900K", "1.0"))
        self.assertFalse(baseline_valid({}, "i9-13900K", "1.0"))

    def test_efficiency_ratio(self):
        self.assertEqual(efficiency_ratio(120.0, 8.0), 15.0)   # CPU 120 W/stream vs GPU 8 → 15×
        self.assertIsNone(efficiency_ratio(120.0, 0))
        self.assertIsNone(efficiency_ratio(None, 8.0))

    def test_speed_ratio(self):
        self.assertEqual(speed_ratio(21.4, 4.6), 4.7)          # GPU 21.4× vs CPU 4.6× per file
        self.assertIsNone(speed_ratio(10, 0))                  # CPU sustained 0 streams
        self.assertIsNone(speed_ratio(10, None))

    def test_compute_vs_cpu_conversion(self):
        gpu = {"single_stream": 21.4, "max_sustained": 0, "watts_per_stream": 8.0,
               "peak_power_w": 40.0}
        cpu = {"single_stream": 4.6, "max_sustained": 0, "watts_per_stream": 120.0,
               "peak_power_w": 130.0, "preset": "medium", "encoder": "libx265"}
        v = compute_vs_cpu("convert", gpu, cpu, is_dgpu=True)
        self.assertEqual(v["efficiency"], 15.0)
        self.assertEqual(v["speed"], 4.7)
        self.assertEqual(v["speed_kind"], "perfile")
        self.assertTrue(v["dgpu_caveat"])
        self.assertEqual(v["watts_cpu"], 130.0)
        # watts_per_stream = W ÷ throughput(×realtime) which IS Wh per hour of video —
        # do NOT divide by speed again (the original double-division bug)
        self.assertEqual(v["energy_gpu"], 8.0)
        self.assertEqual(v["energy_cpu"], 120.0)

    def test_compute_vs_cpu_streaming_zero(self):
        # CPU can't sustain even one stream at realtime → speed is None, flagged
        gpu = {"single_stream": 7.3, "max_sustained": 6, "watts_per_stream": 3.0,
               "peak_power_w": 12.0}
        cpu = {"single_stream": 0.4, "max_sustained": 0, "watts_per_stream": 60.0,
               "peak_power_w": 95.0, "preset": "veryfast", "encoder": "libx264"}
        v = compute_vs_cpu("streaming", gpu, cpu, is_dgpu=False)
        self.assertEqual(v["speed_kind"], "streams")
        self.assertIsNone(v["speed"])              # cpu max_sustained 0 → no ratio
        self.assertFalse(v["cpu_could_sustain"])
        self.assertFalse(v["dgpu_caveat"])

    def test_load_save_baseline_roundtrip(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "cpu_baseline.json")
            self.assertEqual(load_baseline(p), {})            # missing file → {}
            entry = {"cpu_model": "x", "single_stream": 4.6}
            self.assertTrue(save_baseline(p, "convert|A -> B", entry))
            self.assertTrue(save_baseline(p, "streaming|A -> B", {"single_stream": 1.0}))
            got = load_baseline(p)
            self.assertEqual(got["convert|A -> B"], entry)    # both keys coexist (no overwrite)
            self.assertIn("streaming|A -> B", got)

    def test_save_baseline_unwritable(self):
        # no such directory → degrade to False, never raise
        self.assertFalse(save_baseline("/no/such/dir/x.json", "k", {"a": 1}))


class TestDefaultSelection(unittest.TestCase):
    GPU = {"idx": 0, "vendor": "intel", "available": True}
    NV  = {"idx": 1, "vendor": "nvidia", "available": True}
    CPU = {"idx": 2, "vendor": "cpu", "available": True}

    def test_single_hw_gpu_wins_over_cpu(self):
        # the CPU device is always present — it must not break single-GPU auto-select
        self.assertEqual(default_selection([self.GPU, self.CPU]), self.GPU)

    def test_two_hw_gpus_no_auto(self):
        self.assertIsNone(default_selection([self.GPU, self.NV, self.CPU]))

    def test_cpu_only_box_selects_cpu(self):
        self.assertEqual(default_selection([self.CPU]), self.CPU)

    def test_empty(self):
        self.assertIsNone(default_selection([]))


class TestHistory(unittest.TestCase):
    E = [
        {"ts": 3, "gpu": "UHD 770", "mode": "streaming", "profile": "A", "max_sustained": 5,
         "single_stream": 2.0, "ram_speed": "DDR4-3200"},
        {"ts": 2, "gpu": "UHD 770", "mode": "streaming", "profile": "A", "max_sustained": 3,
         "single_stream": 1.9, "ram_speed": "DDR4-2133"},
        {"ts": 1, "gpu": "RTX 5090", "mode": "streaming", "profile": "A", "max_sustained": 12},
        {"ts": 0, "gpu": "UHD 770", "mode": "convert", "profile": "A", "single_stream": 4.4},
    ]

    def test_history_for_filters_and_sorts_newest_first(self):
        got = history_for(self.E, "UHD 770", "streaming", "A")
        self.assertEqual([e["ts"] for e in got], [3, 2])

    def test_history_for_limit(self):
        got = history_for(self.E, "UHD 770", "streaming", "A", limit=1)
        self.assertEqual(len(got), 1)

    def test_run_delta_streaming(self):
        d = run_delta({"mode": "streaming", "max_sustained": 3},
                      {"mode": "streaming", "max_sustained": 5})
        self.assertEqual((d["metric"], d["prev"], d["cur"]), ("streams", 3, 5))
        self.assertEqual(d["pct"], 67)          # +67%

    def test_run_delta_convert(self):
        d = run_delta({"mode": "convert", "single_stream": 4.0},
                      {"mode": "convert", "single_stream": 3.0})
        self.assertEqual((d["metric"], d["pct"]), ("perfile", -25))

    def test_run_delta_guards(self):
        self.assertIsNone(run_delta(None, {"mode": "streaming", "max_sustained": 5}))
        # previous headline 0 → no ratio
        d = run_delta({"mode": "streaming", "max_sustained": 0},
                      {"mode": "streaming", "max_sustained": 2})
        self.assertIsNone(d["pct"])
        self.assertEqual(d["prev"], 0)

    def test_append_history_caps_and_persists(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "history.json")
            self.assertEqual(load_history(p), [])
            for i in range(7):
                self.assertTrue(append_history(p, {"ts": i}, cap=5))
            got = load_history(p)
            self.assertEqual(len(got), 5)                      # capped
            self.assertEqual([e["ts"] for e in got], [2, 3, 4, 5, 6])  # oldest dropped

    def test_append_history_unwritable(self):
        self.assertFalse(append_history("/no/such/dir/h.json", {"ts": 1}))


class TestBatchEligible(unittest.TestCase):
    G = {"available": True, "decodes": ["h264", "hevc"], "codecs": ["h264", "hevc"]}

    def test_eligible(self):
        self.assertTrue(batch_eligible(self.G, "hevc", "h264"))

    def test_cannot_decode(self):
        self.assertFalse(batch_eligible(self.G, "av1", "h264"))

    def test_cannot_encode(self):
        self.assertFalse(batch_eligible(self.G, "hevc", "av1"))

    def test_unavailable(self):
        g = dict(self.G, available=False)
        self.assertFalse(batch_eligible(g, "hevc", "h264"))

    def test_skip_reason(self):
        # a skipped device gets a human-readable reason for the comparison table
        self.assertIsNone(batch_skip_reason(self.G, "hevc", "h264"))
        self.assertEqual(batch_skip_reason(self.G, "hevc", "av1"), "can't encode AV1")
        self.assertEqual(batch_skip_reason(self.G, "av1", "h264"), "can't decode AV1")
        self.assertEqual(batch_skip_reason(dict(self.G, available=False), "hevc", "h264"),
                         "not testable")


class TestLimitClassification(unittest.TestCase):
    def test_death_reason_session(self):
        self.assertEqual(death_reason("OpenEncodeSessionEx failed: out of memory (10)"), "session")
        self.assertEqual(death_reason("No capable devices: maximum number of encode sessions"),
                         "session")

    def test_death_reason_memory(self):
        # the REAL signature from the patched-5090 VRAM wall (scale_cuda alloc failure)
        self.assertEqual(death_reason("Error while filtering: Cannot allocate memory"), "memory")

    def test_death_reason_unknown(self):
        self.assertIsNone(death_reason("some novel driver error"))
        self.assertIsNone(death_reason(""))
        self.assertIsNone(death_reason(None))

    def test_vram_slope_median_of_increments(self):
        # the measured 5090 shape: metronomic ~1415 MiB per session (level-1 absolute unused)
        samples = [(1, 1414), (2, 2827), (3, 4261), (4, 5674), (5, 7091)]
        self.assertAlmostEqual(vram_slope(samples), 1415, delta=5)

    def test_vram_slope_skips_gaps_and_needs_two_increments(self):
        self.assertIsNone(vram_slope([(1, 1400)]))
        self.assertIsNone(vram_slope([(1, 1400), (2, 2800)]))     # one increment isn't a median
        # non-consecutive levels normalise per session
        self.assertAlmostEqual(vram_slope([(2, 2800), (4, 5600), (6, 8400)]), 1400, delta=1)

    def test_vram_slope_robust_to_one_outlier(self):
        samples = [(1, 1400), (2, 2800), (3, 4200), (4, 9000), (5, 10400)]
        # increments: 1400, 1400, 4800, 1400 → median 1400 (the co-tenant blip is ignored)
        self.assertAlmostEqual(vram_slope(samples), 1400, delta=1)

    def test_predict_wall(self):
        # at N=5 with 18.4 GB free and 1.415 GB/session → 5 + 13 = 18 (the real 5090 case)
        self.assertEqual(predict_wall(5, 18400, 1415), 18)
        self.assertIsNone(predict_wall(5, 18400, 0))
        self.assertIsNone(predict_wall(5, None, 1415))

    def test_classify_throughput(self):
        self.assertEqual(classify_stop(False, None, 14, None), "throughput")

    def test_classify_session(self):
        self.assertEqual(classify_stop(True, "session", 13, None), "session")

    def test_classify_memory_by_signature(self):
        self.assertEqual(classify_stop(True, "memory", 19, None), "memory")

    def test_classify_memory_by_prediction_agreement(self):
        # no recognised signature, but the VRAM prediction said the wall was here (±1)
        self.assertEqual(classify_stop(True, None, 19, 18), "memory")
        self.assertEqual(classify_stop(True, None, 18, 18), "memory")

    def test_classify_unknown(self):
        # hard death, no signature, prediction says the wall was much further away
        self.assertEqual(classify_stop(True, None, 14, 18), "unknown")
        self.assertEqual(classify_stop(True, None, 14, None), "unknown")


class TestSubmission(unittest.TestCase):
    def test_install_id_created_and_stable(self):
        import tempfile, os, uuid
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "install_id")
            a = load_or_create_install_id(p)
            uuid.UUID(a)                                   # valid uuid4
            self.assertEqual(load_or_create_install_id(p), a)   # persisted → stable

    def test_install_id_unwritable_still_returns_id(self):
        import uuid
        a = load_or_create_install_id("/no/such/dir/install_id")
        uuid.UUID(a)                                       # ephemeral but usable — never raises

    def test_envelope(self):
        result = {"tool_version": "1.0", "comparable": True, "max_sustained": 7}
        env = submission_envelope(result, "abc-123", ts=1783100000)
        self.assertEqual(env["schema"], 1)
        self.assertEqual(env["install_id"], "abc-123")
        self.assertEqual(env["submitted_at"], 1783100000)
        self.assertEqual(env["result"]["max_sustained"], 7)


class TestQueryDecode(unittest.TestCase):
    def test_url_decoding(self):
        # real media filenames have spaces/parens — encodeURIComponent sends %20 etc., and the
        # server must DECODE before matching (the bug that made custom files unselectable)
        self.assertEqual(_query_str("/custom?f=Aliens%20(1986)%20Remux-2160p.mkv", "f"),
                         "Aliens (1986) Remux-2160p.mkv")
        self.assertEqual(_query_str("/codec?c=h264", "c"), "h264")
        self.assertIsNone(_query_str("/custom", "f"))
        self.assertEqual(_query_str("/custom?f=", "f"), "")


class TestRapl(unittest.TestCase):
    def test_delta_monotonic(self):
        self.assertEqual(rapl_delta_uj(1_000_000, 3_500_000, 262_143_328_850), 2_500_000)

    def test_delta_wraparound(self):
        # counter wrapped at max_energy_range_uj (5950X: ~65.5e9): cur < prev
        self.assertEqual(rapl_delta_uj(65_532_610_000, 987, 65_532_610_987), 1974)

    def test_delta_guards(self):
        self.assertIsNone(rapl_delta_uj(None, 5, 100))
        self.assertIsNone(rapl_delta_uj(5, None, 100))
        self.assertIsNone(rapl_delta_uj(10, 5, None))   # wrapped but range unknown → can't tell

    def test_watts(self):
        # 25,000,000 µJ over 2 s = 12.5 W
        self.assertEqual(rapl_watts(25_000_000, 2.0), 12.5)
        self.assertIsNone(rapl_watts(None, 2.0))
        self.assertIsNone(rapl_watts(1000, 0))

    def test_package_discovery(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as d:
            def mk(rel, name):
                p = os.path.join(d, rel)
                os.makedirs(p, exist_ok=True)
                with open(os.path.join(p, "name"), "w") as f:
                    f.write(name + "\n")
            mk("intel-rapl/intel-rapl:0", "package-0")                  # keep
            mk("intel-rapl/intel-rapl:1", "psys")                       # skip (not a package)
            mk("intel-rapl/intel-rapl:0/intel-rapl:0:0", "core")        # nested subdomain: not matched
            mk("intel-rapl-mmio/intel-rapl-mmio:0", "package-0")        # mmio duplicate: not matched
            got = rapl_package_paths(d)
            self.assertEqual([os.path.basename(p) for p in got], ["intel-rapl:0"])

    def test_package_discovery_missing_root(self):
        self.assertEqual(rapl_package_paths("/no/such/powercap"), [])


class TestCpuLoadPct(unittest.TestCase):
    def test_basic(self):
        # 1-min loadavg 8 on a 32-thread box = 25% busy
        self.assertEqual(cpu_load_pct(8.0, 32), 25.0)
        self.assertEqual(cpu_load_pct(0.5, 8), 6.2)

    def test_guards(self):
        self.assertIsNone(cpu_load_pct(None, 8))
        self.assertIsNone(cpu_load_pct(1.0, 0))
        self.assertEqual(cpu_load_pct(64.0, 16), 100.0)   # clamped


class TestProcStatCpu(unittest.TestCase):
    """Instantaneous whole-box CPU busy% from /proc/stat deltas — the live tile for CPU-device
    runs (loadavg is a 1-minute average, far too sluggish for a 1 Hz display)."""
    STAT_A = "cpu  100 0 50 800 50 0 0 0 0 0\ncpu0 25 0 12 200 12 0 0 0 0 0\n"
    STAT_B = "cpu  160 0 90 850 50 0 0 0 0 0\ncpu0 40 0 22 212 12 0 0 0 0 0\n"

    def test_parse(self):
        # busy = total - idle - iowait = 1000 - 800 - 50
        self.assertEqual(benchmark.parse_proc_stat(self.STAT_A), (150, 1000))

    def test_parse_bad(self):
        self.assertIsNone(benchmark.parse_proc_stat(""))
        self.assertIsNone(benchmark.parse_proc_stat(None))
        self.assertIsNone(benchmark.parse_proc_stat("cpu  1 2\n"))       # too few fields
        self.assertIsNone(benchmark.parse_proc_stat("cpu  a b c d\n"))   # non-numeric
        self.assertIsNone(benchmark.parse_proc_stat("cpu0 1 2 3 4\n"))   # per-core only

    def test_pct_delta(self):
        a = benchmark.parse_proc_stat(self.STAT_A)
        b = benchmark.parse_proc_stat(self.STAT_B)
        # delta busy 100, delta total 150 -> 66.7%
        self.assertEqual(benchmark.cpu_stat_pct(a, b), 66.7)

    def test_pct_guards(self):
        a = benchmark.parse_proc_stat(self.STAT_A)
        self.assertIsNone(benchmark.cpu_stat_pct(None, a))
        self.assertIsNone(benchmark.cpu_stat_pct(a, None))
        self.assertIsNone(benchmark.cpu_stat_pct(a, a))                  # zero time delta
        # counters never go backwards in reality, but a clamp guards a torn read
        self.assertEqual(benchmark.cpu_stat_pct((200, 1000), (100, 2000)), 0.0)


class TestOriginOk(unittest.TestCase):
    """CSRF guard for the local controller: a cross-origin browser POST carries an Origin
    header whose host won't match ours. Reject only when Origin is present AND mismatched —
    same-origin page requests and non-browser clients (curl, no Origin) pass."""
    def test_no_origin_passes(self):        # curl / same-origin navigations omit Origin
        self.assertTrue(benchmark.origin_ok(None, "tower:8088"))
        self.assertTrue(benchmark.origin_ok("", "tower:8088"))

    def test_same_origin_passes(self):
        self.assertTrue(benchmark.origin_ok("http://tower:8088", "tower:8088"))
        self.assertTrue(benchmark.origin_ok("http://192.0.2.10:8088", "192.0.2.10:8088"))

    def test_cross_origin_rejected(self):
        self.assertFalse(benchmark.origin_ok("http://evil.example", "tower:8088"))
        self.assertFalse(benchmark.origin_ok("https://tower:8088", "tower:9999"))

    def test_null_and_garbage_origin_rejected(self):
        self.assertFalse(benchmark.origin_ok("null", "tower:8088"))
        self.assertFalse(benchmark.origin_ok("http://", "tower:8088"))


class TestNvencLockState(unittest.TestCase):
    """Detect the keylase NVENC session-cap patch by scanning libnvidia-encode.so for the
    per-version stock vs patched byte signatures. Drives the leaderboard's locked/unlocked
    NVIDIA entity split + badge."""
    # a real pair (595.71.05): stock has test/jne, patched has sub eax,eax
    SIGS = {"595.71.05": ["e85121feff4189c685c0", "e85121feff29c04189c6"],
            "550.00": ["85c00f8596000000", "29c090909090909090"]}

    def test_patched_is_unlocked(self):
        lib = b"\x00\x00" + bytes.fromhex("e85121feff29c04189c6") + b"\xff\xff"
        self.assertEqual(benchmark.nvenc_lock_state(lib, "595.71.05", self.SIGS), "unlocked")

    def test_stock_is_locked(self):
        lib = b"\x11" + bytes.fromhex("e85121feff4189c685c0") + b"\x22"
        self.assertEqual(benchmark.nvenc_lock_state(lib, "595.71.05", self.SIGS), "locked")

    def test_unknown_version_is_none(self):
        lib = bytes.fromhex("e85121feff29c04189c6")
        self.assertIsNone(benchmark.nvenc_lock_state(lib, "999.99", self.SIGS))

    def test_neither_signature_present_is_none(self):
        self.assertIsNone(benchmark.nvenc_lock_state(b"\x00" * 64, "595.71.05", self.SIGS))

    def test_empty_or_missing(self):
        self.assertIsNone(benchmark.nvenc_lock_state(b"", "595.71.05", self.SIGS))
        self.assertIsNone(benchmark.nvenc_lock_state(None, "595.71.05", self.SIGS))
        self.assertIsNone(benchmark.nvenc_lock_state(b"\x00", "595.71.05", {}))

    def test_shipped_sig_table_loads_and_covers_5090_driver(self):
        # the real committed data file must load and carry the driver the fleet runs
        sigs = benchmark.load_nvenc_sigs()
        self.assertIn("595.71.05", sigs)
        self.assertEqual(len(sigs["595.71.05"]), 2)


class TestDisplayUnit(unittest.TestCase):
    """Temperature display unit from Unraid's dynamix.cfg (optional RO mount) — the tool
    should show °F when the server dashboard does. Data stays °C everywhere; this only
    drives the UI default (browser toggle can still override)."""
    CFG_F = '[display]\ntty="15"\nunit="F"\nnumber=".,"\n'
    CFG_C = '[display]\nunit="C"\n'

    def test_fahrenheit(self):
        self.assertEqual(benchmark.parse_display_unit(self.CFG_F), "F")

    def test_celsius(self):
        self.assertEqual(benchmark.parse_display_unit(self.CFG_C), "C")

    def test_default_celsius(self):
        self.assertEqual(benchmark.parse_display_unit(""), "C")
        self.assertEqual(benchmark.parse_display_unit(None), "C")
        self.assertEqual(benchmark.parse_display_unit('[display]\ntty="15"\n'), "C")
        # docker creates a DIRECTORY at the target when the host file is missing —
        # _read() returns None there, which must still land on C (covered above)

    def test_not_fooled_by_lookalikes(self):
        self.assertEqual(benchmark.parse_display_unit('somekey_unit="F"\n'), "C")
        self.assertEqual(benchmark.parse_display_unit('unit="X"\n'), "C")


class TestOsVersion(unittest.TestCase):
    """The OS-version reader must handle non-Unraid version strings cleanly — a MOS host
    reported version="MOS 0.5.0" and the old digit-anchored regex dumped the whole raw line."""
    def test_unraid_numeric(self):
        self.assertEqual(benchmark.parse_os_version('version="7.3.2"\n'), "7.3.2")

    def test_non_numeric_os(self):
        self.assertEqual(benchmark.parse_os_version('version="MOS 0.5.0"'), "MOS 0.5.0")

    def test_unquoted(self):
        self.assertEqual(benchmark.parse_os_version('version=7.0.1'), "7.0.1")

    def test_empty_or_none(self):
        self.assertIsNone(benchmark.parse_os_version(""))
        self.assertIsNone(benchmark.parse_os_version(None))

    def test_capped_to_sane_length(self):
        self.assertLessEqual(len(benchmark.parse_os_version('version="' + "x" * 200 + '"')), 60)


class TestOsRelease(unittest.TestCase):
    """The non-Unraid fallback reads PRETTY_NAME from /etc/os-release. It must return a clean,
    self-describing distro name and strip a trailing CPU-arch suffix."""
    SAMPLE = ('NAME="Ubuntu"\nVERSION="22.04.3 LTS (Jammy Jellyfish)"\n'
              'PRETTY_NAME="Ubuntu 22.04.3 LTS"\nID=ubuntu\nVERSION_ID="22.04"\n')

    def test_ubuntu_pretty_name(self):
        self.assertEqual(benchmark.parse_os_release(self.SAMPLE), "Ubuntu 22.04.3 LTS")

    def test_debian_with_parens(self):
        self.assertEqual(
            benchmark.parse_os_release('PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"\n'),
            "Debian GNU/Linux 12 (bookworm)")

    def test_strips_trailing_arch(self):
        # Unraid's own os-release carries the arch; Unraid is read from the version file first,
        # but the stripping must still work if we ever land here.
        self.assertEqual(benchmark.parse_os_release('PRETTY_NAME="Unraid OS 7.3 x86_64"'),
                         "Unraid OS 7.3")

    def test_no_pretty_name(self):
        self.assertIsNone(benchmark.parse_os_release('NAME="Foo"\nID=foo\n'))

    def test_empty_or_none(self):
        self.assertIsNone(benchmark.parse_os_release(""))
        self.assertIsNone(benchmark.parse_os_release(None))

    def test_capped_to_sane_length(self):
        self.assertLessEqual(len(benchmark.parse_os_release('PRETTY_NAME="' + "x" * 200 + '"')), 60)


class TestDecodeProbe(unittest.TestCase):
    """The decode probe must pipe frames through a GPU-only scale filter so a silent CPU
    fallback fails the probe instead of wrongly passing. A GTX 970 can NVENC-encode HEVC but
    can't NVDEC-decode it; the old filterless probe decoded 1 frame in software and wrongly
    reported HEVC decode as supported."""
    def test_nvenc_probe_forces_gpu_scale(self):
        cmd = benchmark.decode_probe_cmd({"api": "nvenc", "index": 0}, "/x/probe.mkv")
        self.assertIn("scale_cuda=64:64", cmd)
        self.assertIn("-noautoscale", cmd)
        self.assertIn("cuda", cmd)                       # -hwaccel cuda

    def test_vaapi_probe_forces_gpu_scale(self):
        cmd = benchmark.decode_probe_cmd({"api": "vaapi", "device": "/dev/dri/renderD128"},
                                         "/x/probe.mkv")
        self.assertIn("scale_vaapi=w=64:h=64", cmd)
        self.assertIn("-noautoscale", cmd)

    def test_nvenc_device_index_threaded(self):
        cmd = benchmark.decode_probe_cmd({"api": "nvenc", "index": 2}, "/x/probe.mkv")
        self.assertEqual(cmd[cmd.index("-hwaccel_device") + 1], "2")

    def test_nvenc_no_index_omits_device(self):
        cmd = benchmark.decode_probe_cmd({"api": "nvenc", "index": None}, "/x/probe.mkv")
        self.assertNotIn("-hwaccel_device", cmd)


class TestHdrTonemap(unittest.TestCase):
    """HDR is a pseudo input codec: transcode_cmd grows a tone-map stage per vendor (chains
    validated live on real hardware incl. the -stream_loop seam). Output is always SDR 8-bit."""
    VAAPI = {"api": "vaapi", "device": "/dev/dri/renderD129", "vendor": "intel"}
    NVENC = {"api": "nvenc", "index": 0, "vendor": "nvidia"}
    CPU = {"api": "software", "vendor": "cpu"}

    def _vf(self, cmd):
        return cmd[cmd.index("-vf") + 1]

    def test_vaapi_hdr_chain(self):
        cmd = benchmark.transcode_cmd(self.VAAPI, "/x.mkv", "h264", hdr=True)
        vf = self._vf(cmd)
        self.assertIn("tonemap_vaapi=format=nv12", vf)
        self.assertLess(vf.index("tonemap_vaapi"), vf.index("scale_vaapi"))  # tone-map at 4K, then scale
        self.assertIn("h264_vaapi", cmd)
        self.assertGreater(cmd.index("-noautoscale"), cmd.index("-i"))

    def test_nvenc_hdr_chain(self):
        cmd = benchmark.transcode_cmd(self.NVENC, "/x.mkv", "h264", hdr=True)
        vf = self._vf(cmd)
        self.assertIn("tonemap_cuda=format=nv12", vf)
        self.assertLess(vf.index("tonemap_cuda"), vf.index("scale_cuda"))
        self.assertIn("h264_nvenc", cmd)

    def test_cpu_hdr_chain(self):
        cmd = benchmark.transcode_cmd(self.CPU, "/x.mkv", "h264", hdr=True)
        vf = self._vf(cmd)
        self.assertIn("tonemapx=", vf)
        self.assertIn("scale=1920:1080", vf)
        self.assertNotIn("-hwaccel", cmd)

    def test_hdr_never_ten_bit(self):
        # tone-mapped output is SDR 8-bit by definition — the auto-10-bit matrix never triggers
        self.assertFalse(source_is_10bit("hdr"))
        self.assertFalse(ten_bit_output("4k", "4k", "hdr", "hevc"))

    def test_hdr_shipped_4k_only(self):
        self.assertIn("hdr", benchmark.SOURCE_CODECS_BY_RES["4k"])
        self.assertNotIn("hdr", benchmark.SOURCE_CODECS_BY_RES["1080p"])

    def test_hdr_clip_master_path(self):
        self.assertTrue(benchmark.clip_master("4k", "hdr").endswith("source_4k_hdr.mkv"))

    def test_batch_skip_reason_tone_map(self):
        gpu = {"available": True, "decodes": ["h264", "hevc"], "codecs": ["h264"]}
        self.assertEqual(benchmark.batch_skip_reason(gpu, "hdr", "h264"), "can't tone-map HDR")
        gpu["decodes"].append("hdr")
        self.assertIsNone(benchmark.batch_skip_reason(gpu, "hdr", "h264"))


class TestSubsBurnin(unittest.TestCase):
    """Subtitle burn-in forces the realistic full-transcode path: hw download → CPU libass render
    → back to the encoder. NVENC takes system-memory frames but REQUIRES the trailing format=nv12
    pin (graph otherwise negotiates a format nvenc rejects — found live: CreateInputBuffer
    invalid param 8)."""
    VAAPI = {"api": "vaapi", "device": "/dev/dri/renderD129", "vendor": "intel"}
    NVENC = {"api": "nvenc", "index": 0, "vendor": "nvidia"}
    CPU = {"api": "software", "vendor": "cpu"}

    def _vf(self, cmd):
        return cmd[cmd.index("-vf") + 1]

    def test_vaapi_subs_chain(self):
        cmd = benchmark.transcode_cmd(self.VAAPI, "/x.mkv", "h264", subs=True)
        vf = self._vf(cmd)
        for a, b in [("hwdownload", "scale="), ("scale=", "subtitles="), ("subtitles=", "hwupload")]:
            self.assertLess(vf.index(a), vf.index(b), f"{a} must precede {b} in {vf}")
        self.assertIn("h264_vaapi", cmd)

    def test_nvenc_subs_chain_sw_frames(self):
        cmd = benchmark.transcode_cmd(self.NVENC, "/x.mkv", "h264", subs=True)
        self.assertNotIn("-hwaccel_output_format", cmd)   # frames auto-download; NVENC uploads itself
        vf = self._vf(cmd)
        self.assertIn("subtitles=", vf)
        self.assertTrue(vf.endswith("format=nv12"), vf)   # the REQUIRED pin before nvenc
        self.assertIn("-hwaccel", cmd)                    # decode still on NVDEC

    def test_cpu_subs_chain(self):
        cmd = benchmark.transcode_cmd(self.CPU, "/x.mkv", "h264", subs=True)
        self.assertIn("subtitles=", self._vf(cmd))
        self.assertNotIn("-hwaccel", cmd)

    def test_hdr_plus_subs_rejected(self):
        with self.assertRaises(ValueError):
            benchmark.transcode_cmd(self.VAAPI, "/x.mkv", "h264", hdr=True, subs=True)


class TestProfileLabel(unittest.TestCase):
    def test_canonical(self):
        self.assertEqual(benchmark.profile_label("4k", "hevc", "1080p", "h264", False, False, False),
                         "4K HEVC -> 1080p H264")

    def test_hdr(self):
        self.assertEqual(benchmark.profile_label("4k", "hdr", "1080p", "h264", False, False, False),
                         "4K HDR -> 1080p H264")

    def test_subs_suffix(self):
        self.assertEqual(benchmark.profile_label("4k", "hevc", "1080p", "h264", False, True, False),
                         "4K HEVC -> 1080p H264 + subs")

    def test_custom_and_ten_bit(self):
        self.assertEqual(benchmark.profile_label("4k", "hevc", "4k", "hevc", True, False, True),
                         "HEVC (your file) -> 4K HEVC 10-bit")


class TestClipManifest(unittest.TestCase):
    """Clips ship as pinned GitHub Release assets (clips-v1); the manifest (name → sha256+size)
    is baked into the image and every download is verified against it before use."""
    def test_manifest_covers_all_shipped_pairs(self):
        for res, codecs in benchmark.SOURCE_CODECS_BY_RES.items():
            for c in codecs:
                name = os.path.basename(benchmark.clip_master(res, c))
                self.assertIn(name, benchmark.CLIP_MANIFEST, f"missing manifest for {res}/{c}")

    def test_manifest_entries_shape(self):
        for name, (sha, size) in benchmark.CLIP_MANIFEST.items():
            self.assertEqual(len(sha), 64)
            self.assertTrue(int(sha, 16) >= 0)     # valid hex
            self.assertGreater(size, 1024 * 1024)  # every clip is > 1 MiB
            self.assertTrue(name.startswith("source_") and name.endswith(".mkv"))

    def test_url_is_pinned_release(self):
        self.assertIn("/releases/download/clips-v1/", benchmark.CLIPS_BASE_URL)
        self.assertTrue(benchmark.CLIPS_BASE_URL.startswith("https://"))


class TestClipVerify(unittest.TestCase):
    def test_verify_file_hash(self):
        import hashlib, tempfile
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"hello clips")
            p = f.name
        good = hashlib.sha256(b"hello clips").hexdigest()
        self.assertTrue(benchmark.verify_file_hash(p, good))
        self.assertFalse(benchmark.verify_file_hash(p, "0" * 64))
        os.unlink(p)
        self.assertFalse(benchmark.verify_file_hash(p, good))   # missing file → False

    def test_cached_ok_checks_exact_size(self):
        import tempfile
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"x" * 100)
            p = f.name
        self.assertTrue(benchmark.cached_ok(p, 100))
        self.assertFalse(benchmark.cached_ok(p, 101))           # size mismatch = corrupt
        os.unlink(p)
        self.assertFalse(benchmark.cached_ok(p, 100))


class TestClipResolve(unittest.TestCase):
    """Resolution order: image /app/clips (transition-era images) → /config/clips cache →
    needs download. Trust cache by existence + exact manifest size."""
    def setUp(self):
        import tempfile
        self.img = tempfile.mkdtemp()
        self.cache = tempfile.mkdtemp()
        self._old = (benchmark.CLIPS_DIR, benchmark.CLIPS_CACHE_DIR)
        benchmark.CLIPS_DIR, benchmark.CLIPS_CACHE_DIR = self.img, self.cache

    def tearDown(self):
        benchmark.CLIPS_DIR, benchmark.CLIPS_CACHE_DIR = self._old

    def test_image_copy_wins(self):
        name = "source_4k_hevc.mkv"
        with open(os.path.join(self.img, name), "wb") as f:
            f.write(b"img")
        p, status = benchmark.resolve_clip(name)
        self.assertEqual(p, os.path.join(self.img, name))
        self.assertEqual(status, "shipped")

    def test_cache_used_when_size_matches(self):
        name = "source_4k_hevc.mkv"
        size = benchmark.CLIP_MANIFEST[name][1]
        with open(os.path.join(self.cache, name), "wb") as f:
            f.seek(size - 1)
            f.write(b"\0")
        p, status = benchmark.resolve_clip(name)
        self.assertEqual(p, os.path.join(self.cache, name))
        self.assertEqual(status, "cached")

    def test_wrong_size_cache_is_ignored(self):
        name = "source_4k_hevc.mkv"
        with open(os.path.join(self.cache, name), "wb") as f:
            f.write(b"truncated")
        p, status = benchmark.resolve_clip(name)
        self.assertIsNone(p)
        self.assertEqual(status, "missing")


class TestClipStageHash(unittest.TestCase):
    """The staged clip is hashed WHILE being copied into the ramdisk (same bytes, one pass) and
    RE-hashed when a previously staged copy is reused — closing the swap-after-stage hole. This
    binds a submission to a correctly staged pinned clip; it cannot (nothing client-side can)
    prove what ffmpeg consumed on user-owned hardware."""
    def setUp(self):
        import tempfile, hashlib
        self.tmp = tempfile.mkdtemp()
        self.ram = tempfile.mkdtemp()
        self._old = (benchmark.CLIPS_DIR, benchmark.CLIPS_CACHE_DIR, benchmark.RAMDISK,
                     benchmark.SOURCE, dict(benchmark.CLIP_MANIFEST), benchmark._STAGED)
        benchmark.CLIPS_DIR, benchmark.CLIPS_CACHE_DIR, benchmark.RAMDISK = self.tmp, self.tmp, self.ram
        benchmark.SOURCE = os.path.join(self.ram, "source.mkv")
        self.data = b"pinned-bitstream-" * 1000
        self.sha = hashlib.sha256(self.data).hexdigest()
        name = "source_4k_hevc.mkv"
        with open(os.path.join(self.tmp, name), "wb") as f:
            f.write(self.data)
        benchmark.CLIP_MANIFEST = {name: (self.sha, len(self.data))}
        benchmark._STAGED = None

    def tearDown(self):
        (benchmark.CLIPS_DIR, benchmark.CLIPS_CACHE_DIR, benchmark.RAMDISK,
         benchmark.SOURCE, benchmark.CLIP_MANIFEST, benchmark._STAGED) = self._old

    def test_copy_with_sha256(self):
        dst = os.path.join(self.ram, "out.bin")
        sha = benchmark.copy_with_sha256(os.path.join(self.tmp, "source_4k_hevc.mkv"), dst)
        self.assertEqual(sha, self.sha)
        with open(dst, "rb") as f:
            self.assertEqual(f.read(), self.data)

    def test_fresh_stage_records_hash_and_verifies(self):
        p = benchmark.stage_clip("4k", "hevc")
        self.assertEqual(p, benchmark.SOURCE)
        self.assertTrue(benchmark._STAGED_VERIFIED)
        self.assertEqual(benchmark._STAGED_SHA, self.sha)

    def test_reuse_rehashes_and_heals_a_swapped_file(self):
        benchmark.stage_clip("4k", "hevc")
        # attacker swaps the STAGED copy for an easier file of the same size
        with open(benchmark.SOURCE, "wb") as f:
            f.write(b"x" * len(self.data))
        p = benchmark.stage_clip("4k", "hevc")     # second run: must NOT trust the stale hash
        self.assertEqual(p, benchmark.SOURCE)
        self.assertEqual(benchmark._STAGED_SHA, self.sha)   # re-staged from the master
        self.assertTrue(benchmark._STAGED_VERIFIED)
        with open(benchmark.SOURCE, "rb") as f:
            self.assertEqual(f.read(), self.data)  # the tampered copy was replaced

    def test_reuse_with_intact_file_stays_verified(self):
        benchmark.stage_clip("4k", "hevc")
        p = benchmark.stage_clip("4k", "hevc")
        self.assertTrue(benchmark._STAGED_VERIFIED)
        self.assertEqual(benchmark._STAGED_SHA, self.sha)

    def test_corrupt_cache_heals_by_redownload(self):
        # a bit-flipped CACHED clip keeps its size (passes resolve_clip) but fails the stage
        # hash — stage must delete it and re-fetch rather than run local-only forever. In this
        # test CLIPS_DIR == CLIPS_CACHE_DIR is split so the master counts as CACHED.
        import tempfile
        cache2 = tempfile.mkdtemp()
        benchmark.CLIPS_DIR = tempfile.mkdtemp()          # empty: nothing "shipped"
        benchmark.CLIPS_CACHE_DIR = cache2
        bad = os.path.join(cache2, "source_4k_hevc.mkv")
        with open(bad, "wb") as f:
            f.write(b"z" * len(self.data))                # same size, wrong bytes
        fetched = {"n": 0}
        def fake_download(name, dest_dir, progress_cb=None, abort_event=None):
            fetched["n"] += 1
            p = os.path.join(dest_dir, name)
            with open(p, "wb") as f:
                f.write(self.data)                        # the pinned bitstream
            return p
        old_dl = benchmark.download_clip
        benchmark.download_clip = fake_download
        try:
            benchmark.stage_clip("4k", "hevc")
        finally:
            benchmark.download_clip = old_dl
        self.assertEqual(fetched["n"], 1)                 # corrupt cache triggered ONE re-fetch
        self.assertTrue(benchmark._STAGED_VERIFIED)
        self.assertEqual(benchmark._STAGED_SHA, self.sha)


class TestRunComparable(unittest.TestCase):
    """clip_verified joins the comparable gate: a locally generated (hash-mismatched) clip can
    never produce a leaderboard-eligible run."""
    ARGS = dict(mode="streaming", source_res="4k", target_res="1080p", custom_source=False,
                is_cpu=False, threshold=1.0, hold=25, settle=5, clip_verified=True)

    def test_canonical_comparable(self):
        self.assertTrue(benchmark.is_run_comparable(**self.ARGS))

    def test_unverified_clip_blocks(self):
        self.assertFalse(benchmark.is_run_comparable(**{**self.ARGS, "clip_verified": False}))

    def test_existing_gates_still_hold(self):
        for k, v in [("mode", "convert"), ("custom_source", True),
                     ("threshold", 0.9), ("hold", 12), ("settle", 2), ("target_res", "720p")]:
            self.assertFalse(benchmark.is_run_comparable(**{**self.ARGS, k: v}), k)

    def test_cpu_runs_are_comparable(self):
        # CPU software runs joined the leaderboard (2026-07-18): same clips, same rules, locked
        # veryfast preset — as comparable as any GPU run (server enforces preset/encoder)
        self.assertTrue(benchmark.is_run_comparable(**{**self.ARGS, "is_cpu": True}))


class TestBatchJobs(unittest.TestCase):
    """A batch is a list of {gpu, input_codec, subs} jobs at the current output. Sweeps cover
    every supported 4K source per device (device-grouped, shipped order); the subtitles toggle
    applies to every job EXCEPT hdr, which sits out of a subs sweep visibly."""
    IGPU = {"idx": 0, "name": "iGPU", "vendor": "intel", "available": True,
            "decodes": ["h264", "hevc", "av1", "hdr"], "codecs": ["h264", "hevc"]}
    DGPU = {"idx": 1, "name": "dGPU", "vendor": "nvidia", "available": True,
            "decodes": ["h264", "hevc", "av1", "hdr"], "codecs": ["h264", "hevc", "av1"]}
    AMD = {"idx": 2, "name": "amd", "vendor": "amd", "available": True,
           "decodes": ["h264", "hevc", "av1"], "codecs": ["h264", "hevc", "av1"]}  # no hdr
    CPU = {"idx": 3, "name": "cpu", "vendor": "cpu", "available": True,
           "decodes": ["h264", "hevc", "av1", "hdr"], "codecs": ["h264", "hevc", "av1"]}

    def test_sweep_all_sources_device_grouped(self):
        jobs, skipped = benchmark.build_batch_jobs([self.IGPU, self.AMD], "sweep", "h264",
                                                   False, "hevc")
        labels = [(j["gpu"]["name"], j["input_codec"], j["subs"]) for j in jobs]
        self.assertEqual(labels, [
            ("iGPU", "h264", False), ("iGPU", "hevc", False), ("iGPU", "av1", False),
            ("iGPU", "hdr", False),
            ("amd", "h264", False), ("amd", "hevc", False), ("amd", "av1", False)])
        # the AMD lacks HDR tone-mapping — that gap must be VISIBLE, not silently missing
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["gpu"], "amd")
        self.assertIn("tone-map", skipped[0]["reason"])

    def test_subs_sweep_skips_hdr_with_reason(self):
        jobs, skipped = benchmark.build_batch_jobs([self.IGPU], "sweep", "h264", True, "hevc")
        self.assertEqual([(j["input_codec"], j["subs"]) for j in jobs],
                         [("h264", True), ("hevc", True), ("av1", True)])
        self.assertEqual(len(skipped), 1)
        self.assertIn("HDR", skipped[0]["reason"])

    def test_device_that_cannot_encode_output_is_skipped(self):
        jobs, skipped = benchmark.build_batch_jobs([self.IGPU, self.DGPU], "sweep", "av1",
                                                   False, "hevc")
        self.assertTrue(all(j["gpu"]["name"] == "dGPU" for j in jobs))
        self.assertEqual(skipped[0]["gpu"], "iGPU")
        self.assertIn("encode", skipped[0]["reason"])

    def test_current_kind_uses_selection(self):
        jobs, skipped = benchmark.build_batch_jobs([self.IGPU, self.CPU], "current", "h264",
                                                   True, "av1")
        self.assertEqual([(j["gpu"]["name"], j["input_codec"], j["subs"]) for j in jobs],
                         [("iGPU", "av1", True), ("cpu", "av1", True)])

    def test_current_kind_skips_nondecoding_device(self):
        jobs, skipped = benchmark.build_batch_jobs([self.AMD], "current", "h264", False, "hdr")
        self.assertEqual(jobs, [])
        self.assertIn("tone-map", skipped[0]["reason"])

    def test_sweep_source_subset(self):
        # the panel's multi-select: only the chosen sources run, shipped order preserved
        jobs, skipped = benchmark.build_batch_jobs([self.IGPU], "sweep", "h264", False, "hevc",
                                                   sources=["av1", "hevc"])
        self.assertEqual([j["input_codec"] for j in jobs], ["hevc", "av1"])
        self.assertEqual(skipped, [])

    def test_sweep_subset_ignores_unknown_and_none_means_all(self):
        jobs, _ = benchmark.build_batch_jobs([self.IGPU], "sweep", "h264", False, "hevc",
                                             sources=None)
        self.assertEqual(len(jobs), 4)


if __name__ == "__main__":
    unittest.main()
