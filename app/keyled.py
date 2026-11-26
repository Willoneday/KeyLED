# -*- coding: utf-8 -*-
"""KeyLED - native lighting driver for AULA WIN 60 HE (Fuhlen A HUB family).

Why this exists: the browser version stops animating the moment its tab is
hidden, throttled or closed. This is a plain desktop process: the render
thread is never throttled, it starts with Windows, and it lives in the tray.

Protocol (reverse engineered, see ../PROTOCOL.md):
    HID  VID_2E3C / PID_C365 / MI_02, UsagePage 0xFF1B, Usage 0x91
    report id 1, 64 bytes total
    [0x09, 0x00, 0x00, page, len, ...data]
    8 pages: 7 x 54 + 1 x 18 = 396 bytes = 132 slots x RGB
    the board keeps this frame after power loss

One module on purpose: the exe is built from this file plus ui.html (see
build-exe.bat). It reads top to bottom, and each section only calls the ones
above it:

     1 constants     identity, key geometry, effect catalogue, process wiring
     2 resources     bundled files, keyled.log
     3 colour maths  pure helpers shared by every effect
     4 LOVE clip     the bundled 268-frame animation
     5 audio         WASAPI loopback -> 16 band levels
     6 effects       one function per effect; gen_frame dispatches
     7 HID           open the keyboard, write one frame
     8 window        native drag, rounded corners
     9 config        config.json / presets.json, autostart
    10 state edits   the only functions that mutate a state dict
    11 engine        the render thread
    12 js API        everything ui.html may call
    13 tray
    14 lifecycle     exit policy, self-check, main()

Threading. pywebview runs every bridge call on a fresh thread, so the API,
the tray and the render loop all run at once. Two rules keep that safe:
a state dict is mutated only through section 10 while holding Engine.lock,
and renderers only ever read. The state dict looks like this:

    {"effect": "fire",                   # an id from EFFECTS
     "colors": ["#ff4d4d", "#ff9a00"],   # palette in use; it belongs to "effect"
     "palettes": {"fire": [...]},        # the user's override, per effect
     "speed": 1.0, "bright": 1.0, "dir": 1,
     "frame": 0, "frame_play": False,    # canvas cursor and playback
     "custom_frames": [["#rrggbb" x 61], ...]}   # the canvas, 1..600 frames
"""

import ctypes
import json
import math
import os
import sys
import threading
import time
import winreg
from ctypes import wintypes


# ---------------------------------------------------------------- identity
APP = "KeyLED"
GUID = "{4d1e55b2-f16f-11cf-88cb-001111000030}"
MUTEX = "KeyLED_SingleInstance_v1"
PROBE_MUTEX = "KeyLED_Probe"
ERROR_ALREADY_EXISTS = 183


class Ctx:
    """Process-wide wiring: the engine, the window, the tray icon.

    A module-level object rather than attributes on Api, because pywebview
    walks every public attribute of the js_api object and recurses into any
    non-callable one. Api must therefore stay a bag of methods.
    """

    def __init__(self):
        self.eng = None        # Engine, set in main()
        self.win = None        # pywebview Window, set in main()
        self.hwnd = None       # our top-level window, found once by own_hwnd()
        self.tray = None       # pystray.Icon
        self.tray_ok = False   # the icon came up: hiding to the tray is safe
        self.quitting = False  # a close is now a real exit
        self.maxed = False     # window toggle state


CTX = Ctx()


# ---------------------------------------------------------------- key layout
ROWS = [
    [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2],
    [1.5, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1.5],
    [1.75, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2.25],
    [2.25, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2.75],
    [1.25, 1.25, 1.25, 6.25, 1.25, 1.25, 1.25, 1.25],
]

# LED slot per key, calibrated on real hardware.
SLOT = [
    22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 36,
    44, 45, 46, 47, 48, 49, 50, 51, 52, 53, 54, 55, 56, 58,
    66, 68, 69, 70, 71, 72, 73, 74, 75, 76, 77, 78, 80,
    88, 90, 91, 92, 93, 94, 95, 96, 97, 98, 99, 100,
    110, 111, 112, 116, 119, 120, 121, 122,
]

NKEY = 61
MAX_FRAMES = 600
UNITS = 15.0
PAGE_LEN = [54, 54, 54, 54, 54, 54, 54, 18]
TOTAL = 396


def _key_geometry():
    """x offset, width (in key units) and row of every key, in SLOT order."""
    kx, kw, kr = [], [], []
    for r, row in enumerate(ROWS):
        x = 0.0
        for w in row:
            kx.append(x)
            kw.append(w)
            kr.append(r)
            x += w
    return kx, kw, kr


KX, KW, KR = _key_geometry()


# ------------------------------------------------------------------ effects
EFFECTS = [
    # 官方固件同款：固件自己的 21 种靠 Fn+PgUp 切换，这里是 PC 端复刻
    ("solid",    "常亮",   "单色",  "official"),
    ("breath",   "呼吸",   "起伏",  "official"),
    ("rainbow",  "彩虹",   "色相",  "official"),
    ("ripple",   "涟漪",   "扩散",  "official"),
    ("spectrum", "光谱",   "循环",  "official"),
    ("raindrop", "雨滴",   "下落",  "official"),
    ("scan",     "扫描",   "横扫",  "official"),
    # 软件自带
    ("love",     "LOVE · 爱心 · 心跳 · 星空 · 扫光 · 彩虹", "268帧", "studio"),
    ("love4",    "LOVE 四色", "跟随轮廓", "studio"),
    ("fire",     "火焰",   "自下",  "studio"),
    ("heart",    "心跳",   "双拍",  "studio"),
    ("sweep",    "扫光",   "光带",  "studio"),
    ("star",     "星空",   "闪烁",  "studio"),
    ("beat",     "音乐律动", "随音量堆叠", "studio"),
    # 自定义
    ("custom",   "画板",   "逐键",  "custom"),
]
EFFECT_IDS = [e[0] for e in EFFECTS]

# Each effect owns a palette so switching back and forth does not smear one
# colour scheme over everything. Users may override any of them.
DEFAULT_PALETTES = {
    "solid":    ["#3d7dff"],
    "breath":   ["#5ee0a0"],
    "rainbow":  ["#ff2d55"],
    "ripple":   ["#3d7dff", "#5ee0a0"],
    "spectrum": ["#5b8cff"],
    "raindrop": ["#22d3ee"],
    "scan":     ["#5ee0a0"],
    "love":     ["#f31233", "#ff8a3d"],
    "love4":    ["#f31233", "#5b8cff", "#5ee0a0", "#ffd166"],
    "fire":     ["#ff4d4d", "#ff9a00"],
    "heart":    ["#f31233"],
    "sweep":    ["#ff4d4d", "#ffd166", "#5ee0a0", "#5b8cff"],
    "star":     ["#e6ecf6"],
    "beat":     ["#3d7dff", "#5ee0a0"],
    "custom":   ["#ffffff"],
}

GROUPS = [
    ("official", "官方预设", "固件同款，PC 端复刻"),
    ("studio",   "软件灯效", "动画与合成效果"),
    ("custom",   "自定义",   "逐键手绘，可多帧"),
]


# ---------------------------------------------------------------- resources
def res(name):
    """Path to a bundled resource, works both in-source and inside the exe."""
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, name)


def beside_exe(name):
    """A file the user dropped next to the exe, or None if there is none.

    Only looked for in a packaged build, so a stray file in the source tree
    can never shadow what the build ships.
    """
    if not getattr(sys, "frozen", False):
        return None
    path = os.path.join(os.path.dirname(sys.executable), name)
    return path if os.path.exists(path) else None


# ------------------------------------------------------------------- logging
def log_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "KeyLED")


def log(msg):
    """One line to keyled.log, which starts over once it passes 400 KB."""
    try:
        d = log_dir()
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "keyled.log")
        if os.path.exists(p) and os.path.getsize(p) > 400000:
            os.remove(p)
        with open(p, "a", encoding="utf-8") as f:
            f.write(time.strftime("%m-%d %H:%M:%S") + "  " + str(msg) + "\n")
    except Exception:
        pass


# --------------------------------------------------------------- colour math
def clamp(v, a, b):
    return a if v < a else (b if v > b else v)


