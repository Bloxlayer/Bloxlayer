import ctypes
import ctypes.wintypes
import json
import os
import subprocess
import sys
import threading
import time
import winreg

import glfw
import imgui
import psutil
from imgui.integrations.glfw import GlfwRenderer

_winmm = ctypes.WinDLL("winmm", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_ntdll = ctypes.WinDLL("ntdll", use_last_error=True)

NORMAL_PC = 0x00000020
ABOVE_NORMAL_PC = 0x00008000
HIGH_PC = 0x00000080
REALTIME_PC = 0x00000100
PROCESS_ALL_ACCESS = 0x1F0FFF

THREAD_PRIORITY_ABOVE_NORMAL = 1
THREAD_PRIORITY_HIGHEST = 2
THREAD_PRIORITY_TIME_CRITICAL = 15
THREAD_SET_INFORMATION = 0x0020
THREAD_QUERY_INFORMATION = 0x0040

PRIORITY_MAP = {
    "Normal": NORMAL_PC,
    "Above Normal": ABOVE_NORMAL_PC,
    "High": HIGH_PC,
    "Realtime": REALTIME_PC,
}
PRIORITY_KEYS = list(PRIORITY_MAP.keys())

POWER_PLANS = {
    "Balanced": "381b4222-f694-41f0-9685-ff5bb260df2e",
    "High Performance": "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c",
    "Ultimate": "e9a42b02-d5df-448d-aa00-03f14749eb61",
}
POWER_KEYS = list(POWER_PLANS.keys())

ROBLOX_EXE = "RobloxPlayerBeta.exe"
CRASH_EXE = "RobloxCrashHandler.exe"
APP_DIR = os.path.join(os.environ.get("LOCALAPPDATA", "C:/"), "OpOptimizer")
SETTINGS_F = os.path.join(APP_DIR, "settings.json")

DEFAULTS = {
    "enabled": True,
    "loop_delay": 0.5,
    "show_debug": True,
    "notify_status": True,
    "log_actions": True,
    "priority": "High",
    "force_pri_interval": 5,
    "io_priority_high": True,
    "boost_threads": True,
    "thread_priority": "Highest",
    "kill_crash_handler": True,
    "suspend_telemetry": False,
    "cpu_affinity_enabled": False,
    "cpu_affinity": [],
    "avoid_ecores": True,
    "force_aff_interval": 5,
    "cpu_limit": 90,
    "auto_throttle": False,
    "trim_working_set": True,
    "trim_interval": 30,
    "mem_limit_mb": 4096,
    "clean_logs": True,
    "clean_interval": 300,
    "aggressive_trim": False,
    "timer_1ms": True,
    "timer_half_ms": False,
    "apply_power_plan": True,
    "power_plan": "High Performance",
    "disable_cpu_parking": False,
    "mmcss_tweak": True,
    "gpu_priority_tweak": True,
    "fullscreen_opt_off": True,
    "game_mode_on": True,
    "disable_nagle": False,
    "qos_priority": False,
    "frametime_scale": 80,
    "minimized_throttle": True,
}

settings = DEFAULTS.copy()
state = {
    "cpu": 0.0,
    "mem": 0.0,
    "status": "Idle",
    "frametimes": [],
    "threads": 0,
    "priority_ok": False,
    "affinity_ok": False,
    "timer_ok": False,
    "power_ok": False,
    "mmcss_ok": False,
    "gpu_tweak_ok": False,
    "io_ok": False,
    "thread_ok": False,
    "crash_killed": False,
    "last_pri": 0,
    "last_aff": 0,
    "last_trim": 0,
    "last_clean": 0,
    "log": [],
    "roblox_found": False,
    "is_admin": False,
    "cpu_history": [],
    "setting_watch": None,
    "setting_watch_name": "",
    "setting_watch_baseline": 0.0,
    "setting_watch_start": 0.0,
    "impact": {},
}


def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def log(msg):
    if settings.get("log_actions"):
        ts = time.strftime("%H:%M:%S")
        state["log"].append((ts, msg))
        if len(state["log"]) > 200:
            state["log"].pop(0)


def ensure_dir():
    os.makedirs(APP_DIR, exist_ok=True)


def load_settings():
    ensure_dir()
    if os.path.exists(SETTINGS_F):
        try:
            with open(SETTINGS_F, "r") as f:
                loaded = json.load(f)
                for k, v in loaded.items():
                    if k in DEFAULTS:
                        settings[k] = v
        except Exception:
            pass


def save_settings():
    ensure_dir()
    try:
        with open(SETTINGS_F, "w") as f:
            json.dump({k: settings[k] for k in DEFAULTS}, f, indent=4)
    except Exception:
        pass


_timer_set = False


def set_timer_resolution():
    global _timer_set
    try:
        if settings["timer_half_ms"]:
            current = ctypes.c_ulong(0)
            _ntdll.NtSetTimerResolution(5000, True, ctypes.byref(current))
            state["timer_ok"] = True
            if not _timer_set:
                log("Timer resolution set to 0.5 ms")
        elif settings["timer_1ms"]:
            _winmm.timeBeginPeriod(1)
            state["timer_ok"] = True
            if not _timer_set:
                log("Timer resolution set to 1 ms")
        else:
            _winmm.timeEndPeriod(1)
            state["timer_ok"] = False
        _timer_set = True
    except Exception as e:
        state["timer_ok"] = False
        log(f"Timer resolution failed: {e}")


def restore_timer():
    try:
        _winmm.timeEndPeriod(1)
    except Exception:
        pass


def apply_power_plan():
    guid = POWER_PLANS.get(settings["power_plan"])
    if not guid:
        return
    try:
        result = subprocess.run(
            ["powercfg", "/setactive", guid], capture_output=True, timeout=5
        )
        if result.returncode == 0:
            state["power_ok"] = True
            log(f"Power plan set to {settings['power_plan']}")
        else:
            if settings["power_plan"] == "Ultimate":
                subprocess.run(
                    [
                        "powercfg",
                        "-duplicatescheme",
                        "e9a42b02-d5df-448d-aa00-03f14749eb61",
                    ],
                    capture_output=True,
                    timeout=5,
                )
                subprocess.run(
                    ["powercfg", "/setactive", guid], capture_output=True, timeout=5
                )
                state["power_ok"] = True
                log("Ultimate Performance plan created and applied")
            else:
                state["power_ok"] = False
    except Exception as e:
        state["power_ok"] = False
        log(f"Power plan failed: {e}")


def apply_mmcss_tweak():
    key_path = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile\Tasks\Games"
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, key_path, 0, winreg.KEY_SET_VALUE
        ) as k:
            winreg.SetValueEx(k, "GPU Priority", 0, winreg.REG_DWORD, 8)
            winreg.SetValueEx(k, "Priority", 0, winreg.REG_DWORD, 6)
            winreg.SetValueEx(k, "Scheduling Category", 0, winreg.REG_SZ, "High")
            winreg.SetValueEx(k, "SFIO Priority", 0, winreg.REG_SZ, "High")
        state["mmcss_ok"] = True
        log("MMCSS Games profile optimized")
    except Exception as e:
        state["mmcss_ok"] = False
        log(f"MMCSS tweak failed (need admin): {e}")


