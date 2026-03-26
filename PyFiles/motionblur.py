import ctypes
import json
import os
import threading
import time

import glfw
import imgui
import numpy as np
import win32api
import win32con
import win32gui
from imgui.integrations.glfw import GlfwRenderer
from OpenGL.GL import *

GL_BGR = 0x80E0
GL_BGRA = 0x80E1

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
SW = user32.GetSystemMetrics(0)
SH = user32.GetSystemMetrics(1)

try:
    _dm = win32api.EnumDisplaySettings(None, -1)
    MONITOR_HZ = int(_dm.DisplayFrequency) or 60
except Exception:
    MONITOR_HZ = 60

try:
    import dxcam

    _HAS_DXCAM = True
except ImportError:
    _HAS_DXCAM = False

try:
    import mss as _mss_mod

    _HAS_MSS = True
except ImportError:
    _HAS_MSS = False

if _HAS_DXCAM:
    _BACKEND = "dxcam"
elif _HAS_MSS:
    _BACKEND = "mss"
else:
    _BACKEND = "bitblt"

print(f"[CBlur] {SW}x{SH} @ {MONITOR_HZ} Hz  |  capture backend: {_BACKEND}")

_CFG_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "CBlur"
)
_CFG_FILE = os.path.join(_CFG_DIR, "config.json")


class Cfg:
    enabled = True
    strength = 0.55
    gui_visible = True
    interp_steps = 4

    def load(self):
        try:
            with open(_CFG_FILE, "r") as f:
                d = json.load(f)
            self.strength = float(d.get("strength", self.strength))
            self.enabled = bool(d.get("enabled", self.enabled))
            self.interp_steps = int(d.get("interp_steps", self.interp_steps))
            print(f"[CBlur] config loaded from {_CFG_FILE}")
        except Exception:
            pass

    def save(self):
        try:
            os.makedirs(_CFG_DIR, exist_ok=True)
            with open(_CFG_FILE, "w") as f:
                json.dump(
                    {
                        "strength": self.strength,
                        "enabled": self.enabled,
                        "interp_steps": self.interp_steps,
                    },
                    f,
                    indent=2,
                )
        except Exception as e:
            print(f"[CBlur] config save failed: {e}")


cfg = Cfg()
cfg.load()

print(f"[CBlur] interp steps: {cfg.interp_steps}")