def hex2rgb(h):
    """"#rrggbb" -> [r, g, b]. Anything it cannot parse is black, never an
    error: the value may come straight from a hand-edited config or preset."""
    if not isinstance(h, str):
        return [0, 0, 0]
    h = h.strip().lstrip("#")
    if len(h) == 8 and all(c in "0123456789abcdefABCDEF" for c in h):
        h = h[:6]            # CSS #rrggbbaa; the board has no alpha
    if len(h) == 3:
        h = h[0] * 2 + h[1] * 2 + h[2] * 2
    try:
        n = int(h, 16)
    except ValueError:
        return [0, 0, 0]
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255]


def mix(a, b, t):
    return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t]


def mul(c, f):
    return [c[0] * f, c[1] * f, c[2] * f]


def hsv(h, s, v):
    h = (h % 1 + 1) % 1
    i = int(math.floor(h * 6))
    f = h * 6 - i
    p = v * (1 - s)
    q = v * (1 - f * s)
    t = v * (1 - (1 - f) * s)
    k = i % 6
    if k == 0:
        return [v * 255, t * 255, p * 255]
    if k == 1:
        return [q * 255, v * 255, p * 255]
    if k == 2:
        return [p * 255, v * 255, t * 255]
    if k == 3:
        return [p * 255, q * 255, v * 255]
    if k == 4:
        return [t * 255, p * 255, v * 255]
    return [v * 255, p * 255, q * 255]


def hs(i):
    """Deterministic pseudo-random 0..1, the noise source behind fire and star."""
    x = math.sin(i * 127.1 + 311.7) * 43758.5453
    return x - math.floor(x)


def pal_rgb(st):
    """The palette in use as RGB; white if the state somehow has no colours."""
    return [hex2rgb(c) for c in st["colors"]] or [[255, 255, 255]]


def pal_at(p, cs):
    """Colour at position p along a gradient through palette cs. p wraps into
    0..1, so the gradient loops; a one-colour palette is a flat fill."""
    if len(cs) == 1:
        return cs[0]
    n = len(cs)
    x = ((p % 1) + 1) % 1 * n
    i = int(math.floor(x)) % n
    return mix(cs[i], cs[(i + 1) % n], x - math.floor(x))



# ------------------------------------------------------------------ LOVE clip


def _love_from_json(path):
    """Vendor-exported animation: {"name":..,"speed":100,"data":["rgb(r, g, b)", ...]}"""
    import re
    pat = re.compile(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)")
    with open(path, "r", encoding="utf-8") as f:
        d = json.load(f)
    arr = d["data"] if isinstance(d, dict) else d
    if len(arr) % NKEY != 0:
        raise ValueError("frame count %d is not a multiple of %d" % (len(arr), NKEY))
    out = bytearray()
    for s in arr:
        if isinstance(s, str):
            m = pat.match(s)
            if not m:
                out += b"\x00\x00\x00"
                continue
            out += bytes((min(255, int(m.group(1))),
                          min(255, int(m.group(2))),
                          min(255, int(m.group(3)))))
        elif isinstance(s, (list, tuple)) and len(s) >= 3:
            out += bytes((min(255, int(s[0])), min(255, int(s[1])), min(255, int(s[2]))))
        else:
            out += b"\x00\x00\x00"
    return bytes(out)


class LoveClip:
    """268 pre-baked frames, 61 keys each, 10 fps -> a 26.8 s loop.

    Every value of every frame is fixed in the clip, which is why the order
    in which keys light up cannot be changed from the palette.
    """

    FPS = 10.0

    def __init__(self):
        self.data = b""
        self.n = 0

    def load(self):
        data, src = None, ""
        # A data.json dropped next to the exe lets people swap clips without repacking.
        swap = beside_exe("data.json")
        if swap:
            try:
                data = _love_from_json(swap)
                src = "data.json next to exe"
            except Exception as e:
                log("love json failed: %s" % e)
        if data is None:
            try:
                with open(res("love.bin"), "rb") as f:
                    data = f.read()
                src = "bundled love.bin"
            except Exception as e:
                log("love bin missing: %s" % e)
        stride = NKEY * 3
        if data and len(data) % stride == 0:
            self.data = data
            self.n = len(data) // stride
            log("love clip: %d frames from %s" % (self.n, src))
        else:
            log("love clip: unavailable")

    def frame(self, t, sp, bright):
        """The whole board at time t with brightness applied, or None without a clip."""
        if not self.n:
            return None
        src = self.data
        off = (int(t * self.FPS * sp) % self.n) * NKEY * 3
        return [min(255, int(v * bright)) for v in src[off:off + NKEY * 3]]


LOVE = LoveClip()




# --------------------------------------------------------------- audio input
NBAND = 16
_FMIN, _FMAX = 55.0, 12000.0