def apply_gpu_priority_tweak():
    profile_path = (
        r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile"
    )
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, profile_path, 0, winreg.KEY_SET_VALUE
        ) as k:
            winreg.SetValueEx(
                k, "NetworkThrottlingIndex", 0, winreg.REG_DWORD, 0xFFFFFFFF
            )
            winreg.SetValueEx(k, "SystemResponsiveness", 0, winreg.REG_DWORD, 0)
        state["gpu_tweak_ok"] = True
        log("GPU priority and network throttle tweaks applied")
    except Exception as e:
        state["gpu_tweak_ok"] = False
        log(f"GPU priority tweak failed (need admin): {e}")


def apply_game_mode():
    try:
        key_path = r"SOFTWARE\Microsoft\GameBar"
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE
        ) as k:
            winreg.SetValueEx(k, "AllowAutoGameMode", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(k, "AutoGameModeEnabled", 0, winreg.REG_DWORD, 1)
        log("Windows Game Mode enabled")
    except Exception as e:
        log(f"Game Mode tweak failed: {e}")


def apply_fullscreen_opt_off(exe_path):
    if not exe_path:
        return
    try:
        layers_path = (
            r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers"
        )
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, layers_path) as k:
            winreg.SetValueEx(
                k, exe_path, 0, winreg.REG_SZ, "~ DISABLEDXMAXIMIZEDWINDOWEDMODE"
            )
        log("Fullscreen optimizations disabled for Roblox")
    except Exception as e:
        log(f"Fullscreen opt tweak failed: {e}")


def disable_nagle_for_roblox():
    key_path = r"SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as root:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(root, i)
                    with winreg.OpenKey(root, sub, 0, winreg.KEY_SET_VALUE) as k:
                        winreg.SetValueEx(k, "TcpAckFrequency", 0, winreg.REG_DWORD, 1)
                        winreg.SetValueEx(k, "TCPNoDelay", 0, winreg.REG_DWORD, 1)
                    i += 1
                except OSError:
                    break
        log("Nagle's algorithm disabled")
    except Exception as e:
        log(f"Nagle tweak failed (need admin): {e}")


def get_roblox():
    for p in psutil.process_iter(["name", "exe"]):
        try:
            if p.info["name"] == ROBLOX_EXE:
                return p
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return None


def apply_priority(proc):
    try:
        pclass = PRIORITY_MAP.get(settings["priority"], HIGH_PC)
        handle = _kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, proc.pid)
        if handle:
            _kernel32.SetPriorityClass(handle, pclass)
            _kernel32.CloseHandle(handle)
        state["priority_ok"] = True
    except Exception:
        state["priority_ok"] = False


def apply_io_priority(proc):
    PROCESS_IO_PRIORITY = 33
    try:
        handle = _kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, proc.pid)
        if handle:
            priority = ctypes.c_ulong(3 if settings["io_priority_high"] else 2)
            _ntdll.NtSetInformationProcess(
                handle,
                PROCESS_IO_PRIORITY,
                ctypes.byref(priority),
                ctypes.sizeof(priority),
            )
            _kernel32.CloseHandle(handle)
        state["io_ok"] = True
    except Exception:
        state["io_ok"] = False


def boost_threads(proc):
    try:
        tp = (
            THREAD_PRIORITY_TIME_CRITICAL
            if settings["thread_priority"] == "Time Critical"
            else THREAD_PRIORITY_HIGHEST
        )
        for t in proc.threads():
            handle = _kernel32.OpenThread(
                THREAD_SET_INFORMATION | THREAD_QUERY_INFORMATION, False, t.id
            )
            if handle:
                _kernel32.SetThreadPriority(handle, tp)
                _kernel32.CloseHandle(handle)
        state["thread_ok"] = True
    except Exception:
        state["thread_ok"] = False