class Grabber(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self._lock = threading.Lock()
        ch = 4 if _BACKEND in ("dxcam", "mss") else 3
        self._buf_a = np.empty((SH, SW, ch), dtype=np.uint8)
        self._buf_b = np.empty((SH, SW, ch), dtype=np.uint8)
        self._buf_out = np.empty((SH, SW, ch), dtype=np.uint8)
        self._active = 0
        self._new = False
        self._stop = threading.Event()
        self.fps = 0.0
        self._last_t = time.perf_counter()

    def _run_dxcam(self):
        bufs = [self._buf_a, self._buf_b]
        write = 0
        frame_budget = 1.0 / MONITOR_HZ
        while not self._stop.is_set():
            cam = None
            try:
                cam = dxcam.create(output_color="BGRA")
                while not self._stop.is_set():
                    t0 = time.perf_counter()
                    frame = cam.grab()
                    if frame is None:
                        elapsed = time.perf_counter() - t0
                        sleep = frame_budget - elapsed
                        if sleep > 0.001:
                            time.sleep(sleep)
                        continue
                    bufs[write][:] = frame
                    now = time.perf_counter()
                    dt = now - self._last_t
                    self._last_t = now
                    if dt > 0:
                        self.fps = self.fps * 0.9 + (1.0 / dt) * 0.1
                    with self._lock:
                        self._active = write
                        self._new = True
                    write = 1 - write
                    elapsed = time.perf_counter() - t0
                    sleep = frame_budget - elapsed
                    if sleep > 0.001:
                        time.sleep(sleep)
            except Exception as e:
                print(f"[CBlur] dxcam error: {e} — restarting in 0.5s")
                time.sleep(0.5)
            finally:
                if cam is not None:
                    try:
                        del cam
                    except Exception:
                        pass
                try:
                    import dxcam.core as _dxcore

                    if hasattr(_dxcore, "_camera_instances"):
                        _dxcore._camera_instances.clear()
                except Exception:
                    pass

    def _run_mss(self):
        import mss

        bufs = [self._buf_a, self._buf_b]
        write = 0
        frame_budget = 1.0 / MONITOR_HZ
        with mss.mss() as sct:
            monitor = {"top": 0, "left": 0, "width": SW, "height": SH}
            while not self._stop.is_set():
                t0 = time.perf_counter()
                raw = sct.grab(monitor)
                frame = np.frombuffer(raw.bgra, dtype=np.uint8).reshape(SH, SW, 4)
                bufs[write][:] = frame
                now = time.perf_counter()
                dt = now - self._last_t
                self._last_t = now
                if dt > 0:
                    self.fps = self.fps * 0.9 + (1.0 / dt) * 0.1
                with self._lock:
                    self._active = write
                    self._new = True
                write = 1 - write
                elapsed = time.perf_counter() - t0
                sleep = frame_budget - elapsed
                if sleep > 0.001:
                    time.sleep(sleep)

    def _run_bitblt(self):
        screen_dc = user32.GetDC(None)
        mem_dc = gdi32.CreateCompatibleDC(screen_dc)
        bmp = gdi32.CreateCompatibleBitmap(screen_dc, SW, SH)
        gdi32.SelectObject(mem_dc, bmp)

        class BMIH(ctypes.Structure):
            _fields_ = [
                ("biSize", ctypes.c_uint32),
                ("biWidth", ctypes.c_int32),
                ("biHeight", ctypes.c_int32),
                ("biPlanes", ctypes.c_uint16),
                ("biBitCount", ctypes.c_uint16),
                ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32),
                ("biXPelsPerMeter", ctypes.c_int32),
                ("biYPelsPerMeter", ctypes.c_int32),
                ("biClrUsed", ctypes.c_uint32),
                ("biClrImportant", ctypes.c_uint32),
            ]

        bmi = BMIH()
        bmi.biSize = ctypes.sizeof(BMIH)
        bmi.biWidth = SW
        bmi.biHeight = -SH
        bmi.biPlanes = 1
        bmi.biBitCount = 24
        bmi.biCompression = 0
        SRCCOPY = 0x00CC0020
        frame_budget = 1.0 / MONITOR_HZ
        bufs = [self._buf_a, self._buf_b]
        write = 0
        try:
            while not self._stop.is_set():
                t0 = time.perf_counter()
                buf = bufs[write]
                cptr = buf.ctypes.data_as(ctypes.c_char_p)
                gdi32.BitBlt(mem_dc, 0, 0, SW, SH, screen_dc, 0, 0, SRCCOPY)
                gdi32.GetDIBits(mem_dc, bmp, 0, SH, cptr, ctypes.byref(bmi), 0)
                now = time.perf_counter()
                dt = now - self._last_t
                self._last_t = now
                if dt > 0:
                    self.fps = self.fps * 0.9 + (1.0 / dt) * 0.1
                with self._lock:
                    self._active = write
                    self._new = True
                write = 1 - write
                elapsed = time.perf_counter() - t0
                sleep = frame_budget - elapsed
                if sleep > 0.001:
                    time.sleep(sleep)
        finally:
            gdi32.DeleteObject(bmp)
            gdi32.DeleteDC(mem_dc)
            user32.ReleaseDC(None, screen_dc)

    def run(self):
        if _BACKEND == "dxcam":
            self._run_dxcam()
        elif _BACKEND == "mss":
            self._run_mss()
        else:
            self._run_bitblt()

    def consume(self):
        with self._lock:
            if not self._new:
                return None
            self._new = False
            src = self._buf_a if self._active == 0 else self._buf_b
            np.copyto(self._buf_out, src)
        return self._buf_out

    def stop(self):
        self._stop.set()


VERT = """
#version 330 core
layout(location=0) in vec2 pos;
layout(location=1) in vec2 uv;
out vec2 vUV;
void main(){ vUV = uv; gl_Position = vec4(pos, 0.0, 1.0); }
"""

FRAG_BLIT = """
#version 330 core
in vec2 vUV; out vec4 fragColor;
uniform sampler2D uTex;
void main(){ fragColor = vec4(texture(uTex, vUV).rgb, 1.0); }
"""