class AudioMonitor:
    """WASAPI loopback -> 16 log-spaced band levels in [0, 1].

    Follows whatever Windows is *playing*. Loopback is the only reliable
    source: with headphones plugged in "Stereo Mix" sees nothing, and a
    microphone hears the room, not the music.

    One thread owns the PortAudio session. The analyser runs on PortAudio's
    callback thread and only assigns floats and list slots here, which is
    atomic enough under the GIL for the render thread to read without a lock.
    """

    RETRY = 5.0         # wait after a failed attempt, so a device that is not
                        # there costs one enumeration per few seconds, not per frame
    LIVE_FOR = 2.0      # "live": a buffer carrying signal arrived within this time
    CHECK_EVERY = 2.0   # how often to notice that the default output changed

    def __init__(self):
        self.dev = ""        # output device being tapped
        self.err = ""        # last failure, shown by the UI
        self.last = 0.0      # time of the last buffer that carried signal
        self.level = 0.0
        self.bands = [0.0] * NBAND
        self._floor = 0.0
        self._wanted = False
        self._dead = False   # pyaudiowpatch could not be imported; retrying is pointless
        self._thread = None
        self._wake = threading.Event()

    def live(self):
        return time.time() - self.last < self.LIVE_FOR

    def sync(self, wanted):
        """The engine calls this every frame: capture only while an effect needs it."""
        alive = self._thread is not None and self._thread.is_alive()
        if wanted:
            if not alive and not self._dead:
                self.start()
        elif self._wanted:
            self.stop()

    def start(self):
        self.err = ""
        self._wanted = True
        if self._thread is not None and self._thread.is_alive():
            return          # already running
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        """Release the output device the moment the effect is switched away."""
        if not self._wanted:
            return
        self._wanted = False
        self._wake.set()
        self.last = 0.0
        self.level = 0.0
        log("audio released")

    def _run(self):
        self._wake.clear()
        try:
            import pyaudiowpatch as pa
        except Exception as e:
            self.err = "缺少 pyaudiowpatch"
            self._dead = True
            log("audio import failed: %s" % e)
            return
        try:
            import numpy as np
        except Exception as e:
            np = None    # the stream still opens; the level simply stays at 0
            log("numpy missing, audio level stays 0: %s" % e)

        while self._wanted:
            try:
                self._capture(pa, np)
                pause = 0.4
            except Exception as e:
                self.err = str(e)[:90]
                log("audio failed: %s" % e)
                pause = self.RETRY
            if self._wanted:
                self._wake.wait(pause)

    @staticmethod
    def _pick_loopback(pa, p):
        """The default output device's name, and the loopback device to tap for
        it: the one whose name contains the output's, or any loopback device
        when no name matches."""
        wasapi = p.get_host_api_info_by_type(pa.paWASAPI)
        out_idx = int(wasapi["defaultOutputDevice"])
        out_name = p.get_device_info_by_index(out_idx)["name"]
        loops = list(p.get_loopback_device_info_generator())
        target = None
        for lb in loops:
            if out_name in lb["name"]:
                target = lb
                break
        if target is None and loops:
            target = loops[0]
        return out_name, target

    def _capture(self, pa, np):
        """One loopback session. Returns when the default output device changes,
        so switching between speakers / headset / HDMI keeps working."""
        p = None
        stream = None
        err = [False]
        try:
            p = pa.PyAudio()
            out_name, target = self._pick_loopback(pa, p)
            if target is None:
                raise RuntimeError("没有可用的环回设备")

            rate = int(target["defaultSampleRate"])
            ch = int(target["maxInputChannels"])
            self.dev = out_name
            self.err = ""
            self._floor = 0.0

            def cb(in_data, frame_count, tinfo, status):
                try:
                    self._analyze(np, in_data, ch, rate)
                except Exception as e:
                    # log once: a failing analyser otherwise looks like silence
                    if not err[0]:
                        err[0] = True
                        log("analyze failed: %s" % e)
                return (None, pa.paContinue)

            stream = p.open(format=pa.paInt16, channels=ch, rate=rate, input=True,
                            input_device_index=int(target["index"]),
                            frames_per_buffer=1024, stream_callback=cb)
            stream.start_stream()
            log("loopback open: %s %dHz %dch" % (out_name, rate, ch))

            last_check = time.time()
            while self._wanted:
                self._wake.wait(0.15)
                now = time.time()
                if now - last_check > self.CHECK_EVERY:
                    last_check = now
                    try:
                        idx = int(p.get_host_api_info_by_type(pa.paWASAPI)["defaultOutputDevice"])
                        if p.get_device_info_by_index(idx)["name"] != out_name:
                            log("output device changed, reopening")
                            return
                    except Exception:
                        return
        finally:
            try:
                if stream:
                    stream.stop_stream()
                    stream.close()
            except Exception:
                pass
            try:
                if p:
                    p.terminate()
            except Exception:
                pass

    def _analyze(self, np, data, ch, rate):
        """int16 frames -> 16 log-spaced band levels (0..1)."""
        if np is None:
            return
        n = len(data) // 2
        if n < ch * 128:
            return
        arr = np.frombuffer(data, dtype=np.int16)
        if ch >= 2:
            arr = arr[: (len(arr) // ch) * ch].reshape(-1, ch).mean(axis=1)
        if len(arr) < 128:
            return

        af = arr.astype(np.float32)
        rms = float(np.sqrt((af * af).mean()))

        # Noise floor may only creep up while we are already near silence, and is
        # capped. Letting it track loud passages makes sustained music get absorbed
        # after a few seconds, which looked like "sound plays but nothing lights".
        fl = self._floor
        if fl <= 0.0 or rms < fl:
            self._floor = rms
        elif rms < fl * 1.4 and fl < 800.0:
            self._floor = min(800.0, fl + (rms - fl) * 0.004)
        eff = rms - self._floor * 1.8

        if eff < 110.0:
            bands = self.bands
            for i in range(NBAND):
                bands[i] *= 0.68
            self.level *= 0.68
            return

        win = np.hanning(len(af)).astype(np.float32)
        spec = np.abs(np.fft.rfft(af * win))
        freqs = np.fft.rfftfreq(len(af), 1.0 / float(rate))

        edges = _FMIN * (_FMAX / _FMIN) ** (np.arange(NBAND + 1) / float(NBAND))
        idx = np.searchsorted(freqs, edges)
        # |FFT| -> signal amplitude: windowed single-sided peak is mag * 4 / N
        amp_scale = 4.0 / (len(af) * 32768.0)

        bands = self.bands
        for i in range(NBAND):
            a = int(idx[i])
            b = int(idx[i + 1])
            if b <= a:
                b = min(a + 1, len(spec))
            # take the peak bin, not the mean: log-spaced bands get much wider at
            # the top, and averaging dilutes a tone by the band width
            mag = float(spec[a:b].max()) if b > a else 0.0
            ratio = mag * amp_scale
            db = 20.0 * math.log10(ratio + 1e-7)
            v = (db + 48.0) / 42.0          # -48dB..-6dB  ->  0..1  (i.e. 0..5 rows)
            v = 0.0 if v < 0.0 else (1.0 if v > 1.0 else v)
            bands[i] += (v - bands[i]) * 0.5

        lv = min(1.0, eff / 32768.0 * 10.0)
        self.level += (lv - self.level) * 0.5
        self.last = time.time()


AUDIO = AudioMonitor()


# ------------------------------------------------------------------- effects
# One function per effect: (st, t) -> 61 * 3 values with brightness applied.
# The arithmetic is what each effect *is*, and the keyboard is the only place
# it shows, so it is kept exactly as it has always been. What changed is only
# where it is computed: everything that does not depend on the key is lifted
# out of the per-key loop, and the per-key constants live in tables.
KXN = [x / UNITS for x in KX]                                    # left edge, 0..1
KCEN = [clamp((x + w / 2) / UNITS, 0, 0.9999) for x, w in zip(KX, KW)]   # centre, 0..1
KBI = [int(c * NBAND) for c in KCEN]                             # band under each key
KLR = [(4 - r) + 0.5 for r in KR]                                # row height for "beat"
KCOL = [int(math.floor(x)) for x in KX]                          # 1u column a key starts in
KHS = [hs(i) for i in range(NKEY)]                               # per-key flicker for "star"
KPHASE = [hs(c * 7 + 3) for c in KCOL]                           # per-column phase for "raindrop"


def _ripple_distance(kx, kwd, krow):
    """Distance from the ripple origin (board centre, middle row)."""
    dx = (kx + kwd / 2) - 7.6
    dy = (krow - 2) * 1.6
    return math.sqrt(dx * dx + dy * dy)


RIP_DIST = [_ripple_distance(x, w, r) for x, w, r in zip(KX, KW, KR)]


def _lit(out, i, c, b):
    """Write colour c of key i into out, brightness b applied."""
    j = i * 3
    out[j] = int(clamp(c[0] * b, 0, 255))
    out[j + 1] = int(clamp(c[1] * b, 0, 255))
    out[j + 2] = int(clamp(c[2] * b, 0, 255))


def _lit_all(c, b):
    """A whole frame of one colour."""
    return [int(clamp(c[0] * b, 0, 255)), int(clamp(c[1] * b, 0, 255)), int(clamp(c[2] * b, 0, 255))] * NKEY


def _fx_solid(st, t):
    return _lit_all(pal_rgb(st)[0], st["bright"])


def _fx_breath(st, t):
    c = mul(pal_rgb(st)[0], 0.12 + 0.88 * (0.5 + 0.5 * math.sin(t * math.pi * st["speed"])))
    return _lit_all(c, st["bright"])


def _fx_rainbow(st, t):
    out = [0] * (NKEY * 3)
    off = t * 0.06 * st["speed"] * st["dir"]
    for i in range(NKEY):
        h = (KXN[i] * 0.85 + off) % 1
        _lit(out, i, hsv((h + 1) % 1, 1, 1), st["bright"])
    return out


def _fx_star(st, t):
    out = [0] * (NKEY * 3)
    c1, b = pal_rgb(st)[0], st["bright"]
    base = t * st["speed"] * 0.55
    for i in range(NKEY):
        ph = (base + KHS[i]) % 1
        f = math.sin(ph / 0.16 * math.pi) if ph < 0.16 else 0.0
        _lit(out, i, mul(c1, f * 1.1), b)
    return out


def _fx_heart(st, t):
    ph = (t * st["speed"]) % 1
    p1 = math.exp(-((ph - 0.12) / 0.055) ** 2)
    p2 = 0.75 * math.exp(-((ph - 0.34) / 0.055) ** 2)
    return _lit_all(mul(pal_rgb(st)[0], clamp(p1 + p2, 0, 1)), st["bright"])


def _fx_sweep(st, t):
    out = [0] * (NKEY * 3)
    cs, b = pal_rgb(st), st["bright"]
    pos = ((t * st["speed"] * 0.32) % 1) * 19 - 2
    for i in range(NKEY):
        dd = abs(KX[i] - pos)
        _lit(out, i, mul(pal_at(KXN[i], cs), 0.12 + 0.88 * clamp(1 - dd / 2.4, 0, 1)), b)
    return out


def heat_color(h):
    """Black -> red -> yellow -> white by heat 0..1."""
    if h < 0.3:
        return [h / 0.3 * 150, 0, 0]
    if h < 0.62:
        q = (h - 0.3) / 0.32
        return [150 + q * 105, q * 135, 0]
    q = (h - 0.62) / 0.38
    return [255, 135 + q * 120, q * 130]


def _fx_fire(st, t):
    out = [0] * (NKEY * 3)
    base, b = pal_rgb(st)[0], st["bright"]
    tick = math.floor(t * st["speed"] * 9) * 7      # the flicker re-rolls 9 times a second
    for i in range(NKEY):
        flick = hs(int(i * 137 + tick))
        h = clamp(0.1 + KR[i] * 0.155 + flick * 0.4 - 0.06, 0, 1)
        _lit(out, i, mix(heat_color(h), mul(base, h * 1.5), 0.42), b)
    return out


def _fx_ripple(st, t):
    out = [0] * (NKEY * 3)
    cs, b, d = pal_rgb(st), st["bright"], st["dir"]
    w1 = t * st["speed"] * 0.9 * d
    w2 = t * st["speed"] * 0.05 * d
    for i in range(NKEY):
        dist = RIP_DIST[i]
        ph = ((dist * 0.24 - w1) % 1 + 1) % 1
        f2 = math.exp(-((ph - 0.5) / 0.21) ** 2)
        _lit(out, i, mul(pal_at(dist * 0.1 - w2, cs), 0.16 + 0.84 * f2), b)
    return out


def _fx_spectrum(st, t):
    out = [0] * (NKEY * 3)
    off = t * st["speed"] * 0.18
    for i in range(NKEY):
        _lit(out, i, hsv((KXN[i] * 0.55 + off) % 1, 1, 1), st["bright"])
    return out


def _fx_raindrop(st, t):
    out = [0] * (NKEY * 3)
    cs, b = pal_rgb(st), st["bright"]
    base = t * st["speed"] * 0.7
    by_col = [pal_at(c / UNITS, cs) for c in range(int(UNITS))]
    for i in range(NKEY):
        fall = ((base + KPHASE[i]) % 1.0) * 5.6 - 0.3
        f = clamp(1.0 - abs(KR[i] - fall) / 1.1, 0, 1)
        _lit(out, i, mul(by_col[KCOL[i]], 0.06 + 0.94 * f * f), b)
    return out


def _fx_scan(st, t):
    out = [0] * (NKEY * 3)
    cs, b = pal_rgb(st), st["bright"]
    pos = ((t * st["speed"] * 0.45) % 1) * 6.5 - 0.5
    for i in range(NKEY):
        dd = abs(KR[i] - pos)
        _lit(out, i, mul(pal_at(KXN[i], cs), 0.08 + 0.92 * clamp(1 - dd / 1.15, 0, 1)), b)
    return out


def _fx_beat(st, t):
    """Keys are sampled by their centre, so wide keys (space, shifts) do not
    take their left edge's band and light up as one long bar."""
    out = [0] * (NKEY * 3)
    cs, b, bands = pal_rgb(st), st["bright"], AUDIO.bands
    for i in range(NKEY):
        height = bands[KBI[i]] * 5.4
        level = KLR[i]                 # height this row sits at: 0.5 at the bottom, 4.5 at the top
        if level <= height:
            # bottom rows a bit dimmer, the peak row near white: VU feel
            c = mul(pal_at(KCEN[i], cs), 0.55 + 0.45 * min(1.0, level / 4.5))
            if level > height - 0.9:
                c = mix(c, [255, 255, 255], 0.5)
            _lit(out, i, c, b)
    return out


def _fx_love4(st, t):
    """The clip as a mask: how bright a key is decides whether it is on."""
    out = [0] * (NKEY * 3)
    if not LOVE.n:
        return out
    cs, b, data = pal_rgb(st), st["bright"], LOVE.data
    idx = int(t * LoveClip.FPS * st["speed"]) % LOVE.n
    base, shift = idx * NKEY * 3, idx / 150.0
    for i in range(NKEY):
        off = base + i * 3
        inten = max(data[off], data[off + 1], data[off + 2]) / 255.0
        if inten >= 0.06:
            _lit(out, i, mul(pal_at(i / float(NKEY) + shift, cs), inten), b)
    return out


def _fx_custom(st, t):
    """The canvas: one row of "#rrggbb" per frame."""
    out = [0] * (NKEY * 3)
    frames = st.get("custom_frames") or []
    if not frames:
        return out
    if st.get("frame_play") and len(frames) > 1:
        fi = int(t * 8.0 * st["speed"]) % len(frames)       # canvas plays at 8 fps
    else:
        fi = st.get("frame", 0)
        if not (0 <= fi < len(frames)):
            fi = 0
    if fi >= len(frames):
        return out       # the row was deleted under us between the check and here
    row = frames[fi]
    b, n = st["bright"], len(row)
    for i in range(NKEY):
        _lit(out, i, hex2rgb(row[i] if i < n else "#000000"), b)
    return out


def _fx_wave(st, t):
    """Fallback for an effect id we do not know, or LOVE with no clip loaded."""
    out = [0] * (NKEY * 3)
    cs, b, d = pal_rgb(st), st["bright"], st["dir"]
    w = t * st["speed"] * 0.11
    for i in range(NKEY):
        _lit(out, i, pal_at(KXN[i] * d + w + KR[i] * 0.012, cs), b)
    return out


# "love" is deliberately absent: it is the clip, handled in gen_frame, and it
# falls back to the wave only when the clip could not be loaded.
RENDERERS = {
    "solid": _fx_solid, "breath": _fx_breath, "rainbow": _fx_rainbow, "star": _fx_star,
    "heart": _fx_heart, "sweep": _fx_sweep, "fire": _fx_fire, "ripple": _fx_ripple,
    "spectrum": _fx_spectrum, "raindrop": _fx_raindrop, "scan": _fx_scan, "beat": _fx_beat,
    "love4": _fx_love4, "custom": _fx_custom,
}


def gen_frame(t, st):
    """One frame for the state at time t: 61 * 3 values, brightness applied."""
    if st["effect"] == "love":
        clip = LOVE.frame(t, st["speed"], st["bright"])
        if clip is not None:
            return clip
    return RENDERERS.get(st["effect"], _fx_wave)(st, t)


# ----------------------------------------------------------------------- HID
HID_ENUM = r"SYSTEM\CurrentControlSet\Enum\HID"
HID_IFACE = "VID_2E3C&PID_C365&MI_02"

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
GR, GW, SHARE, OPEN = 0x80000000, 0x40000000, 3, 3       # read, write, share r/w, OPEN_EXISTING
INVALID = ctypes.c_void_p(-1).value

k32.CreateFileW.restype = ctypes.c_void_p
k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                            ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
k32.WriteFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                          ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
k32.WriteFile.restype = wintypes.BOOL
k32.CloseHandle.argtypes = [ctypes.c_void_p]
k32.CreateMutexW.restype = ctypes.c_void_p
k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]


def open_dev():
    """Handle of the keyboard's HID interface, or None if it is absent or busy.

    A HUB holds the interface exclusively while it is running, which is what
    "等待键盘" in the title bar means. Entries of unplugged devices stay in
    the registry, so the CreateFileW is what tells present from remembered.
    """
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, HID_ENUM)
    except OSError:
        log("no HID enum key")
        return None
    with root:
        i = 0
        while True:
            try:
                name = winreg.EnumKey(root, i)
            except OSError:
                return None                         # end of the enumeration
            i += 1
            if not name.upper().startswith(HID_IFACE):
                continue
            with winreg.OpenKey(root, name) as dev:
                j = 0
                while True:
                    try:
                        inst = winreg.EnumKey(dev, j)
                    except OSError:
                        break
                    j += 1
                    path = "\\\\?\\HID#%s#%s#%s" % (name, inst, GUID)
                    h = k32.CreateFileW(path, GR | GW, SHARE, None, OPEN, 0, None)
                    if h != INVALID and h:
                        log("device opened: " + name)
                        return h