def trim_working_set(proc):
    try:
        handle = _kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, proc.pid)
        if handle:
            if settings["aggressive_trim"]:
                _kernel32.SetProcessWorkingSetSizeEx(
                    handle,
                    ctypes.c_size_t(0xFFFFFFFFFFFFFFFF),
                    ctypes.c_size_t(0xFFFFFFFFFFFFFFFF),
                    0,
                )
            else:
                _kernel32.K32EmptyWorkingSet(handle)
            _kernel32.CloseHandle(handle)
    except Exception:
        pass


def apply_affinity(proc):
    try:
        if settings["cpu_affinity_enabled"]:
            if settings["cpu_affinity"]:
                proc.cpu_affinity(settings["cpu_affinity"])
            elif settings["avoid_ecores"]:
                all_cores = list(range(psutil.cpu_count(logical=True)))
                phys = psutil.cpu_count(logical=False) or len(all_cores)
                p_cores = all_cores[: min(phys * 2, len(all_cores))]
                proc.cpu_affinity(p_cores)
        state["affinity_ok"] = True
    except Exception:
        state["affinity_ok"] = False


def kill_crash_handler():
    for p in psutil.process_iter(["name"]):
        try:
            if p.info["name"] == CRASH_EXE:
                p.kill()
                state["crash_killed"] = True
                log("RobloxCrashHandler killed")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass


def clean_logs():
    path = os.path.expandvars(r"%LOCALAPPDATA%\Roblox\logs")
    if not os.path.exists(path):
        return
    count = 0
    for f in os.listdir(path):
        try:
            os.remove(os.path.join(path, f))
            count += 1
        except Exception:
            pass
    if count:
        log(f"Cleaned {count} Roblox log file(s)")


_startup_done = False


def run_startup_tweaks():
    global _startup_done
    if _startup_done:
        return
    _startup_done = True
    set_timer_resolution()
    if settings["apply_power_plan"]:
        apply_power_plan()
    if settings["mmcss_tweak"]:
        apply_mmcss_tweak()
    if settings["gpu_priority_tweak"]:
        apply_gpu_priority_tweak()
    if settings["game_mode_on"]:
        apply_game_mode()
    if settings["disable_nagle"]:
        disable_nagle_for_roblox()
    roblox = get_roblox()
    if roblox:
        exe = roblox.info.get("exe") if roblox.info else None
        if exe and settings["fullscreen_opt_off"]:
            apply_fullscreen_opt_off(exe)


CPU_HISTORY_MAX = 120
WATCH_DURATION = 12.0
WATCH_SETTLE = 3.0
IMPACT_THRESHOLD = 8.0


def record_cpu_sample(cpu_val):
    state["cpu_history"].append((time.perf_counter(), cpu_val))
    cutoff = time.perf_counter() - 60.0
    state["cpu_history"] = [(t, v) for t, v in state["cpu_history"] if t > cutoff]


def baseline_cpu():
    now = time.perf_counter()
    window = [(t, v) for t, v in state["cpu_history"] if now - t <= 20.0]
    if len(window) < 3:
        return None
    return sum(v for _, v in window) / len(window)


def start_setting_watch(name):
    base = baseline_cpu()
    if base is None:
        return
    state["setting_watch_name"] = name
    state["setting_watch_baseline"] = base
    state["setting_watch_start"] = time.perf_counter()
    state["setting_watch"] = "pending"
    log(f"Monitoring impact of '{name}' (baseline CPU {base:.1f}%)")


def tick_setting_watch(cpu_val):
    if state["setting_watch"] not in ("pending", "measuring"):
        return
    elapsed = time.perf_counter() - state["setting_watch_start"]
    if elapsed < WATCH_SETTLE:
        state["setting_watch"] = "pending"
        return
    state["setting_watch"] = "measuring"
    if elapsed >= WATCH_SETTLE + WATCH_DURATION:
        now = time.perf_counter()
        window_start = state["setting_watch_start"] + WATCH_SETTLE
        samples = [v for t, v in state["cpu_history"] if t >= window_start]
        if len(samples) >= 3:
            avg_after = sum(samples) / len(samples)
            delta = avg_after - state["setting_watch_baseline"]
            name = state["setting_watch_name"]
            if delta > IMPACT_THRESHOLD:
                state["impact"][name] = "worse"
                log(
                    f"'{name}' appears to be hurting performance (+{delta:.1f}% CPU avg)"
                )
            elif delta < -IMPACT_THRESHOLD:
                state["impact"][name] = "better"
                log(
                    f"'{name}' appears to be helping performance ({delta:.1f}% CPU avg)"
                )
            else:
                state["impact"][name] = "neutral"
        state["setting_watch"] = None
        state["setting_watch_name"] = ""