FRAG_INTERP = """
#version 330 core
in vec2 vUV; out vec4 fragColor;
uniform sampler2D uA;
uniform sampler2D uB;
uniform float     uT;
void main(){
    vec2 uv = vec2(vUV.x, 1.0 - vUV.y);
    vec3 a  = texture(uA, uv).rgb;
    vec3 b  = texture(uB, uv).rgb;
    float st = smoothstep(0.0, 1.0, uT);
    fragColor = vec4(mix(a, b, st), 1.0);
}
"""

FRAG_ACCUM = """
#version 330 core
in vec2 vUV; out vec4 fragColor;
uniform sampler2D uCur;
uniform sampler2D uAcc;
uniform float     uDecay;
void main(){
    vec3 cur = texture(uCur, vUV).rgb;
    vec3 acc = texture(uAcc, vUV).rgb;
    fragColor = vec4(mix(cur, acc, uDecay), 1.0);
}
"""


def compile_shader(src, kind):
    s = glCreateShader(kind)
    glShaderSource(s, src)
    glCompileShader(s)
    if not glGetShaderiv(s, GL_COMPILE_STATUS):
        raise RuntimeError(glGetShaderInfoLog(s).decode())
    return s


def link_program(vs, fs):
    v = compile_shader(vs, GL_VERTEX_SHADER)
    f = compile_shader(fs, GL_FRAGMENT_SHADER)
    p = glCreateProgram()
    glAttachShader(p, v)
    glAttachShader(p, f)
    glLinkProgram(p)
    if not glGetProgramiv(p, GL_LINK_STATUS):
        raise RuntimeError(glGetProgramInfoLog(p).decode())
    glDeleteShader(v)
    glDeleteShader(f)
    return p


def make_fbo(w, h):
    tex = glGenTextures(1)
    glBindTexture(GL_TEXTURE_2D, tex)
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGB16F, w, h, 0, GL_RGB, GL_FLOAT, None)
    for p, v in [
        (GL_TEXTURE_MIN_FILTER, GL_LINEAR),
        (GL_TEXTURE_MAG_FILTER, GL_LINEAR),
        (GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE),
        (GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE),
    ]:
        glTexParameteri(GL_TEXTURE_2D, p, v)
    fbo = glGenFramebuffers(1)
    glBindFramebuffer(GL_FRAMEBUFFER, fbo)
    glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, tex, 0)
    glBindFramebuffer(GL_FRAMEBUFFER, 0)
    return fbo, tex


def build_quad():
    verts = np.array(
        [-1, -1, 0, 0, 1, -1, 1, 0, 1, 1, 1, 1, -1, 1, 0, 1], dtype=np.float32
    )
    idx = np.array([0, 1, 2, 0, 2, 3], dtype=np.uint32)
    vao = glGenVertexArrays(1)
    vbo = glGenBuffers(1)
    ebo = glGenBuffers(1)
    glBindVertexArray(vao)
    glBindBuffer(GL_ARRAY_BUFFER, vbo)
    glBufferData(GL_ARRAY_BUFFER, verts.nbytes, verts, GL_STATIC_DRAW)
    glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, ebo)
    glBufferData(GL_ELEMENT_ARRAY_BUFFER, idx.nbytes, idx, GL_STATIC_DRAW)
    glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 16, ctypes.c_void_p(0))
    glEnableVertexAttribArray(0)
    glVertexAttribPointer(1, 2, GL_FLOAT, GL_FALSE, 16, ctypes.c_void_p(8))
    glEnableVertexAttribArray(1)
    return vao


def draw_quad(vao):
    glBindVertexArray(vao)
    glDrawElements(GL_TRIANGLES, 6, GL_UNSIGNED_INT, None)


BG = (0.08, 0.08, 0.08, 1)
BG2 = (0.12, 0.12, 0.12, 1)
BG3 = (0.17, 0.17, 0.17, 1)
ACCENT = (0.60, 0.20, 0.90, 1)
ACCD = (0.40, 0.10, 0.62, 1)
ACCHOV = (0.74, 0.36, 1.00, 1)
TXT = (0.92, 0.92, 0.92, 1)
TXTD = (0.42, 0.42, 0.42, 1)
BORD = (0.20, 0.20, 0.20, 1)
BTNON = (0.28, 0.13, 0.55, 1)
BTNONH = (0.40, 0.20, 0.72, 1)
BTNOFF = (0.18, 0.18, 0.18, 1)
BTNOFFH = (0.26, 0.26, 0.26, 1)
WHITE = (1.0, 1.0, 1.0, 1.0)