def send(h, payload):
    """One 64-byte report: id 1, then the payload, zero padded."""
    buf = bytes([1]) + bytes(payload)
    if len(buf) < 64:
        buf += b"\x00" * (64 - len(buf))
    arr = (ctypes.c_ubyte * 64).from_buffer_copy(buf)
    w = wintypes.DWORD(0)
    return bool(k32.WriteFile(h, arr, 64, ctypes.byref(w), None))


def write_frame(h, frame, page_delay=0.004):
    """Push one frame: 8 paged 0x09 writes. The gaps are what the firmware
    needs between pages; they, not the rendering, set the frame rate."""
    if not h:
        return False
    flat = bytearray(TOTAL)
    for i in range(NKEY):
        s = SLOT[i] * 3
        flat[s] = frame[i * 3]
        flat[s + 1] = frame[i * 3 + 1]
        flat[s + 2] = frame[i * 3 + 2]

    off = 0
    for page, ln in enumerate(PAGE_LEN):
        pkt = bytes([0x09, 0x00, 0x00, page, ln]) + bytes(flat[off:off + ln])
        off += ln
        if not send(h, pkt):
            log("write failed page=%d" % page)
            return False
        if page_delay:
            time.sleep(page_delay)
    return True


# -------------------------------------------------------------------- window
# Frameless windows cannot be dragged by the page on WebView2, so the page
# asks for a native caption drag instead.
_u32 = ctypes.windll.user32
_u32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_u32.SendMessageW.restype = ctypes.c_ssize_t
_u32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_u32.SetWindowRgn.argtypes = [wintypes.HWND, ctypes.c_void_p, wintypes.BOOL]
WM_NCLBUTTONDOWN = 0xA1
HTCAPTION = 2