def optimizer_loop():
    run_startup_tweaks()
    last_time = time.perf_counter()
    _crash_kill_done = False

    while True:
        now = time.perf_counter()
        state["threads"] = threading.active_count()

        if not settings["enabled"]:
            state["status"] = "Disabled"
            time.sleep(settings["loop_delay"])
            continue

        proc = get_roblox()

        if not proc:
            state["roblox_found"] = False
            state["status"] = "Waiting for Roblox..."
            _crash_kill_done = False
            time.sleep(settings["loop_delay"])
            continue

        state["roblox_found"] = True

        if settings["kill_crash_handler"] and not _crash_kill_done:
            kill_crash_handler()
            _crash_kill_done = True

        if now - state["last_pri"] > settings["force_pri_interval"]:
            apply_priority(proc)
            if settings["io_priority_high"]:
                apply_io_priority(proc)
            state["last_pri"] = now

        if (
            settings["cpu_affinity_enabled"]
            and now - state["last_aff"] > settings["force_aff_interval"]
        ):
            apply_affinity(proc)
            state["last_aff"] = now

        if settings["boost_threads"]:
            try:
                boost_threads(proc)
            except Exception:
                pass

        try:
            cpu_val = proc.cpu_percent(interval=0.05)
            mem_rss = proc.memory_info().rss / 1024 / 1024
            state["cpu"] = cpu_val
            state["mem"] = mem_rss
            record_cpu_sample(cpu_val)
            tick_setting_watch(cpu_val)
        except Exception:
            state["cpu"] = 0.0
            state["mem"] = 0.0

        if (
            settings["trim_working_set"]
            and now - state["last_trim"] > settings["trim_interval"]
        ):
            trim_working_set(proc)
            state["last_trim"] = now

        if (
            settings["clean_logs"]
            and now - state["last_clean"] > settings["clean_interval"]
        ):
            clean_logs()
            state["last_clean"] = now

        if settings["auto_throttle"] and state["cpu"] > settings["cpu_limit"]:
            time.sleep(0.02)

        state["status"] = ""

        ft = (now - last_time) * 1000
        last_time = now
        state["frametimes"].append(ft)
        if len(state["frametimes"]) > 300:
            state["frametimes"].pop(0)

        time.sleep(settings["loop_delay"])


def apply_theme():
    style = imgui.get_style()
    style.window_rounding = 0.0
    style.frame_rounding = 2.0
    style.grab_rounding = 2.0
    style.tab_rounding = 2.0
    style.scrollbar_rounding = 2.0
    style.popup_rounding = 2.0
    style.child_rounding = 0.0

    style.window_padding = (16.0, 14.0)
    style.frame_padding = (9.0, 5.0)
    style.item_spacing = (10.0, 7.0)
    style.item_inner_spacing = (6.0, 5.0)
    style.scrollbar_size = 9.0
    style.grab_min_size = 8.0
    style.window_border_size = 1.0
    style.frame_border_size = 1.0
    style.tab_border_size = 0.0
    c = style.colors

    c[imgui.COLOR_WINDOW_BACKGROUND] = (0.031, 0.035, 0.059, 1.00)
    c[imgui.COLOR_CHILD_BACKGROUND] = (0.047, 0.051, 0.082, 1.00)
    c[imgui.COLOR_POPUP_BACKGROUND] = (0.055, 0.059, 0.094, 1.00)

    c[imgui.COLOR_BORDER] = (0.118, 0.122, 0.188, 1.00)
    c[imgui.COLOR_BORDER_SHADOW] = (0.00, 0.00, 0.00, 0.00)

    c[imgui.COLOR_TEXT] = (0.894, 0.894, 0.941, 1.00)
    c[imgui.COLOR_TEXT_DISABLED] = (0.353, 0.353, 0.478, 1.00)

    c[imgui.COLOR_FRAME_BACKGROUND] = (0.071, 0.075, 0.118, 1.00)
    c[imgui.COLOR_FRAME_BACKGROUND_HOVERED] = (0.102, 0.106, 0.165, 1.00)
    c[imgui.COLOR_FRAME_BACKGROUND_ACTIVE] = (0.133, 0.137, 0.212, 1.00)

    c[imgui.COLOR_SLIDER_GRAB] = (0.424, 0.247, 1.000, 1.00)
    c[imgui.COLOR_SLIDER_GRAB_ACTIVE] = (0.612, 0.435, 1.000, 1.00)
    c[imgui.COLOR_CHECK_MARK] = (0.612, 0.435, 1.000, 1.00)

    c[imgui.COLOR_BUTTON] = (0.212, 0.141, 0.502, 1.00)
    c[imgui.COLOR_BUTTON_HOVERED] = (0.322, 0.212, 0.659, 1.00)
    c[imgui.COLOR_BUTTON_ACTIVE] = (0.424, 0.271, 0.788, 1.00)

    c[imgui.COLOR_TAB] = (0.055, 0.059, 0.094, 1.00)
    c[imgui.COLOR_TAB_HOVERED] = (0.212, 0.141, 0.502, 1.00)
    c[imgui.COLOR_TAB_ACTIVE] = (0.259, 0.173, 0.596, 1.00)
    c[imgui.COLOR_TAB_UNFOCUSED] = (0.047, 0.051, 0.082, 1.00)
    c[imgui.COLOR_TAB_UNFOCUSED_ACTIVE] = (0.118, 0.082, 0.275, 1.00)

    c[imgui.COLOR_TITLE_BACKGROUND] = (0.031, 0.035, 0.059, 1.00)
    c[imgui.COLOR_TITLE_BACKGROUND_ACTIVE] = (0.180, 0.118, 0.424, 1.00)
    c[imgui.COLOR_TITLE_BACKGROUND_COLLAPSED] = (0.031, 0.035, 0.059, 0.80)

    c[imgui.COLOR_HEADER] = (0.165, 0.110, 0.388, 0.85)
    c[imgui.COLOR_HEADER_HOVERED] = (0.259, 0.173, 0.569, 0.90)
    c[imgui.COLOR_HEADER_ACTIVE] = (0.322, 0.212, 0.659, 1.00)

    c[imgui.COLOR_SCROLLBAR_BACKGROUND] = (0.031, 0.035, 0.059, 1.00)
    c[imgui.COLOR_SCROLLBAR_GRAB] = (0.165, 0.141, 0.314, 1.00)
    c[imgui.COLOR_SCROLLBAR_GRAB_HOVERED] = (0.251, 0.212, 0.455, 1.00)
    c[imgui.COLOR_SCROLLBAR_GRAB_ACTIVE] = (0.322, 0.271, 0.565, 1.00)

    c[imgui.COLOR_SEPARATOR] = (0.118, 0.122, 0.188, 1.00)
    c[imgui.COLOR_SEPARATOR_HOVERED] = (0.322, 0.212, 0.659, 0.80)
    c[imgui.COLOR_SEPARATOR_ACTIVE] = (0.424, 0.271, 0.788, 1.00)

    c[imgui.COLOR_RESIZE_GRIP] = (0.212, 0.141, 0.502, 0.35)
    c[imgui.COLOR_RESIZE_GRIP_HOVERED] = (0.322, 0.212, 0.659, 0.65)
    c[imgui.COLOR_RESIZE_GRIP_ACTIVE] = (0.424, 0.271, 0.788, 1.00)