def psc(i, c):
    imgui.push_style_color(i, c[0], c[1], c[2], c[3])


def apply_theme():
    s = imgui.get_style()
    s.window_rounding = 10
    s.frame_rounding = 6
    s.grab_rounding = 6
    s.frame_padding = (10, 5)
    s.item_spacing = (8, 7)
    s.window_padding = (14, 14)
    c = s.colors
    c[imgui.COLOR_WINDOW_BACKGROUND] = BG
    c[imgui.COLOR_CHILD_BACKGROUND] = BG
    c[imgui.COLOR_BORDER] = BORD
    c[imgui.COLOR_FRAME_BACKGROUND] = BG2
    c[imgui.COLOR_FRAME_BACKGROUND_HOVERED] = BG3
    c[imgui.COLOR_FRAME_BACKGROUND_ACTIVE] = BG3
    c[imgui.COLOR_TITLE_BACKGROUND] = (0.05, 0.05, 0.05, 1)
    c[imgui.COLOR_TITLE_BACKGROUND_ACTIVE] = (0.09, 0.09, 0.09, 1)
    c[imgui.COLOR_SLIDER_GRAB] = ACCD
    c[imgui.COLOR_SLIDER_GRAB_ACTIVE] = ACCENT
    c[imgui.COLOR_BUTTON] = BG3
    c[imgui.COLOR_BUTTON_HOVERED] = ACCD
    c[imgui.COLOR_BUTTON_ACTIVE] = ACCENT
    c[imgui.COLOR_CHECK_MARK] = ACCENT
    c[imgui.COLOR_SEPARATOR] = BORD
    c[imgui.COLOR_TEXT] = TXT
    c[imgui.COLOR_TEXT_DISABLED] = TXTD


def draw_panel(render_fps, grab_fps):
    if not cfg.gui_visible:
        return
    imgui.set_next_window_size(310, 210, imgui.ONCE)
    imgui.set_next_window_position(16, 16, imgui.ONCE)
    imgui.set_next_window_bg_alpha(0.94)
    imgui.begin(
        "CBlur##w", False, imgui.WINDOW_NO_SAVED_SETTINGS | imgui.WINDOW_NO_COLLAPSE
    )

    psc(imgui.COLOR_TEXT, ACCENT)
    imgui.text(f"  {render_fps:.0f}")
    imgui.pop_style_color()
    imgui.same_line()
    psc(imgui.COLOR_TEXT, TXTD)
    imgui.text(f"rnd  |  {grab_fps:.0f} grab [{_BACKEND}]")
    imgui.pop_style_color()
    imgui.same_line()
    avail = imgui.get_content_region_available_width()
    imgui.set_cursor_pos_x(imgui.get_cursor_pos_x() + avail - 54)
    psc(imgui.COLOR_TEXT, TXTD)
    imgui.text("F7  hide")
    imgui.pop_style_color()

    imgui.separator()
    imgui.spacing()

    imgui.spacing()
    imgui.push_item_width(-1)
    psc(imgui.COLOR_TEXT, TXTD)
    imgui.text("  Strength")
    imgui.pop_style_color()
    imgui.same_line()
    psc(imgui.COLOR_TEXT, ACCENT)
    imgui.text(f"  {cfg.strength:.2f}")
    imgui.pop_style_color()
    _, cfg.strength = imgui.slider_float("##str", cfg.strength, 0.0, 0.99, "")
    imgui.spacing()

    imgui.same_line()
    for name, val in [
        ("Light", 0.25),
        ("Normal", 0.45),
        ("Strong", 0.55),
        ("Heavy", 0.75),
    ]:
        active = abs(cfg.strength - val) < 0.02
        psc(imgui.COLOR_BUTTON, BTNON if active else BG3)
        psc(imgui.COLOR_BUTTON_HOVERED, BTNONH if active else ACCD)
        psc(imgui.COLOR_TEXT, WHITE if active else TXT)
        if imgui.button(f"{name}##p{name}", 55, 0):
            cfg.strength = val
        imgui.pop_style_color(3)
        imgui.same_line()
    imgui.new_line()
    imgui.spacing()
    psc(imgui.COLOR_TEXT, TXTD)
    imgui.text("  Interp Steps")
    imgui.pop_style_color()
    imgui.same_line()
    psc(imgui.COLOR_TEXT, ACCENT)
    imgui.text(f"  {cfg.interp_steps}")
    imgui.pop_style_color()
    changed, new_steps = imgui.slider_int("##steps", cfg.interp_steps, 1, 16, "")
    if changed:
        cfg.interp_steps = new_steps

    imgui.spacing()
    imgui.pop_item_width()
    imgui.end()