_g32 = ctypes.windll.gdi32
_g32.CreateRoundRectRgn.restype = ctypes.c_void_p

_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def own_hwnd():
    """Top-level window of *this* process. Enumerated by pid rather than found
    by title, so another KeyLED instance can never be mistaken for ours."""
    if CTX.hwnd:
        return CTX.hwnd
    pid = os.getpid()
    found = []

    def cb(h, lparam):
        p = wintypes.DWORD(0)
        _u32.GetWindowThreadProcessId(h, ctypes.byref(p))
        if p.value == pid:
            buf = ctypes.create_unicode_buffer(128)
            _u32.GetWindowTextW(h, buf, 128)
            if buf.value == APP:
                found.append(h)
        return True

    try:
        _u32.EnumWindows(_WNDENUMPROC(cb), 0)
    except Exception as e:
        log("enum windows: %s" % e)
    if found:
        CTX.hwnd = found[0]
    return CTX.hwnd


def round_corners(radius=16, on=True):
    """Clip the window to a rounded rect. Win10 has no DWM corner API, and
    colour-key transparency would punch holes through pure-red pixels."""
    try:
        hwnd = own_hwnd()
        if not hwnd:
            return
        if not on or radius <= 0:
            _u32.SetWindowRgn(hwnd, None, True)
            return
        r = wintypes.RECT()
        _u32.GetWindowRect(hwnd, ctypes.byref(r))
        w, h = r.right - r.left, r.bottom - r.top
        if w <= 0 or h <= 0:
            return
        rgn = _g32.CreateRoundRectRgn(0, 0, w + 1, h + 1, radius * 2, radius * 2)
        _u32.SetWindowRgn(hwnd, rgn, True)
        log("corners %dx%d r=%d" % (w, h, radius))
    except Exception as e:
        log("corners: %s" % e)


def drag_window():
    """Native caption drag; the page cannot drag a frameless WebView2 window."""
    try:
        hwnd = own_hwnd()
        if hwnd:
            _u32.ReleaseCapture()
            _u32.SendMessageW(hwnd, WM_NCLBUTTONDOWN, HTCAPTION, 0)
    except Exception as e:
        log("drag failed: %s" % e)


# -------------------------------------------------------------------- config
CONFIG = None      # set below, after the class is defined


def _strings(v, limit):
    """The leading strings of v, at most limit of them; [] if v is not a list."""
    return [c for c in v if isinstance(c, str)][:limit] if isinstance(v, list) else []


def default_state():
    return {
        "effect": "love",
        "colors": ["#f31233", "#ff8a3d"],
        "palettes": {},
        "speed": 1.0,
        "bright": 1.0,
        "dir": 1,
        "frame": 0,
        "frame_play": False,
        "custom_frames": [["#000000"] * NKEY],
    }


class ConfigStore:
    """config.json: the state in use. One atomic write per change, throttled
    because the sliders write on every pixel of a drag; everything discrete
    passes force=True so it is on disk before the call returns."""

    THROTTLE = 1.0

    def __init__(self):
        self.path = os.path.join(log_dir(), "config.json")
        self._lock = threading.Lock()      # two bridge calls may save at once
        self._saved_at = 0.0

    def load(self):
        """The saved state, or the defaults. Reading stops at the first field
        that does not parse, keeping everything before it, so a truncated file
        costs the part after the damage and nothing else."""
        st = default_state()
        try:
            if not os.path.exists(self.path):
                return st
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
            if d.get("effect") in EFFECT_IDS:
                st["effect"] = d["effect"]
            cs = _strings(d.get("colors"), 5)
            if cs:
                st["colors"] = cs
            pl = d.get("palettes")
            if isinstance(pl, dict):
                st["palettes"] = {k: _strings(v, 5) for k, v in pl.items()
                                  if k in EFFECT_IDS and isinstance(v, list) and v}
            st["speed"] = clamp(float(d.get("speed", 1.0)), 0.25, 3.0)
            st["bright"] = clamp(float(d.get("bright", 1.0)), 0.0, 1.0)
            st["dir"] = -1 if int(d.get("dir", 1)) < 0 else 1

            # multi-frame canvas, with migration from the old single-frame key
            cf = d.get("custom_frames")
            if (isinstance(cf, list) and cf
                    and all(isinstance(f, list) and len(f) == NKEY for f in cf)):
                st["custom_frames"] = cf[:MAX_FRAMES]
            else:
                old = d.get("custom")
                if isinstance(old, list) and len(old) == NKEY:
                    st["custom_frames"] = [old]

            try:
                st["frame"] = max(0, min(int(d.get("frame", 0)), len(st["custom_frames"]) - 1))
            except Exception:
                st["frame"] = 0
            st["frame_play"] = bool(d.get("frame_play", False))
        except Exception as e:
            log("config load: %s" % e)

        # the palette on record for the effect wins over the top-level copy
        pal = (st.get("palettes") or {}).get(st["effect"]) or DEFAULT_PALETTES.get(st["effect"])
        if pal:
            st["colors"] = list(pal)
        return st

    def save(self, st, force=False):
        now = time.time()
        with self._lock:
            if not force and now - self._saved_at < self.THROTTLE:
                return
            self._saved_at = now
            try:
                os.makedirs(log_dir(), exist_ok=True)
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(st, f, ensure_ascii=False, indent=2)
                os.replace(tmp, self.path)
            except Exception as e:
                log("config save: %s" % e)


CONFIG = ConfigStore()