def section_header(title):
    """Uppercase label + full-width divider — cleaner than plain separator."""
    imgui.spacing()
    imgui.text_colored(title.upper(), 0.424, 0.247, 1.000, 1.00)
    imgui.separator()
    imgui.spacing()


def status_pill(ok):
    """Compact coloured tag: ACTIVE / INACTIVE."""
    if ok:
        imgui.text_colored("[ON]", 0.20, 0.85, 0.50, 1.0)
    else:
        imgui.text_colored("[OFF]", 0.55, 0.55, 0.65, 1.0)
    imgui.same_line()


def impact_badge(setting_name):
    impact = state["impact"].get(setting_name)
    if impact == "worse":
        imgui.same_line(spacing=8)
        imgui.text_colored("[HURTS FPS]", 1.0, 0.28, 0.28, 1.0)
    elif impact == "better":
        imgui.same_line(spacing=8)
        imgui.text_colored("[HELPS FPS]", 0.20, 0.85, 0.50, 1.0)
    elif (
        state["setting_watch"] in ("pending", "measuring")
        and state["setting_watch_name"] == setting_name
    ):
        imgui.same_line(spacing=8)
        imgui.text_colored("[measuring]", 0.90, 0.80, 0.20, 1.0)


def toggled_setting(label, key, widget_id=None):
    wid = widget_id or f"##{key}"
    prev = settings[key]
    changed, val = imgui.checkbox(f"{label}{wid}", prev)
    if changed:
        settings[key] = val
        start_setting_watch(key)
    impact_badge(key)
    return changed, val


def tab_dashboard():
    if state["roblox_found"]:
        imgui.text_colored("  ROBLOX DETECTED", 0.20, 0.85, 0.50, 1.0)
    else:
        imgui.text_colored("  WAITING FOR ROBLOX", 0.95, 0.72, 0.10, 1.0)

    imgui.same_line(spacing=20)
    imgui.text_colored(
        f"  {state['status']}",
        0.424,
        0.247,
        1.000,
        1.0,
    )
    imgui.separator()
    imgui.spacing()

    imgui.columns(4, "stats_row", border=False)

    def stat_block(label, value, color=(0.894, 0.894, 0.941, 1.0)):
        imgui.text_colored(value, *color)
        imgui.text_colored(label, 0.353, 0.353, 0.478, 1.0)

    stat_block(
        "CPU",
        f"{state['cpu']:.1f}%",
        (0.424, 0.247, 1.000, 1.0) if state["cpu"] < 70 else (1.0, 0.38, 0.38, 1.0),
    )
    imgui.next_column()
    stat_block("MEMORY", f"{state['mem']:.0f} MB")
    imgui.next_column()
    stat_block("THREADS", str(state["threads"]))
    imgui.next_column()
    stat_block(
        "ADMIN",
        "YES" if state["is_admin"] else "NO",
        (0.20, 0.85, 0.50, 1.0) if state["is_admin"] else (1.0, 0.38, 0.38, 1.0),
    )
    imgui.columns(1)

    imgui.spacing()
    imgui.separator()
    imgui.spacing()

    if state["setting_watch"] in ("pending", "measuring"):
        imgui.text_colored(
            f"  Measuring: {state['setting_watch_name']}...",
            0.90,
            0.80,
            0.20,
            1.0,
        )
        imgui.spacing()

    if state["impact"]:
        imgui.text_colored("IMPACT SUMMARY", 0.424, 0.247, 1.000, 1.0)
        imgui.separator()
        imgui.spacing()
        for name, val in state["impact"].items():
            if val == "worse":
                imgui.text_colored(f"  - {name}", 1.0, 0.30, 0.30, 1.0)
                imgui.same_line()
                imgui.text_disabled("hurting FPS — consider disabling")
            elif val == "better":
                imgui.text_colored(f"  - {name}", 0.20, 0.85, 0.50, 1.0)
                imgui.same_line()
                imgui.text_disabled("helping FPS")
        imgui.spacing()

    imgui.text_colored("ACTIVE OPTIMIZATIONS", 0.424, 0.247, 1.000, 1.0)
    imgui.separator()
    imgui.spacing()

    items_l = [
        ("Timer Res", state["timer_ok"]),
        ("Power Plan", state["power_ok"]),
        ("MMCSS", state["mmcss_ok"]),
        ("GPU Priority", state["gpu_tweak_ok"]),
    ]
    items_r = [
        ("CPU Priority", state["priority_ok"]),
        ("I/O Priority", state["io_ok"]),
        ("Thread Boost", state["thread_ok"]),
        ("Affinity", state["affinity_ok"]),
    ]

    imgui.columns(2, "opt_cols", border=False)
    for label, ok in items_l:
        status_pill(ok)
        imgui.text(label)
    imgui.next_column()
    for label, ok in items_r:
        status_pill(ok)
        imgui.text(label)
    imgui.columns(1)

    imgui.spacing()
    imgui.separator()
    imgui.spacing()

    if len(state["frametimes"]) > 1:
        arr = (ctypes.c_float * len(state["frametimes"]))(*state["frametimes"])
        avg = sum(state["frametimes"]) / len(state["frametimes"])
        mn = min(state["frametimes"])
        mx = max(state["frametimes"])
        imgui.text_disabled(
            f"Loop frametime   avg {avg:.1f} ms   min {mn:.1f} ms   max {mx:.1f} ms"
        )
        imgui.plot_lines(
            "##ft",
            arr,
            overlay_text="ms",
            graph_size=(0, settings["frametime_scale"]),
        )
        imgui.spacing()

    _, settings["enabled"] = imgui.checkbox("  Enabled##ena", settings["enabled"])
    imgui.same_line(spacing=16)
    if imgui.button("Apply All##applyall"):
        global _startup_done
        _startup_done = False
        run_startup_tweaks()
    imgui.same_line(spacing=10)
    if imgui.button("Clear Impact##clrimp"):
        state["impact"].clear()
        state["cpu_history"].clear()
        log("Performance impact data cleared")