def set_clickthrough(hwnd, enable):
    _get = ctypes.windll.user32.GetWindowLongPtrW
    _set = ctypes.windll.user32.SetWindowLongPtrW
    ex = _get(hwnd, -20)
    ex = (
        (ex | win32con.WS_EX_TRANSPARENT)
        if enable
        else (ex & ~win32con.WS_EX_TRANSPARENT)
    )
    _set(hwnd, -20, ex)


def _make_grabber():
    g = Grabber()
    g.start()
    return g


def _clear_fbos(*fbos):
    for f in fbos:
        glBindFramebuffer(GL_FRAMEBUFFER, f)
        glClearColor(0, 0, 0, 0)
        glClear(GL_COLOR_BUFFER_BIT)
    glBindFramebuffer(GL_FRAMEBUFFER, 0)


def main():
    if not glfw.init():
        raise RuntimeError("glfw init failed")
    glfw.window_hint(glfw.DECORATED, glfw.FALSE)
    glfw.window_hint(glfw.FLOATING, glfw.TRUE)
    glfw.window_hint(glfw.TRANSPARENT_FRAMEBUFFER, glfw.TRUE)
    glfw.window_hint(glfw.FOCUS_ON_SHOW, glfw.FALSE)
    glfw.window_hint(glfw.DOUBLEBUFFER, glfw.TRUE)

    win = glfw.create_window(SW, SH, "CBlur", None, None)
    if not win:
        glfw.terminate()
        raise RuntimeError("window creation failed")
    glfw.set_window_pos(win, 0, 0)
    glfw.make_context_current(win)
    glfw.swap_interval(1)

    hwnd = win32gui.FindWindow(None, "CBlur")
    if hwnd:
        _get = ctypes.windll.user32.GetWindowLongPtrW
        _set = ctypes.windll.user32.SetWindowLongPtrW
        ex = _get(hwnd, -20)
        ex = (
            ex | win32con.WS_EX_LAYERED | win32con.WS_EX_NOACTIVATE
        ) & ~win32con.WS_EX_TRANSPARENT
        _set(hwnd, -20, ex)
        win32gui.SetLayeredWindowAttributes(hwnd, 0, 255, win32con.LWA_ALPHA)

        WDA_EXCLUDEFROMCAPTURE = 0x11
        ok = user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
        if not ok:
            print("WDA_EXCLUDEFROMCAPTURE failed, falling back to WDA_NONE")
            user32.SetWindowDisplayAffinity(hwnd, 0x0)

        win32gui.SetWindowPos(
            hwnd, win32con.HWND_TOPMOST, 0, 0, SW, SH, win32con.SWP_NOACTIVATE
        )

    imgui.create_context()
    apply_theme()
    impl = GlfwRenderer(win)

    vao = build_quad()
    p_blit = link_program(VERT, FRAG_BLIT)
    p_interp = link_program(VERT, FRAG_INTERP)
    p_accum = link_program(VERT, FRAG_ACCUM)

    blit_u = glGetUniformLocation(p_blit, "uTex")
    interp_a = glGetUniformLocation(p_interp, "uA")
    interp_b = glGetUniformLocation(p_interp, "uB")
    interp_t = glGetUniformLocation(p_interp, "uT")
    acc_cur = glGetUniformLocation(p_accum, "uCur")
    acc_acc = glGetUniformLocation(p_accum, "uAcc")
    acc_dec = glGetUniformLocation(p_accum, "uDecay")

    fbo_a, tex_a = make_fbo(SW, SH)
    fbo_b, tex_b = make_fbo(SW, SH)
    prev_fbo, prev_tex = make_fbo(SW, SH)
    interp_fbo, interp_tex = make_fbo(SW, SH)

    _clear_fbos(fbo_a, fbo_b, prev_fbo, interp_fbo)

    _is_4ch = _BACKEND in ("dxcam", "mss")
    _gl_fmt = GL_BGRA if _is_4ch else GL_BGR
    _gl_ifmt = GL_RGBA8 if _is_4ch else GL_RGB8

    raw_tex = glGenTextures(1)
    glBindTexture(GL_TEXTURE_2D, raw_tex)
    glTexImage2D(GL_TEXTURE_2D, 0, _gl_ifmt, SW, SH, 0, GL_RGB, GL_UNSIGNED_BYTE, None)
    for p, v in [
        (GL_TEXTURE_MIN_FILTER, GL_LINEAR),
        (GL_TEXTURE_MAG_FILTER, GL_LINEAR),
        (GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE),
        (GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE),
    ]:
        glTexParameteri(GL_TEXTURE_2D, p, v)

    fbytes = SW * SH * (4 if _is_4ch else 3)
    pbo = glGenBuffers(1)
    glBindBuffer(GL_PIXEL_UNPACK_BUFFER, pbo)
    glBufferData(GL_PIXEL_UNPACK_BUFFER, fbytes, None, GL_STREAM_DRAW)
    glBindBuffer(GL_PIXEL_UNPACK_BUFFER, 0)

    ping = True
    has_frame = False
    has_prev = False
    interp_ready = False
    shown = False
    render_fps = 0.0
    last_t = time.perf_counter()
    prev_f7 = False
    prev_vis = cfg.gui_visible
    prev_en = cfg.enabled
    top_tick = 0
    prev_strength = cfg.strength
    prev_interp_steps = cfg.interp_steps
    _save_timer = 0

    time.sleep(0.35)
    grabber = _make_grabber()

    def accum_pass(src_tex):
        nonlocal ping
        dst_fbo = fbo_a if ping else fbo_b
        hist_tex = tex_b if ping else tex_a
        glBindFramebuffer(GL_FRAMEBUFFER, dst_fbo)
        glUseProgram(p_accum)
        glActiveTexture(GL_TEXTURE0)
        glBindTexture(GL_TEXTURE_2D, src_tex)
        glUniform1i(acc_cur, 0)
        glActiveTexture(GL_TEXTURE1)
        glBindTexture(GL_TEXTURE_2D, hist_tex)
        glUniform1i(acc_acc, 1)
        glUniform1f(acc_dec, decay)
        draw_quad(vao)
        ping = not ping

    def interp_pass(a_tex, b_tex, t):
        glBindFramebuffer(GL_FRAMEBUFFER, interp_fbo)
        glUseProgram(p_interp)
        glActiveTexture(GL_TEXTURE0)
        glBindTexture(GL_TEXTURE_2D, a_tex)
        glUniform1i(interp_a, 0)
        glActiveTexture(GL_TEXTURE1)
        glBindTexture(GL_TEXTURE_2D, b_tex)
        glUniform1i(interp_b, 1)
        glUniform1f(interp_t, t)
        draw_quad(vao)

    def blit_pass(src_tex, dst_fbo=0):
        glBindFramebuffer(GL_FRAMEBUFFER, dst_fbo)
        glUseProgram(p_blit)
        glActiveTexture(GL_TEXTURE0)
        glBindTexture(GL_TEXTURE_2D, src_tex)
        glUniform1i(blit_u, 0)
        draw_quad(vao)

    while not glfw.window_should_close(win):
        glfw.poll_events()
        impl.process_inputs()

        f7 = bool(win32api.GetAsyncKeyState(win32con.VK_F7) & 0x8000)
        if f7 and not prev_f7:
            cfg.gui_visible = not cfg.gui_visible
        prev_f7 = f7

        if cfg.gui_visible != prev_vis and hwnd:
            set_clickthrough(hwnd, not cfg.gui_visible)
            prev_vis = cfg.gui_visible

        if (
            cfg.strength != prev_strength
            or cfg.enabled != prev_en
            or cfg.interp_steps != prev_interp_steps
        ):
            _save_timer = 60
            prev_strength = cfg.strength
            prev_interp_steps = cfg.interp_steps

        if cfg.enabled != prev_en:
            if cfg.enabled:
                _clear_fbos(prev_fbo, interp_fbo)
                has_frame = False
                has_prev = False
                interp_ready = False
                shown = False
                ping = True
                if not grabber.is_alive():
                    time.sleep(0.35)
                    grabber = _make_grabber()
                if hwnd:
                    win32gui.ShowWindow(hwnd, 5)
            else:
                grabber.stop()
                if hwnd:
                    win32gui.ShowWindow(hwnd, 0)
            prev_en = cfg.enabled

        if _save_timer > 0:
            _save_timer -= 1
            if _save_timer == 0:
                cfg.save()

        top_tick = (top_tick + 1) % 120
        if hwnd and top_tick == 0 and cfg.enabled:
            win32gui.SetWindowPos(
                hwnd,
                win32con.HWND_TOPMOST,
                0,
                0,
                SW,
                SH,
                win32con.SWP_NOACTIVATE | win32con.SWP_NOMOVE | win32con.SWP_NOSIZE,
            )

        if not cfg.enabled:
            glClearColor(0, 0, 0, 0)
            glClear(GL_COLOR_BUFFER_BIT)
            if cfg.gui_visible:
                imgui.new_frame()
                draw_panel(render_fps, 0)
                imgui.render()
                impl.render(imgui.get_draw_data())
            glfw.swap_buffers(win)
            now = time.perf_counter()
            dt = now - last_t
            last_t = now
            if dt > 0:
                render_fps = (
                    (1 / dt)
                    if render_fps == 0
                    else render_fps + 0.05 * (1 / dt - render_fps)
                )
            continue

        frame = grabber.consume()
        if frame is not None:
            glBindTexture(GL_TEXTURE_2D, raw_tex)
            if not has_frame:
                glTexImage2D(
                    GL_TEXTURE_2D,
                    0,
                    _gl_ifmt,
                    SW,
                    SH,
                    0,
                    _gl_fmt,
                    GL_UNSIGNED_BYTE,
                    frame,
                )

                glBindBuffer(GL_PIXEL_UNPACK_BUFFER, pbo)
                glBufferSubData(GL_PIXEL_UNPACK_BUFFER, 0, fbytes, frame)
                glBindBuffer(GL_PIXEL_UNPACK_BUFFER, 0)
                has_frame = True
                if not shown and hwnd:
                    win32gui.ShowWindow(hwnd, 5)
                    shown = True
            else:
                glBindBuffer(GL_PIXEL_UNPACK_BUFFER, pbo)
                glBufferSubData(GL_PIXEL_UNPACK_BUFFER, 0, fbytes, frame)
                glTexSubImage2D(
                    GL_TEXTURE_2D,
                    0,
                    0,
                    0,
                    SW,
                    SH,
                    _gl_fmt,
                    GL_UNSIGNED_BYTE,
                    None,
                )
                glBindBuffer(GL_PIXEL_UNPACK_BUFFER, 0)

        if not has_frame:
            glClearColor(0, 0, 0, 0)
            glClear(GL_COLOR_BUFFER_BIT)
            glfw.swap_buffers(win)
            continue

        glViewport(0, 0, SW, SH)

        cap_fps = max(grabber.fps, 1.0)
        cap_fps = min(cap_fps, 1000.0)
        base = 0.35 + cfg.strength * 0.28
        decay_single = float(np.clip(base ** (120.0 / cap_fps), 0.0, 1))
        decay = float(decay_single ** (1.0 / cfg.interp_steps))

        if frame is not None:
            if has_prev:
                for i in range(1, cfg.interp_steps + 1):
                    interp_pass(prev_tex, raw_tex, i / cfg.interp_steps)
                    accum_pass(interp_tex)
            else:
                interp_pass(raw_tex, raw_tex, 1.0)
                blit_pass(interp_tex, fbo_a)
                blit_pass(interp_tex, fbo_b)
                accum_pass(interp_tex)

            blit_pass(raw_tex, prev_fbo)
            has_prev = True
            interp_ready = True

        else:
            if interp_ready:
                accum_pass(interp_tex)

        result_tex = tex_b if ping else tex_a
        blit_pass(result_tex, dst_fbo=0)

        if cfg.gui_visible:
            imgui.new_frame()
            draw_panel(render_fps, cap_fps)
            imgui.render()
            impl.render(imgui.get_draw_data())

        glfw.swap_buffers(win)

        now = time.perf_counter()
        dt = now - last_t
        last_t = now
        if dt > 0:
            render_fps = (
                (1 / dt)
                if render_fps == 0
                else render_fps + 0.05 * (1 / dt - render_fps)
            )

    cfg.save()
    grabber.stop()
    impl.shutdown()
    glfw.terminate()


if __name__ == "__main__":
    main()