class PresetStore:
    """presets.json: whole states the user saved. The file is only ever
    rewritten as a unit, so read-modify-write happens under one lock."""

    MAX = 60

    def __init__(self):
        self.path = os.path.join(log_dir(), "presets.json")
        self.lock = threading.RLock()      # reentrant: names() may be asked inside an update
        self._stamp = None      # (mtime, size) the cached names were read at
        self._names = []

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
            # anything that is not a preset is dropped rather than breaking the list
            return [p for p in d if isinstance(p, dict)] if isinstance(d, list) else []
        except Exception:
            return []

    def save(self, lst):
        try:
            os.makedirs(log_dir(), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(lst, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except Exception as e:
            log("presets save: %s" % e)

    def names(self):
        """Just the names, for the sidebar. The page asks for them on every
        refresh and a preset can hold 600 frames, so the parsed names are kept
        until the file's size or modification time changes."""
        with self.lock:
            try:
                s = os.stat(self.path)
                stamp = (s.st_mtime_ns, s.st_size)
                if stamp != self._stamp:
                    self._names = [p.get("name", "") for p in self.load()]
                    self._stamp = stamp
            except Exception:
                self._stamp, self._names = None, []
            return list(self._names)


PRESETS = PresetStore()


# ----------------------------------------------------------------- presets
def _row(lit):
    """A canvas frame with the given keys lit purple."""
    row = ["#000000"] * NKEY
    for i in lit:
        row[i] = "#7c4dff"
    return row


# The user's own two drawings, kept exactly as painted.
BUILTIN_PRESETS = [
    {"name": "爱心", "effect": "custom", "colors": ["#7c4dff"], "speed": 1.0,
     "bright": 1.0, "dir": 1, "frame": 0, "frame_play": False,
     "custom_frames": [_row((
         8, 9, 11, 12, 21, 22, 23, 24, 25, 26,
         35, 36, 37, 38, 39, 48, 49, 50, 51, 57,
     ))]},
    {"name": "LOVE", "effect": "custom", "colors": ["#7c4dff"], "speed": 1.0,
     "bright": 1.0, "dir": 1, "frame": 0, "frame_play": False,
     "custom_frames": [_row((
         0, 3, 4, 5, 7, 10, 12, 13, 14, 16,
         19, 21, 23, 25, 28, 30, 33, 35, 36, 39,
         40, 41, 43, 44, 45, 48, 51, 53, 54, 55,
         58, 59, 60,
     ))]},
]

STATE_KEYS = ("effect", "colors", "speed", "bright", "dir",
              "custom_frames", "frame", "frame_play")


def apply_snapshot(cur, src):
    """Make cur describe src. Applied in place so the engine keeps its dict;
    values are copied so later edits never reach the preset they came from."""
    for k in STATE_KEYS:
        if k in src:
            cur[k] = json.loads(json.dumps(src[k]))
    if cur["effect"] not in EFFECT_IDS:
        cur["effect"] = "love"
    cf = cur.get("custom_frames")
    if not isinstance(cf, list) or not cf:
        cur["custom_frames"] = [["#000000"] * NKEY]
    try:
        cur["frame"] = max(0, min(int(cur.get("frame", 0)),
                                  len(cur["custom_frames"]) - 1))
    except Exception:
        cur["frame"] = 0


# ------------------------------------------------------------------ autostart
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def exe_path():
    return os.path.abspath(sys.executable if getattr(sys, "frozen", False) else sys.argv[0])


def autostart_on():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as k:
            v, _ = winreg.QueryValueEx(k, APP)
            return bool(v)
    except OSError:
        return False


def autostart_set(on):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_ALL_ACCESS) as k:
            if on:
                winreg.SetValueEx(k, APP, 0, winreg.REG_SZ, '"%s" --tray' % exe_path())
            else:
                try:
                    winreg.DeleteValue(k, APP)
                except OSError:
                    pass
        log("autostart -> %s" % on)
        return True
    except Exception as e:
        log("autostart fail: %s" % e)
        return False


# --------------------------------------------------------------- state edits
# Everything that changes a state dict. Callers hold Engine.lock. None of
# these save, log or touch the window, so each one is a few plain lines.
def state_set_effect(st, fid):
    """Switch effect and bring along that effect's own palette."""
    st["effect"] = fid
    pal = (st.get("palettes") or {}).get(fid) or DEFAULT_PALETTES.get(fid)
    if pal:
        st["colors"] = list(pal)


def state_reset_palette(st):
    """Drop the user's override for the current effect only."""
    fid = st["effect"]
    (st.get("palettes") or {}).pop(fid, None)
    st["colors"] = list(DEFAULT_PALETTES.get(fid, ["#ffffff"]))


def _remember_palette(st):
    """A colour change belongs to the effect in use, and to no other."""
    st.setdefault("palettes", {})[st["effect"]] = list(st["colors"])


def state_set_color(st, index, hexv):
    cs = st["colors"]
    if 0 <= index < len(cs):
        cs[index] = hexv
        _remember_palette(st)


def state_add_color(st):
    cs = st["colors"]
    if len(cs) < 5:
        cs.append(cs[-1])
        _remember_palette(st)


def state_del_color(st):
    cs = st["colors"]
    if len(cs) > 1:
        cs.pop()
        _remember_palette(st)


def state_select_frame(st, i):
    if 0 <= i < len(st["custom_frames"]):
        st["frame"] = i


def _frame_index(st):
    """The cursor, or 0 when it points outside the canvas."""
    fi = st.get("frame", 0)
    return fi if 0 <= fi < len(st["custom_frames"]) else 0


def state_paint(st, index, erase):
    """Paint one key of the current frame. Painting always lands on the
    canvas, so whatever effect was showing switches to it."""
    frames = st.get("custom_frames") or [["#000000"] * NKEY]
    st["custom_frames"] = frames
    if st["effect"] != "custom":
        st["effect"] = "custom"
    fi = st.get("frame", 0)
    if not (0 <= fi < len(frames)):
        fi = 0
        st["frame"] = 0
    row = list(frames[fi])             # a new row: the render thread may be reading the old one
    row[index] = "#000000" if erase else st["colors"][0]
    frames[fi] = row


def state_add_frame(st):
    """Insert a copy of the current frame after it, and move to it."""
    frames = st["custom_frames"]
    if len(frames) < MAX_FRAMES:
        fi = _frame_index(st)
        frames.insert(fi + 1, list(frames[fi]))
        st["frame"] = fi + 1


def state_del_frame(st):
    frames = st["custom_frames"]
    if len(frames) > 1:
        fi = _frame_index(st)
        frames.pop(fi)
        st["frame"] = max(0, min(fi, len(frames) - 1))


def state_clear_frame(st):
    frames = st["custom_frames"]
    fi = st.get("frame", 0)
    if 0 <= fi < len(frames):
        frames[fi] = ["#000000"] * NKEY


def state_clear_all(st):
    st["custom_frames"] = [["#000000"] * NKEY]
    st["frame"] = 0
    st["frame_play"] = False


def state_toggle_play(st):
    if len(st["custom_frames"]) < 2:
        st["frame_play"] = False
    else:
        st["frame_play"] = not st.get("frame_play")
    return bool(st.get("frame_play"))


def state_append_frames(st, made, merge):
    """Put rendered rows on the canvas: after it, or merged into it frame by
    frame with the brighter colour winning. Either way the canvas becomes the
    effect in use, rewound and stopped, ready to be edited."""
    cur = list(st.get("custom_frames") or [])
    if len(cur) == 1 and all(c == "#000000" for c in cur[0]):
        cur = []                       # the untouched starter frame is replaced
    if merge and cur:
        for k in range(min(len(made), len(cur))):
            row = list(cur[k])
            other = made[k]
            for i in range(min(NKEY, len(row))):
                a = hex2rgb(row[i])
                b = hex2rgb(other[i])
                row[i] = "#%02x%02x%02x" % (
                    max(a[0], b[0]), max(a[1], b[1]), max(a[2], b[2]))
            cur[k] = row
        if len(made) > len(cur):
            cur.extend(made[len(cur):])
    else:
        cur = cur + made
    st["custom_frames"] = cur[:MAX_FRAMES]
    st["effect"] = "custom"
    st["frame"] = 0
    st["frame_play"] = False


# -------------------------------------------------------------------- engine
class Engine:
    """The state, the keyboard handle and the thread that keeps the frame fresh.

    The thread is the whole point of the app: it runs whether the window is
    visible, minimised or in the tray, and the page only ever reads from it.
    """

    PERIOD = 0.025      # frame time when there is nothing to write
    PROBE = 0.15        # idle step while waiting for the keyboard to come back
    RETRY = 3.0         # seconds between attempts to open it
    ERR_EVERY = 60.0    # a failure that keeps happening is logged this often, not 30 times a second

    def __init__(self):
        self.st = CONFIG.load()
        self.lock = threading.Lock()
        self.latest = []         # last rendered frame, for the page's preview
        self.h = None            # HID handle; owned by the loop thread
        self.connected = False
        self.paused = False      # in memory only: a fresh start is never paused
        self.running = True
        self._t0 = time.time()
        self._retry_at = 0.0
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self.loop, daemon=True)
        self._thread.start()

    def snapshot(self):
        """A shallow copy: the render thread reads it without the lock while the
        API keeps mutating st. custom_frames is therefore shared, which is why
        a row is always replaced and never edited in place, and the list only
        ever grows or shrinks by one."""
        with self.lock:
            return {**self.st, "colors": list(self.st["colors"])}

    def loop(self):
        logged = {}                     # failure -> when it was last written to the log
        while self.running:
            now = time.time()
            if self.h is None:
                if now >= self._retry_at:
                    self.h = open_dev()
                    self._retry_at = now + self.RETRY
                    self.connected = bool(self.h)
                if self.h is None:
                    time.sleep(self.PROBE)
                    continue

            try:
                self._tick(now)
            except Exception as e:
                # One bad frame must cost one frame, never the lights: a cell
                # in a hand-edited config used to end this thread for good.
                what = repr(e)
                if now - logged.get(what, -1e9) >= self.ERR_EVERY:
                    log("frame failed: %s" % what)
                    if len(logged) > 50:
                        logged.clear()
                    logged[what] = now
                time.sleep(0.25)
        self._release()

    def _tick(self, now):
        st = self.snapshot()
        AUDIO.sync(st.get("effect") == "beat")
        self.latest = gen_frame(now - self._t0, st)

        if not self.paused and not write_frame(self.h, self.latest):
            self._release()
            self.connected = False
            self._retry_at = time.time() + self.RETRY
            log("lost device, retrying")

        spent = time.time() - now
        if spent < self.PERIOD:
            time.sleep(self.PERIOD - spent)

    def _release(self):
        if self.h:
            k32.CloseHandle(self.h)
            self.h = None

    def stop(self):
        """End the loop. It finishes the frame it is writing first, so the
        keyboard is never left showing half of one."""
        self.running = False
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)