def tab_process():
    section_header("Process Priority")

    cur_idx = (
        PRIORITY_KEYS.index(settings["priority"])
        if settings["priority"] in PRIORITY_KEYS
        else 2
    )
    changed, val = imgui.combo("Priority Class##pri", cur_idx, PRIORITY_KEYS)
    if changed:
        settings["priority"] = PRIORITY_KEYS[val]
        start_setting_watch("priority")
    impact_badge("priority")

    if settings["priority"] == "Realtime":
        imgui.spacing()
        imgui.text_colored(
            "  WARNING  Realtime priority can cause system stutters.",
            1.0,
            0.55,
            0.10,
            1.0,
        )

    imgui.spacing()
    _, settings["force_pri_interval"] = imgui.slider_int(
        "Re-apply Interval (s)##pripri", settings["force_pri_interval"], 1, 60
    )

    section_header("Thread Boost")

    toggled_setting("Boost Roblox Threads##bt", "boost_threads", "")
    imgui.same_line(spacing=16)
    imgui.text_disabled("elevates render / physics threads")

    thread_opts = ["Highest", "Time Critical"]
    t_idx = 1 if settings.get("thread_priority") == "Time Critical" else 0
    changed, t_val = imgui.combo("Thread Priority##tp", t_idx, thread_opts)
    if changed:
        settings["thread_priority"] = thread_opts[t_val]
        start_setting_watch("thread_priority")
    impact_badge("thread_priority")

    section_header("I/O Priority")

    toggled_setting("High I/O Priority##io", "io_priority_high", "")
    imgui.same_line(spacing=16)
    imgui.text_disabled("faster texture / asset streaming")

    section_header("Sub-Processes")

    toggled_setting("Kill RobloxCrashHandler##kch", "kill_crash_handler", "")
    imgui.same_line(spacing=16)
    imgui.text_disabled("one less background process")

    imgui.spacing()
    toggled_setting("Suspend Roblox Telemetry##telem", "suspend_telemetry", "")
    imgui.same_line(spacing=16)
    imgui.text_colored("experimental", 0.90, 0.72, 0.20, 1.0)

    imgui.spacing()
    imgui.separator()
    imgui.spacing()
    killed = "Yes" if state["crash_killed"] else "No"
    imgui.text_disabled(f"Crash handler killed this session: {killed}")


def tab_cpu():
    section_header("CPU Affinity")

    toggled_setting("Enable Affinity Pinning##aff", "cpu_affinity_enabled", "")

    if settings["cpu_affinity_enabled"]:
        imgui.spacing()
        toggled_setting(
            "Auto-avoid Efficiency Cores (Intel Hybrid)##ec", "avoid_ecores", ""
        )
        imgui.text_disabled("  Pins Roblox to P-cores for best single-thread perf.")

        imgui.spacing()
        imgui.text("Manual Core List  (overrides auto):")
        cores_str = ",".join(str(c) for c in settings["cpu_affinity"])
        changed, new_cores = imgui.input_text(
            "Cores (comma-sep)##cores", cores_str, 128
        )
        if changed:
            try:
                settings["cpu_affinity"] = [
                    int(x.strip()) for x in new_cores.split(",") if x.strip().isdigit()
                ]
            except Exception:
                pass

        imgui.spacing()
        _, settings["force_aff_interval"] = imgui.slider_int(
            "Re-apply Interval (s)##affpri", settings["force_aff_interval"], 1, 60
        )

    section_header("Throttle Guard")

    toggled_setting("Enable Auto-Throttle##at", "auto_throttle", "")
    imgui.same_line(spacing=16)
    imgui.text_colored("NOT recommended — slows Roblox", 1.0, 0.55, 0.10, 1.0)

    if settings["auto_throttle"]:
        imgui.spacing()
        _, settings["cpu_limit"] = imgui.slider_int(
            "Throttle Threshold (%)##cpl", settings["cpu_limit"], 50, 100
        )

    section_header("Hardware Info")

    cpu_l = psutil.cpu_count(logical=True) or 0
    cpu_p = psutil.cpu_count(logical=False) or 0
    imgui.text_disabled(f"{cpu_p} physical cores   {cpu_l} logical cores")

    try:
        freqs = psutil.cpu_freq(percpu=True)
        if freqs:
            imgui.spacing()
            imgui.text_disabled("Per-Core Max Frequencies:")
            for i, f in enumerate(freqs):
                imgui.text_disabled(f"  Core {i:>2}   {f.max:.0f} MHz")
    except Exception:
        pass


def tab_memory():
    section_header("Working Set")

    toggled_setting("Periodic Trim##trim", "trim_working_set", "")
    imgui.same_line(spacing=16)
    imgui.text_disabled("reduce RAM pressure on the OS")

    imgui.spacing()
    toggled_setting("Aggressive Trim (SetWorkingSetSizeEx)##at2", "aggressive_trim", "")
    imgui.same_line(spacing=16)
    imgui.text_colored("may cause brief stutter", 1.0, 0.55, 0.10, 1.0)

    imgui.spacing()
    _, settings["trim_interval"] = imgui.slider_int(
        "Trim Every (s)##tri", settings["trim_interval"], 5, 300
    )
    imgui.spacing()
    if imgui.button("Trim Now##tnow"):
        proc = get_roblox()
        if proc:
            trim_working_set(proc)
            log("Manual working set trim")

    section_header("Memory Limit Guard")

    _, settings["mem_limit_mb"] = imgui.slider_int(
        "Soft Limit (MB)##memlim", settings["mem_limit_mb"], 512, 16384
    )
    imgui.text_disabled("Adds a small delay if Roblox exceeds this threshold.")

    section_header("Log Cleaning")

    toggled_setting("Auto-Clean Roblox Logs##cl", "clean_logs", "")
    imgui.spacing()
    _, settings["clean_interval"] = imgui.slider_int(
        "Clean Every (s)##cli", settings["clean_interval"], 60, 600
    )
    imgui.spacing()
    if imgui.button("Clean Now##clnow"):
        clean_logs()

    section_header("System RAM")

    vm = psutil.virtual_memory()
    imgui.text_disabled(
        f"{vm.used / 1024**3:.1f} GB  /  {vm.total / 1024**3:.1f} GB total"
        f"   ({vm.percent:.0f}% used)"
    )


def tab_system():
    section_header("Timer Resolution")

    imgui.text_colored(
        "Reducing timer resolution is the #1 FPS-stability improvement.",
        0.612,
        0.612,
        0.820,
        1.0,
    )
    imgui.text_disabled(
        "Default Windows timer = 15.6 ms — causes significant frame jitter."
    )
    imgui.spacing()

    changed, settings["timer_1ms"] = imgui.checkbox(
        "1 ms  (timeBeginPeriod)##t1ms", settings["timer_1ms"]
    )
    if changed:
        set_timer_resolution()
        start_setting_watch("timer_1ms")
    impact_badge("timer_1ms")

    imgui.spacing()
    changed, settings["timer_half_ms"] = imgui.checkbox(
        "0.5 ms  (NtSetTimerResolution — requires admin)##thalf",
        settings["timer_half_ms"],
    )
    if changed:
        set_timer_resolution()
        start_setting_watch("timer_half_ms")
    impact_badge("timer_half_ms")

    imgui.spacing()
    status_pill(state["timer_ok"])
    imgui.text("Timer status")

    section_header("Power Plan")

    _, settings["apply_power_plan"] = imgui.checkbox(
        "Apply on Startup##app", settings["apply_power_plan"]
    )
    imgui.spacing()

    plan_idx = (
        POWER_KEYS.index(settings["power_plan"])
        if settings["power_plan"] in POWER_KEYS
        else 1
    )
    changed, plan_val = imgui.combo("Power Plan##pp", plan_idx, POWER_KEYS)
    if changed:
        settings["power_plan"] = POWER_KEYS[plan_val]

    imgui.spacing()
    if imgui.button("Apply Now##appnow"):
        apply_power_plan()
    imgui.same_line(spacing=12)
    status_pill(state["power_ok"])
    imgui.text("Plan status")

    section_header("General")

    _, settings["minimized_throttle"] = imgui.checkbox(
        "Throttle when Roblox is Minimized##minth", settings["minimized_throttle"]
    )
    imgui.spacing()
    _, settings["loop_delay"] = imgui.slider_float(
        "Loop Delay (s)##ld", settings["loop_delay"], 0.1, 5.0
    )