# ------------------------------------------------------------------- js API
BAKE_FPS = 10.0


def bake_count(count):
    """How many frames a bake asks for, clamped to what the canvas can hold."""
    try:
        return max(1, min(MAX_FRAMES, int(count)))
    except Exception:
        return 10


def render_frames(st, n):
    """The first n frames of st's effect at BAKE_FPS, as canvas rows."""
    made = []
    for k in range(n):
        f = gen_frame(k / BAKE_FPS, st)
        made.append(["#%02x%02x%02x" % (f[i * 3], f[i * 3 + 1], f[i * 3 + 2])
                     for i in range(NKEY)])
    return made


def apply_effect(fid):
    """The one way to change effect. The page and the tray menu both come
    here, so both get the same thing: the effect, with its own palette."""
    if fid not in EFFECT_IDS:
        return False
    eng = CTX.eng
    with eng.lock:
        state_set_effect(eng.st, fid)
    CONFIG.save(eng.st, force=True)
    return True


def audio_status():
    """What the caption says about "beat": live and on which device, or why not."""
    return {"audio_live": AUDIO.live(), "audio_dev": AUDIO.dev, "audio_err": AUDIO.err}


class Api:
    """The page's whole view of the app.

    Stateless on purpose. pywebview introspects this object and recurses into
    every non-callable public attribute, so it holds methods and nothing else;
    the wiring lives in CTX. Each call runs on a thread of its own.
    """

    def _edit(self, change, force=True):
        """Apply change(st) under the engine lock, then persist."""
        eng = CTX.eng
        with eng.lock:
            change(eng.st)
        CONFIG.save(eng.st, force)

    # ---- reads ----
    def get_state(self):
        eng = CTX.eng
        st = eng.snapshot()
        return {
            "effects": [[e[0], e[1], e[2], e[3]] for e in EFFECTS],
            "groups": [[g[0], g[1], g[2]] for g in GROUPS],
            "effect": st["effect"],
            "colors": list(st["colors"]),
            "speed": st["speed"],
            "bright": st["bright"],
            "dir": st["dir"],
            "frame": st["frame"],
            "frames": len(st["custom_frames"]),
            "frame_play": bool(st.get("frame_play")),
            "paused": eng.paused,
            "connected": eng.connected,
            "autostart": autostart_on(),
            "maxed": CTX.maxed,
            "presets": PRESETS.names(),
            "builtin": [p["name"] for p in BUILTIN_PRESETS],
            "love_frames": LOVE.n,
            "max_frames": MAX_FRAMES,
            **audio_status(),
        }

    def frame(self):
        return CTX.eng.latest or []

    def status(self):
        """Polled by the page. Cheap on purpose: no canvas, no presets."""
        eng = CTX.eng
        return {
            "connected": eng.connected,
            "paused": eng.paused,
            "autostart": autostart_on(),
            "effect": eng.st["effect"],
            "maxed": CTX.maxed,
            **audio_status(),
        }

    # ---- effect and palette ----
    def set_effect(self, fid):
        apply_effect(fid)
        return True

    def apply_builtin(self, index):
        if not (0 <= index < len(BUILTIN_PRESETS)):
            return False
        self._edit(lambda st: apply_snapshot(st, BUILTIN_PRESETS[index]))
        return True

    def reset_effect(self):
        self._edit(state_reset_palette)
        return True

    def set_color(self, index, hexv):
        self._edit(lambda st: state_set_color(st, index, hexv), force=False)
        return True

    def add_color(self):
        self._edit(state_add_color)
        return True

    def del_color(self):
        self._edit(state_del_color)
        return True

    def set_param(self, key, value):
        eng = CTX.eng
        with eng.lock:
            if key == "bright":
                eng.st["bright"] = clamp(float(value), 0.0, 1.0)
            elif key == "speed":
                eng.st["speed"] = clamp(float(value), 0.25, 3.0)
            elif key == "dir":
                eng.st["dir"] = -1 if float(value) < 0 else 1
        if key == "paused":
            eng.paused = bool(value)
        CONFIG.save(eng.st, force=(key in ("paused", "dir")))
        return True

    def toggle_autostart(self):
        v = not autostart_on()
        autostart_set(v)
        return v

    # ---- canvas ----
    def paint_key(self, index, erase=False):
        if not (0 <= index < NKEY):
            return False
        self._edit(lambda st: state_paint(st, index, erase))
        return True

    def set_frame(self, i):
        self._edit(lambda st: state_select_frame(st, i))
        return True

    def add_frame(self):
        self._edit(state_add_frame)
        return True

    def del_frame(self):
        self._edit(state_del_frame)
        return True

    def clear_all_frames(self):
        self._edit(state_clear_all)
        log("canvas cleared")
        return 1

    def clear_frame(self):
        self._edit(state_clear_frame)
        return True

    def toggle_frame_play(self):
        playing = []

        def change(st):
            playing.append(state_toggle_play(st))

        self._edit(change)
        return bool(playing and playing[0])

    def reset(self):
        eng = CTX.eng
        with eng.lock:
            fresh = default_state()
            eng.st.clear()
            eng.st.update(fresh)
        eng.paused = False
        CONFIG.save(eng.st, force=True)
        log("reset to defaults")
        return True

    # ---- window ----
    def maximize(self):
        w = CTX.win
        if not w:
            return False
        try:
            if CTX.maxed:
                w.restore()
                CTX.maxed = False
                threading.Timer(0.25, lambda: round_corners(16, True)).start()
            else:
                w.maximize()
                CTX.maxed = True
                threading.Timer(0.25, lambda: round_corners(0, False)).start()
        except Exception as e:
            log("maximize failed: %s" % e)
            return False
        return CTX.maxed

    def minimize(self):
        w = CTX.win
        if w:
            try:
                w.minimize()
            except Exception as e:
                log("minimize failed: %s" % e)
        return True

    def close_to_tray(self):
        """Hide to tray, but never leave the user with no way back: if the
        tray icon did not come up, fall back to the taskbar."""
        w = CTX.win
        if not w:
            return False
        if CTX.tray_ok:
            try:
                w.hide()
            except Exception as e:
                log("hide failed: %s" % e)
            if CTX.tray:
                try:
                    CTX.tray.notify("KeyLED 还在后台跑，双击右下角托盘图标打开。", APP)
                except Exception:
                    pass
        else:
            try:
                w.minimize()
                log("tray unavailable, minimised instead")
            except Exception as e:
                log("minimise failed: %s" % e)
        return True

    def start_drag(self):
        drag_window()
        return True

    # ---- presets ----
    def save_preset(self, name):
        eng = CTX.eng
        with eng.lock:
            snap = json.loads(json.dumps(eng.st))
        with PRESETS.lock:
            lst = PRESETS.load()
            nm = (name or "").strip() or ("预设 %d" % (len(lst) + 1))
            snap["name"] = nm[:24]
            lst.append(snap)
            lst = lst[-PRESETS.MAX:]        # keep the newest
            PRESETS.save(lst)
        log("preset saved: %s" % nm)
        return [p.get("name", "") for p in lst]

    def apply_preset(self, index):
        lst = PRESETS.load()
        if not (0 <= index < len(lst)):
            return False
        self._edit(lambda st: apply_snapshot(st, lst[index]))
        return True

    def bake_preset(self, kind, index, count, merge=False):
        """Render a preset with *its own* settings into frames, appended to
        (or merged into) the canvas. The effect in use is left untouched."""
        n = bake_count(count)
        if kind == "builtin":
            src = BUILTIN_PRESETS[index] if 0 <= index < len(BUILTIN_PRESETS) else None
        else:
            lst = PRESETS.load()
            src = lst[index] if 0 <= index < len(lst) else None
        if not src:
            return 0

        eng = CTX.eng
        with eng.lock:
            base = json.loads(json.dumps(eng.st))
        apply_snapshot(base, src)
        made = render_frames(base, n)

        self._edit(lambda st: state_append_frames(st, made, merge))
        total = len(eng.st["custom_frames"])
        log("bake %s#%d x%d merge=%s -> %d frames" % (kind, index, n, merge, total))
        return total

    def bake_canvas(self, effect_id, count):
        """Render an effect, with the colours in use, into real frames appended
        to the canvas. This is how effect A and effect B get stitched together."""
        n = bake_count(count)
        eng = CTX.eng
        with eng.lock:
            base = json.loads(json.dumps(eng.st))
        if effect_id not in EFFECT_IDS:
            effect_id = base.get("effect", "solid")
        base["effect"] = effect_id
        made = render_frames(base, n)

        self._edit(lambda st: state_append_frames(st, made, False))
        total = len(eng.st["custom_frames"])
        log("baked %s x%d -> %d frames" % (effect_id, n, total))
        return total

    def del_preset(self, index):
        with PRESETS.lock:
            lst = PRESETS.load()
            if 0 <= index < len(lst):
                lst.pop(index)
                PRESETS.save(lst)
        return [p.get("name", "") for p in lst]

    def quit(self):
        shutdown()
        return True


# ----------------------------------------------------------------- lifecycle
_exit_lock = threading.Lock()


def shutdown():
    """Quit for real. Safe from any thread, and more than once."""
    with _exit_lock:
        if CTX.quitting:
            return
        CTX.quitting = True
    eng = CTX.eng
    if eng:
        CONFIG.save(eng.st, force=True)
        AUDIO.stop()
        eng.stop()
    if CTX.tray:
        try:
            CTX.tray.stop()
        except Exception:
            pass
    if CTX.win:
        try:
            CTX.win.destroy()
        except Exception:
            pass


def hide_to_tray_on_close(win):
    """The close button hides the window; the tray's 退出 is the real exit."""

    def on_closing():
        if CTX.quitting:
            return True           # pywebview: returning False cancels the close
        try:
            win.hide()
        except Exception:
            pass
        return False

    win.events.closing += on_closing


def let_windows_shut_down(win):
    """Never stand in the way of a Windows shutdown or logoff.

    pywebview asks the closing handler above for *every* close reason, and a
    tray app that answers "no" to the OS is what puts it on the "this app is
    preventing shutdown" screen. The handler cannot see the reason, so watch
    the form as well: its handler runs after pywebview's, and when Windows is
    the one closing us it takes the cancel back. The window then closes for
    real and the usual shutdown() runs; this only saves first, because the OS
    gives an app a few seconds and nothing more.
    """
    def on_form_closing(sender, args):
        # Runs on the window's own thread: an exception here would surface as a
        # .NET error dialog, so everything is caught and written to the log.
        try:
            if str(getattr(args, "CloseReason", "")) != "WindowsShutDown":
                return
            if CTX.eng:
                CONFIG.save(CTX.eng.st, force=True)
            args.Cancel = False
        except Exception as e:
            log("shutdown hook: %s" % e)

    def hook(tries=0):
        native = getattr(win, "native", None)
        if native is None:
            if tries < 5:                           # the form is not built yet
                threading.Timer(0.5, hook, args=(tries + 1,)).start()
            return
        try:
            native.FormClosing += on_form_closing
            log("shutdown hook armed")
        except Exception as e:
            log("shutdown hook: %s" % e)

    hook()


# --------------------------------------------------------------------- tray
def build_icon():
    """The tray icon, drawn rather than shipped: four lit keys on a dark disc."""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((0, 0, 63, 63), fill=(22, 26, 36, 255))
    for i, c in enumerate([(255, 77, 77), (255, 209, 102), (94, 224, 160), (61, 125, 255)]):
        d.rectangle((16 + i * 9, 18, 16 + i * 9 + 6, 46), fill=c + (255,))
    return img


def start_tray(eng, win):
    try:
        import pystray

        def _open(icon, item):
            try:
                win.show()
                win.restore()
            except Exception as e:
                log("show failed: %s" % e)

        def _mk(fid):
            def _h(icon, item):
                apply_effect(fid)
                try:
                    win.evaluate_js("refresh()")
                except Exception:
                    pass
            return _h

        def _auto(icon, item):
            autostart_set(not autostart_on())
            try:
                win.evaluate_js("refresh()")
            except Exception:
                pass

        def _quit(icon, item):
            shutdown()

        menu = pystray.Menu(
            pystray.MenuItem("打开面板", _open, default=True),
            pystray.Menu.SEPARATOR,
            *[pystray.MenuItem(name, _mk(fid)) for fid, name, _d, _g in EFFECTS],
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("开机自启", _auto, checked=lambda i: autostart_on()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", _quit),
        )

        def _setup(ic):
            try:
                ic.visible = True
            except Exception:
                pass
            CTX.tray_ok = True
            log("tray ready")

        icon = pystray.Icon(APP, build_icon(), APP, menu)
        CTX.tray = icon
        threading.Thread(target=icon.run, kwargs={"setup": _setup}, daemon=True).start()
        log("tray starting")
    except Exception as e:
        log("tray failed: %s" % e)


def smoke_probe(win):
    """Self-check used to verify a packaged build actually renders."""
    def run():
        time.sleep(5)
        checks = [
            ("fx",      "document.querySelectorAll('.fx').length"),
            ("chips",   "document.querySelectorAll('.chip').length"),
            ("bridge",  "typeof window.pywebview"),
            ("status",  "document.getElementById('stext').textContent"),
            ("framelen", "String(latest ? latest.length : 0)"),
            ("canvasw", "document.getElementById('cv').width"),
            ("activefx", "document.querySelectorAll('.fx.on').length"),
            ("litpixels", "(function(){var g=ctx.getImageData(0,0,cv.width,cv.height).data;"
                          "var n=0,mx=0;for(var i=0;i<g.length;i+=4){var s=g[i]+g[i+1]+g[i+2];"
                          "if(s>90)n++;if(s>mx)mx=s;}return n+'/'+mx;})()"),
        ]
        for name, expr in checks:
            try:
                log("SMOKE %s=%r" % (name, win.evaluate_js(expr)))
            except Exception as e:
                log("SMOKE %s=ERR %s" % (name, str(e)[:90]))
        time.sleep(0.5)
        shutdown()

    threading.Thread(target=run, daemon=True).start()


# ------------------------------------------------------------------ selftest
def selftest():
    """python keyled.py --selftest: open the keyboard and push a few frames."""
    print("log dir:", log_dir())
    print("keys:", len(KX), "slots:", len(SLOT))
    h = open_dev()
    print("device:", "opened" if h else "NOT FOUND")
    if not h:
        return 1
    st = default_state()
    ok = 0
    for t in (0.0, 0.4, 0.8):
        f = gen_frame(t, st)
        good = write_frame(h, f, 0.008)
        ok += 1 if good else 0
        print("frame t=%.1f  max=%d  write=%s" % (t, max(f), good))
    st["effect"] = "rainbow"
    for t in (0.0, 0.6):
        write_frame(h, gen_frame(t, st), 0.008)
        ok += 1

    LOVE.load()
    if LOVE.n:
        st["effect"] = "love"
        f = gen_frame(1.0, st)
        lit = sum(1 for v in f if v > 0)
        print("love clip: %d frames, lit values at t=1.0: %d" % (LOVE.n, lit))
        write_frame(h, f, 0.008)
        ok += 1
    else:
        print("love clip: NOT LOADED")

    k32.CloseHandle(h)
    print("done, %d writes ok" % ok)
    return 0


# --------------------------------------------------------------------- main
def main():
    if "--selftest" in sys.argv:
        return selftest()

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    smoke = "--smoke" in sys.argv
    # a self-check must not be blocked by the instance the user is running
    k32.CreateMutexW(None, True, PROBE_MUTEX if smoke else MUTEX)
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS and not smoke:
        ctypes.windll.user32.MessageBoxW(None, "KeyLED 已经在运行（右下角托盘）。", APP, 0x40)
        return 0

    start_hidden = any(a.lower() in ("--tray", "/tray", "-tray") for a in sys.argv)
    log("start %s (hidden=%s)" % (" ".join(sys.argv), start_hidden))

    eng = Engine()
    LOVE.load()              # before the first frame: the renderers read the clip
    CTX.eng = eng
    eng.start()

    import webview
    win = webview.create_window(
        APP,
        url=res("ui.html"),
        js_api=Api(),
        width=1040,
        height=760,
        frameless=True,
        resizable=False,
        background_color="#10131a",
    )
    CTX.win = win
    hide_to_tray_on_close(win)

    def after_start():
        start_tray(eng, win)
        let_windows_shut_down(win)
        threading.Timer(0.9, lambda: round_corners(16, True)).start()
        if smoke:
            smoke_probe(win)
        if start_hidden:
            try:
                win.hide()
            except Exception:
                pass

    try:
        webview.start(after_start)
    finally:
        shutdown()
    log("exit")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