def tab_tweaks():
    section_header("MMCSS / Registry  —  Admin Required")

    imgui.text_colored(
        "These tweaks write to the registry and persist until manually undone.",
        0.612,
        0.612,
        0.820,
        1.0,
    )
    imgui.spacing()

    changed, settings["mmcss_tweak"] = imgui.checkbox(
        "MMCSS Games Profile  (GPU Priority=8, Scheduling=High)##mmcss",
        settings["mmcss_tweak"],
    )
    if changed:
        start_setting_watch("mmcss_tweak")
    impact_badge("mmcss_tweak")

    imgui.spacing()
    status_pill(state["mmcss_ok"])
    imgui.same_line()
    imgui.text("MMCSS status")
    imgui.same_line(spacing=16)
    if imgui.button("Apply##amm"):
        apply_mmcss_tweak()

    imgui.spacing()
    imgui.separator()
    imgui.spacing()

    changed, settings["gpu_priority_tweak"] = imgui.checkbox(
        "Disable Network Throttling  +  SystemResponsiveness = 0##gpt",
        settings["gpu_priority_tweak"],
    )
    if changed:
        start_setting_watch("gpu_priority_tweak")
    impact_badge("gpu_priority_tweak")

    imgui.spacing()
    status_pill(state["gpu_tweak_ok"])
    imgui.same_line()
    imgui.text("GPU / Net status")
    imgui.same_line(spacing=16)
    if imgui.button("Apply##agpun"):
        apply_gpu_priority_tweak()

    imgui.spacing()
    imgui.separator()
    imgui.spacing()

    changed, settings["game_mode_on"] = imgui.checkbox(
        "Enable Windows Game Mode##gm", settings["game_mode_on"]
    )
    if changed:
        start_setting_watch("game_mode_on")
    impact_badge("game_mode_on")
    imgui.same_line(spacing=16)
    if imgui.button("Apply##agm"):
        apply_game_mode()

    imgui.spacing()
    imgui.separator()
    imgui.spacing()

    changed, settings["fullscreen_opt_off"] = imgui.checkbox(
        "Disable Fullscreen Optimizations for Roblox##fso",
        settings["fullscreen_opt_off"],
    )
    if changed:
        start_setting_watch("fullscreen_opt_off")
    impact_badge("fullscreen_opt_off")
    imgui.text_disabled("  Applied on next Roblox launch detection.")

    section_header("Network")

    changed, settings["disable_nagle"] = imgui.checkbox(
        "Disable Nagle's Algorithm  (TCP_NODELAY)##nagle", settings["disable_nagle"]
    )
    if changed:
        start_setting_watch("disable_nagle")
    impact_badge("disable_nagle")

    imgui.text_disabled("  Reduces latency / ping. Requires admin. Applied at startup.")
    imgui.spacing()
    if imgui.button("Apply Now##naglenow"):
        disable_nagle_for_roblox()


def tab_log():
    _, settings["log_actions"] = imgui.checkbox(
        "Enable Log##logon", settings["log_actions"]
    )
    imgui.same_line(spacing=16)
    if imgui.button("Clear##clrlog"):
        state["log"].clear()

    imgui.spacing()
    imgui.separator()
    imgui.spacing()

    imgui.begin_child("LogArea##la", 0, 0, border=False)
    for ts, msg in reversed(state["log"]):
        imgui.text_colored(f"[{ts}]", 0.424, 0.247, 1.000, 1.0)
        imgui.same_line(spacing=8)
        imgui.text(msg)
    imgui.end_child()


def draw_ui(w, h):
    apply_theme()

    imgui.set_next_window_position(0, 0)
    imgui.set_next_window_size(w, h)
    flags = (
        imgui.WINDOW_NO_TITLE_BAR
        | imgui.WINDOW_NO_RESIZE
        | imgui.WINDOW_NO_MOVE
        | imgui.WINDOW_NO_COLLAPSE
    )
    imgui.begin("##main", True, flags)
    imgui.text_colored("OPOptimizer", 0.612, 0.435, 1.000, 1.0)
    imgui.same_line(spacing=12)

    avail = imgui.get_content_region_available_width()
    imgui.same_line(spacing=avail - imgui.calc_text_size("ACTIVE  ")[0] - 16)
    if state["roblox_found"]:
        imgui.text_colored("ACTIVE", 0.20, 0.85, 0.50, 1.0)
    else:
        imgui.text_colored("STANDBY", 0.55, 0.55, 0.65, 1.0)

    imgui.separator()
    imgui.spacing()

    if imgui.begin_tab_bar("##tabs"):
        tab_defs = [
            ("Dashboard##dash", tab_dashboard),
            ("Process##proc", tab_process),
            ("CPU##cpu", tab_cpu),
            ("Memory##mem", tab_memory),
            ("System##sys", tab_system),
            ("Tweaks##twk", tab_tweaks),
            ("Log##log", tab_log),
        ]
        for label, fn in tab_defs:
            sel, _ = imgui.begin_tab_item(f"  {label}  ")
            if sel:
                fn()
                imgui.end_tab_item()

        imgui.end_tab_bar()

    imgui.end()
    save_settings()


def init_window():
    if not glfw.init():
        return None
    glfw.window_hint(glfw.RESIZABLE, True)
    glfw.window_hint(glfw.DECORATED, True)
    glfw.window_hint(glfw.SAMPLES, 4)
    window = glfw.create_window(880, 640, "OPOptimizer", None, None)
    if not window:
        glfw.terminate()
        return None
    glfw.make_context_current(window)
    glfw.swap_interval(1)
    imgui.create_context()
    return window


def main():
    load_settings()
    state["is_admin"] = is_admin()
    if not state["is_admin"]:
        log("NOT running as Admin registry tweaks unavailable")
    else:
        log("Running as Administrator all tweaks available")

    threading.Thread(target=optimizer_loop, daemon=True).start()

    window = init_window()
    if not window:
        print("Failed to create GLFW window.")
        return

    impl = GlfwRenderer(window)

    try:
        while not glfw.window_should_close(window):
            glfw.poll_events()
            impl.process_inputs()
            w, h = glfw.get_framebuffer_size(window)

            imgui.new_frame()
            draw_ui(w, h)
            imgui.render()
            impl.render(imgui.get_draw_data())
            glfw.swap_buffers(window)
    finally:
        restore_timer()
        impl.shutdown()
        glfw.terminate()


if __name__ == "__main__":
    main()
