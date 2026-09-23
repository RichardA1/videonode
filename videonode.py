#!/usr/bin/env python3
"""VideoNode - offline video/image player for Raspberry Pi 3/4, with a TV menu.

See README.md for setup and usage.

Modes:
  full   ("Episodes")   - one long video (filename order), then a break of
                          random short clips and images, forever
  sample ("Quick Cuts") - random N-second cuts of random videos (and
                          optionally images), forever

Menu is drawn with pygame straight to the display (no desktop needed) and
controlled with a USB keyboard/mouse. During playback the keyboard/mouse are
read directly via evdev: Esc / Q / right-click = menu, N / Right / left-click = skip.

Looks for media on USB first, falls back to the SD card, and rescans every
cycle so new files and USB drives are picked up automatically.
"""

import argparse
import configparser
import json
import logging
import logging.handlers
import os
import random
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import namedtuple
from pathlib import Path

SECTION = "videonode"
DEFAULTS = {
    "mode": "full",
    "ui": "yes",
    "autostart_seconds": "15",
    "idle_resume_seconds": "300",
    "usb_root": "/media/usb/videos",
    "sd_root": str(Path.home() / "videos"),
    "long_dir": "long",
    "short_dir": "short",
    "images_dir": "images",
    "num_short_clips": "3",
    "num_images": "2",
    "image_seconds": "8",
    "sample_seconds": "30",
    "sample_batch": "10",
    "sample_margin": "10",
    "sample_include_short": "no",
    "sample_include_images": "yes",
    "extensions": ".mp4 .mkv .m4v .mov",
    "image_extensions": ".jpg .jpeg .png .webp .bmp .gif",
    "min_file_age": "30",
    "player_cmd": "mpv --hwdec=v4l2m2m-copy --vo=gpu --gpu-context=drm "
                  "--fullscreen --no-osc --osd-level=0 --input-terminal=no "
                  "--msg-level=all=error",
    "audio_device": "",
    "resume": "yes",
    "state_file": str(Path.home() / ".videonode_state.json"),
    "rescan_interval": "10",
    "max_play_seconds": "0",
    "log_level": "INFO",
    "log_file": "",
}
MODE_NAMES = {"full": "Episodes", "sample": "Quick Cuts"}

IPC_SOCKET = "/tmp/videonode-mpv.sock"
# Fit images inside 1080p before they reach the GPU (the Pi 3 can't draw
# textures over 2048 px, and phone photos are 4000+).
IMAGE_VF = "lavfi=[scale=1920:1080:force_original_aspect_ratio=decrease]"

log = logging.getLogger("videonode")
exit_event = threading.Event()


def as_bool(s):
    return str(s).strip().lower() in ("1", "yes", "true", "on")


def cfg_int(cfg, key, lo=None):
    """Tolerant int parsing: a typo in the config shouldn't kill playback."""
    try:
        v = int(float(cfg[key]))
    except (ValueError, KeyError):
        log.warning("Bad value for %s (%r), using default %s",
                    key, cfg.get(key), DEFAULTS[key])
        v = int(DEFAULTS[key])
    return v if lo is None else max(lo, v)


# --------------------------------------------------------------------------- config

def parse_args():
    ap = argparse.ArgumentParser(description="Offline video/image player with TV menu")
    ap.add_argument("--config", default=str(Path(__file__).with_name("videonode.conf")))
    ap.add_argument("--mode", choices=("full", "sample"),
                    help="start this mode (skips the menu countdown in headless runs)")
    ap.add_argument("--no-ui", action="store_true", help="play without the menu")
    ap.add_argument("--log-level")
    return ap.parse_args()


def read_config(args):
    cp = configparser.ConfigParser(interpolation=None)
    cp.read_dict({SECTION: DEFAULTS})
    found = []
    try:
        found = cp.read(args.config)
    except configparser.Error as e:
        log.error("Config file %s is invalid (%s), using defaults", args.config, e)
        cp = configparser.ConfigParser(interpolation=None)
        cp.read_dict({SECTION: DEFAULTS})
    cfg = dict(cp[SECTION])
    if args.mode:
        cfg["mode"] = args.mode
    if args.log_level:
        cfg["log_level"] = args.log_level
    if cfg["mode"] not in MODE_NAMES:
        log.warning("Unknown mode %r, using 'full'", cfg["mode"])
        cfg["mode"] = "full"
    cfg["_found"] = bool(found)
    return cfg


def update_conf_file(path, changes):
    """Change `key = value` lines in place, keeping comments and layout."""
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        lines = [f"[{SECTION}]"]
    done = set()
    out = []
    for line in lines:
        stripped = line.strip()
        key = stripped.split("=", 1)[0].strip() if "=" in stripped else None
        if key in changes and not stripped.startswith(("#", ";")):
            out.append(f"{key} = {changes[key]}")
            done.add(key)
        else:
            out.append(line)
    out += [f"{k} = {v}" for k, v in changes.items() if k not in done]
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w") as f:
            f.write("\n".join(out) + "\n")
        os.replace(tmp, path)
        log.info("Saved settings: %s", ", ".join(f"{k}={v}" for k, v in changes.items()))
        return True
    except OSError as e:
        log.error("Could not save settings to %s: %s", path, e)
        return False


def setup_logging(cfg):
    log.setLevel(getattr(logging, cfg["log_level"].upper(), logging.INFO))
    sh = logging.StreamHandler(sys.stderr)  # -> journald under systemd
    sh.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    log.addHandler(sh)
    if cfg["log_file"]:
        try:
            fh = logging.handlers.RotatingFileHandler(
                cfg["log_file"], maxBytes=1_000_000, backupCount=3)
            fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            log.addHandler(fh)
        except OSError as e:
            log.warning("Cannot open log file %s: %s", cfg["log_file"], e)


# --------------------------------------------------------------------------- media

Library = namedtuple("Library", "root long short images")
Item = namedtuple("Item", "path start length image")


def ext_set(s):
    return {e.lower() if e.startswith(".") else "." + e.lower() for e in s.split()}


def find_media(directory, exts, min_age=0):
    """Files in `directory` with matching extensions, skipping any modified in
    the last `min_age` seconds (i.e. still being copied in over the network)."""
    if not directory.is_dir():
        return []
    now = time.time()
    found = []
    try:
        for p in directory.iterdir():
            if not (p.is_file() and p.suffix.lower() in exts
                    and not p.name.startswith(".")):
                continue
            try:
                if now - p.stat().st_mtime < min_age:
                    continue
            except OSError:
                continue
            found.append(p)
    except OSError as e:  # e.g. USB yanked mid-scan
        log.warning("Error scanning %s: %s", directory, e)
        return []
    return sorted(found)


def scan_library(cfg):
    """First root (USB, then SD) that has any media."""
    vexts = ext_set(cfg["extensions"])
    iexts = ext_set(cfg["image_extensions"])
    age = cfg_int(cfg, "min_file_age", 0)
    for root in (Path(cfg["usb_root"]), Path(cfg["sd_root"])):
        lib = Library(root,
                      find_media(root / cfg["long_dir"], vexts, age),
                      find_media(root / cfg["short_dir"], vexts, age),
                      find_media(root / cfg["images_dir"], iexts, age))
        if lib.long or lib.short or lib.images:
            return lib
    return Library(None, [], [], [])


_duration_cache = {}
_ffprobe_missing = False


def get_duration(path):
    """Video duration in seconds via ffprobe, cached per file. None if unknown."""
    global _ffprobe_missing
    try:
        st = path.stat()
    except OSError:
        return None
    key = (str(path), st.st_mtime, st.st_size)
    if key in _duration_cache:
        return _duration_cache[key]
    if _ffprobe_missing:
        return None
    dur = None
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=20)
        dur = float(r.stdout.strip())
    except FileNotFoundError:
        _ffprobe_missing = True
        log.warning("ffprobe not found (sudo apt install ffmpeg); "
                    "quick cuts will start at the beginning of each video")
    except (subprocess.TimeoutExpired, ValueError):
        log.warning("Could not read duration of %s", path.name)
    _duration_cache[key] = dur
    return dur


def sample_start(duration, length, margin):
    """Random start time, skipping `margin` seconds at each end when there's room."""
    if not duration or duration <= length:
        return None
    if duration - 2 * margin > length:
        lo, hi = margin, duration - length - margin
    else:
        lo, hi = 0, duration - length
    return random.uniform(lo, hi)


class ShuffleBag:
    """Uses every item once in random order before any repeats."""

    def __init__(self):
        self.bag = []
        self.last = None

    def draw(self, pool):
        current = set(pool)
        self.bag = [p for p in self.bag if p in current]  # drop removed files
        if not self.bag:
            self.bag = list(pool)
            random.shuffle(self.bag)
            if len(self.bag) > 1 and self.bag[-1] == self.last:
                self.bag[0], self.bag[-1] = self.bag[-1], self.bag[0]
        self.last = self.bag.pop()
        return self.last


def load_state(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(path, state):
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, path)
    except OSError as e:
        log.warning("Could not save state to %s: %s", path, e)


# --------------------------------------------------------------------------- player

class Player:
    """Runs mpv on a list of Items (one mpv process per list, so transitions
    within a list are smooth). `halt` stops the current and future runs."""

    def __init__(self):
        self.cmd = ["mpv"]
        self.max_unknown = None
        self.proc = None
        self.lock = threading.RLock()  # re-entrant: signal handler may fire mid-call
        self.halt = threading.Event()

    def configure(self, cfg):
        cmd = shlex.split(cfg["player_cmd"])
        if cfg["audio_device"]:
            cmd.append(f"--audio-device={cfg['audio_device']}")
        cmd.append(f"--input-ipc-server={IPC_SOCKET}")
        self.cmd = cmd
        self.max_unknown = cfg_int(cfg, "max_play_seconds", 0) or None

    @staticmethod
    def _item_args(it):
        a = ["--{"]
        if it.image:
            a += ["--hwdec=no", f"--image-display-duration={it.length}", f"--vf={IMAGE_VF}"]
        else:
            if it.start is not None:
                a.append(f"--start={it.start:.1f}")
            if it.length:
                a.append(f"--length={it.length}")
        # paths are absolute, so they can't be mistaken for options
        a += [str(it.path), "--}"]
        return a

    def _timeout(self, items):
        total = 0
        for it in items:
            if it.image:
                total += it.length + 10
            elif it.length:
                total += it.length + 20
            else:
                d = get_duration(it.path)
                if d:
                    total += d + 60
                elif self.max_unknown:
                    total += self.max_unknown
                else:
                    return None
        return total

    def play_items(self, items, label):
        """Returns True on clean exit."""
        if self.halt.is_set() or not items:
            return False
        args = []
        for it in items:
            args += self._item_args(it)
        if len(items) == 1:
            log.info("Playing: %s", items[0].path.name)
        else:
            log.info("Playing %s: %s", label, ", ".join(
                it.path.name + (f"@{int(it.start)}s" if it.start else "") for it in items))
        start = time.monotonic()
        with self.lock:
            if self.halt.is_set():
                return False
            self.proc = subprocess.Popen(
                self.cmd + args, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, errors="replace")
        proc = self.proc
        try:
            out, _ = proc.communicate(timeout=self._timeout(items))
        except subprocess.TimeoutExpired:
            log.warning("Timed out playing %s, killing player", label)
            self._kill()
            return False
        finally:
            with self.lock:
                self.proc = None

        if proc.returncode == 0:
            log.debug("Finished %s in %.0fs", label, time.monotonic() - start)
            return True
        if self.halt.is_set():
            return False
        tail = (out or "").strip().splitlines()[-5:]
        log.error("Player exited %s on %s%s", proc.returncode, label,
                  (": " + " | ".join(tail)) if tail else "")
        return False

    def skip(self):
        """Jump to the next item (or end the run if it's the last one)."""
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(1)
                s.connect(IPC_SOCKET)
                s.sendall(b'{"command": ["playlist-next", "force"]}\n')
            log.info("Skipped")
        except OSError:
            self._kill()

    def abort(self):
        self.halt.set()
        self._kill()

    def _kill(self):
        with self.lock:
            p = self.proc
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()


# --------------------------------------------------------------------------- engine

class Engine:
    def __init__(self, player):
        self.player = player
        self.state = None
        self.bags = {"clips": ShuffleBag(), "images": ShuffleBag(), "cuts": ShuffleBag()}

    def run(self, cfg, mode):
        """Play until player.halt is set (menu requested or shutdown)."""
        p = self.player
        if exit_event.is_set():
            return
        p.configure(cfg)
        p.halt.clear()
        if self.state is None:
            self.state = load_state(cfg["state_file"]) if as_bool(cfg["resume"]) else {}
        rescan = cfg_int(cfg, "rescan_interval", 1)
        log.info("Playing in %s mode", MODE_NAMES[mode])

        last_root, waiting, failures = object(), False, 0
        while not p.halt.is_set():
            lib = scan_library(cfg)
            if lib.root is None:
                if not waiting:
                    log.warning("No media in %s or %s, rechecking every %ss",
                                cfg["usb_root"], cfg["sd_root"], rescan)
                    waiting = True
                p.halt.wait(rescan)
                continue
            waiting = False
            if lib.root != last_root:
                log.info("Media source: %s (%d long, %d short, %d images)",
                         lib.root, len(lib.long), len(lib.short), len(lib.images))
                last_root = lib.root

            try:
                results = (self._quick_cuts(cfg, lib) if mode == "sample"
                           else self._episodes(cfg, lib))
            except FileNotFoundError:
                log.critical("Player not found: %s", p.cmd[0])
                p.halt.wait(30)
                continue

            if p.halt.is_set():
                break
            if not results:  # nothing playable with the current settings
                p.halt.wait(rescan)
                continue
            if any(results):
                failures = 0
            else:
                failures += 1
                delay = min(60, 5 * failures)
                log.error("All playback failed (%d in a row), waiting %ss", failures, delay)
                p.halt.wait(delay)
        log.info("Playback stopped")

    def _episodes(self, cfg, lib):
        p = self.player
        results = []
        if lib.long:
            names = [x.name for x in lib.long]
            nxt = self.state.get("next")
            idx = names.index(nxt) if nxt in names else 0
            results.append(p.play_items([Item(lib.long[idx], None, None, False)], "episode"))
            if p.halt.is_set():
                return results  # interrupted: replay this episode next time
            self.state = {"next": names[(idx + 1) % len(names)], "source": str(lib.root)}
            if as_bool(cfg["resume"]):
                save_state(cfg["state_file"], self.state)

        items = []
        if lib.short:
            items += [Item(self.bags["clips"].draw(lib.short), None, None, False)
                      for _ in range(cfg_int(cfg, "num_short_clips", 0))]
        if lib.images:
            secs = cfg_int(cfg, "image_seconds", 1)
            items += [Item(self.bags["images"].draw(lib.images), None, secs, True)
                      for _ in range(cfg_int(cfg, "num_images", 0))]
        random.shuffle(items)
        if items:
            results.append(p.play_items(items, f"break of {len(items)}"))
        return results

    def _quick_cuts(self, cfg, lib):
        length = cfg_int(cfg, "sample_seconds", 1)
        margin = cfg_int(cfg, "sample_margin", 0)
        pool = list(lib.long)
        if as_bool(cfg["sample_include_short"]) or not pool:
            pool += lib.short
        if as_bool(cfg["sample_include_images"]) or not pool:
            pool += lib.images
        if not pool:
            return []
        images = set(lib.images)
        img_secs = max(1, min(cfg_int(cfg, "image_seconds", 1), length))
        items = []
        for _ in range(cfg_int(cfg, "sample_batch", 1)):
            path = self.bags["cuts"].draw(pool)
            if path in images:
                items.append(Item(path, None, img_secs, True))
            else:
                items.append(Item(path, sample_start(get_duration(path), length, margin),
                                  length, False))
        return [self.player.play_items(items, f"{len(items)} quick cuts")]


# --------------------------------------------------------------------------- playback input

class InputWatcher(threading.Thread):
    """Reads keyboards/mice/TV remotes directly while mpv owns the screen."""

    def __init__(self, on_menu, on_skip):
        super().__init__(daemon=True)
        self.on_menu, self.on_skip = on_menu, on_skip
        self.stop_flag = threading.Event()

    def stop(self):
        self.stop_flag.set()

    def run(self):
        try:
            import evdev
            from evdev import ecodes as e
        except ImportError:
            log.warning("python3-evdev not installed: keyboard control during "
                        "playback disabled (sudo apt install python3-evdev)")
            return
        import select

        menu_keys = {e.KEY_ESC, e.KEY_Q, e.KEY_M, e.KEY_BACKSPACE, e.KEY_HOME,
                     e.KEY_BACK, e.KEY_EXIT, e.BTN_RIGHT}
        skip_keys = {e.KEY_N, e.KEY_RIGHT, e.KEY_SPACE, e.KEY_ENTER,
                     e.KEY_NEXT, e.KEY_NEXTSONG, e.BTN_LEFT}
        devices, last_scan = {}, 0.0
        while not self.stop_flag.is_set():
            if time.monotonic() - last_scan > 3:  # pick up hot-plugged devices
                last_scan = time.monotonic()
                for path in evdev.list_devices():
                    if path in devices:
                        continue
                    try:
                        d = evdev.InputDevice(path)
                    except OSError:
                        continue
                    if e.EV_KEY in d.capabilities():
                        devices[path] = d
                    else:
                        d.close()
            if not devices:
                self.stop_flag.wait(1)
                continue
            try:
                ready, _, _ = select.select(list(devices.values()), [], [], 0.5)
            except (OSError, ValueError):
                ready = []
            for d in ready:
                try:
                    events = list(d.read())
                except BlockingIOError:
                    continue
                except OSError:  # unplugged
                    devices.pop(d.path, None)
                    continue
                for ev in events:
                    if ev.type != e.EV_KEY or ev.value != 1 or self.stop_flag.is_set():
                        continue
                    if ev.code in menu_keys:
                        log.info("Menu requested")
                        self.on_menu()
                    elif ev.code in skip_keys:
                        self.on_skip()
        for d in devices.values():
            try:
                d.close()
            except OSError:
                pass


# --------------------------------------------------------------------------- UI

def ip_address():
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True,
                             text=True, timeout=2).stdout.split()
        return out[0] if out else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def rng(a, b):
    return [str(i) for i in range(a, b + 1)]


# (config key, label, choices, formatter)
SETTINGS = [
    ("num_short_clips", "Clips between episodes", rng(0, 10), "{}"),
    ("num_images", "Images between episodes", rng(0, 10), "{}"),
    ("image_seconds", "Image display time",
     ["3", "5", "8", "10", "15", "20", "30", "60"], "{}s"),
    ("sample_seconds", "Quick cut length",
     ["5", "10", "15", "20", "30", "45", "60", "90", "120"], "{}s"),
    ("sample_include_short", "Quick cuts use clips", ["no", "yes"], "yesno"),
    ("sample_include_images", "Quick cuts use images", ["no", "yes"], "yesno"),
    ("sample_margin", "Quick cuts skip intro/credits",
     ["0", "5", "10", "20", "30", "60"], "{}s"),
    ("autostart_seconds", "Auto-start after boot",
     ["0", "5", "10", "15", "30", "60"], "off/{}s"),
    ("idle_resume_seconds", "Resume if menu left idle",
     ["0", "60", "120", "300", "600", "1800"], "off/min"),
]


def fmt_setting(fmt, v):
    if fmt == "yesno":
        return "Yes" if as_bool(v) else "No"
    if fmt.startswith("off/"):
        if v in ("0", ""):
            return "Off"
        if fmt == "off/min":
            n = int(v)
            return f"{n // 60} min" if n % 60 == 0 else f"{n}s"
        return fmt[4:].format(v)
    return fmt.format(v)


class UI:
    BG = (16, 18, 26)
    PANEL = (30, 33, 46)
    PANEL_SEL = (46, 42, 40)
    ACCENT = (255, 170, 60)
    TEXT = (236, 237, 242)
    DIM = (140, 145, 162)
    WARN = (255, 115, 95)

    def __init__(self):
        os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
        if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
            os.environ.setdefault("SDL_VIDEODRIVER", "kmsdrm")
        import pygame
        self.pg = pygame
        self.screen = None
        self.open()

    # -- display lifecycle (mpv and the menu can't share the screen) --

    def open(self):
        pg = self.pg
        pg.display.init()   # not pygame.init(): that would grab the audio device
        pg.font.init()
        self.screen = pg.display.set_mode((0, 0), pg.FULLSCREEN)
        pg.display.set_caption("VideoNode")
        pg.mouse.set_visible(True)
        self.W, self.H = self.screen.get_size()
        u = self.u = self.H / 720
        self.f_title = pg.font.Font(None, int(76 * u))
        self.f_item = pg.font.Font(None, int(46 * u))
        self.f_row = pg.font.Font(None, int(38 * u))
        self.f_small = pg.font.Font(None, int(29 * u))
        pg.event.clear()

    def close(self):
        self.pg.display.quit()
        self.screen = None

    # -- drawing helpers --

    def text(self, s, font, color, pos, anchor="topleft"):
        surf = font.render(s, True, color)
        r = surf.get_rect(**{anchor: pos})
        self.screen.blit(surf, r)
        return r

    def panel(self, rect, selected):
        pg, u = self.pg, self.u
        rad = int(12 * u)
        pg.draw.rect(self.screen, self.PANEL_SEL if selected else self.PANEL, rect,
                     border_radius=rad)
        if selected:
            pg.draw.rect(self.screen, self.ACCENT, rect, width=max(2, int(3 * u)),
                         border_radius=rad)

    def header(self, title, cfg, lib):
        u, m = self.u, int(60 * self.u)
        self.screen.fill(self.BG)
        self.text(title, self.f_title, self.ACCENT, (m, int(38 * u)))
        if lib.root is None:
            lines = [("No media found", self.WARN)]
        else:
            src = "USB drive" if str(lib.root) == cfg["usb_root"] else "SD card"
            lines = [(f"Media on {src}", self.TEXT),
                     (f"{len(lib.long)} videos  ·  {len(lib.short)} clips  ·  "
                      f"{len(lib.images)} images", self.DIM)]
        ip = self._ip
        lines.append((f"Add files:  \\\\{socket.gethostname()}\\videos"
                      + (f"   ({ip})" if ip else ""), self.DIM))
        y = int(44 * u)
        for s, c in lines:
            y = self.text(s, self.f_small, c, (self.W - m, y), "topright").bottom + int(6 * u)

    def footer(self, lines):
        u, m = self.u, int(60 * self.u)
        y = self.H - int(28 * u)
        for s, c in reversed(lines):
            y = self.text(s, self.f_small, c, (m, y), "bottomleft").top - int(6 * u)

    def message(self, title, sub=None):
        if not self.screen:
            return
        self.screen.fill(self.BG)
        self.text(title, self.f_title, self.ACCENT, (self.W // 2, self.H // 2), "center")
        if sub:
            self.text(sub, self.f_small, self.DIM,
                      (self.W // 2, self.H // 2 + int(60 * self.u)), "center")
        self.pg.display.flip()

    # -- screens --

    def main_items(self, cfg):
        return [
            ("full", "Play Episodes",
             f"Videos in order, with {cfg['num_short_clips']} clips + "
             f"{cfg['num_images']} images between"),
            ("sample", "Play Quick Cuts",
             f"Random {cfg['sample_seconds']}-second cuts from your videos"),
            ("settings", "Settings", "Clip counts, timing, auto-start"),
            ("shutdown", "Shut Down", "Safely power off before unplugging"),
        ]

    def draw_main(self, cfg, lib, banner):
        u, m = self.u, int(60 * self.u)
        self.header("VideoNode", cfg, lib)
        self.rects = []
        w, h, y = int(min(self.W - 2 * m, 780 * u)), int(86 * u), int(190 * u)
        for i, (key, title, sub) in enumerate(self.main_items(cfg)):
            rect = self.pg.Rect(m, y, w, h)
            self.panel(rect, i == self.sel)
            self.text(title, self.f_item, self.TEXT, (rect.x + int(28 * u), rect.y + int(14 * u)))
            self.text(sub, self.f_small, self.DIM, (rect.x + int(28 * u), rect.y + int(54 * u)))
            if key == cfg["mode"]:
                self.text("last used", self.f_small, self.ACCENT,
                          (rect.right - int(24 * u), rect.y + int(18 * u)), "topright")
            self.rects.append((rect, i))
            y += h + int(14 * u)
        foot = [("Arrows + Enter, or the mouse, to choose.", self.DIM),
                ("While playing:  Esc / right-click = menu    N / Right / left-click = skip",
                 self.DIM)]
        if banner:
            foot.insert(0, banner)
        self.footer(foot)

    def draw_settings(self, cfg, lib):
        u, m = self.u, int(60 * self.u)
        self.header("Settings", cfg, lib)
        self.rects = []
        w, h, y = int(min(self.W - 2 * m, 900 * u)), int(44 * u), int(150 * u)
        rows = [(label, fmt_setting(fmt, self.pending.get(key, cfg[key])))
                for key, label, _c, fmt in SETTINGS] + [("Save and go back", None)]
        for i, (label, value) in enumerate(rows):
            rect = self.pg.Rect(m, y, w, h)
            self.panel(rect, i == self.sel)
            self.text(label, self.f_row, self.TEXT if value else self.ACCENT,
                      (rect.x + int(22 * u), rect.centery), "midleft")
            if value is not None:
                self.text(f"<   {value}   >", self.f_row, self.ACCENT,
                          (rect.right - int(22 * u), rect.centery), "midright")
            self.rects.append((rect, i))
            y += h + int(7 * u)
        self.footer([("Left/Right or click to change  ·  right-click or Esc to save and go back",
                      self.DIM)])

    def draw_confirm(self, cfg, lib):
        u, m = self.u, int(60 * self.u)
        self.header("Shut down?", cfg, lib)
        self.rects = []
        w, h, y = int(min(self.W - 2 * m, 520 * u)), int(70 * u), int(210 * u)
        for i, label in enumerate(["Cancel", "Shut Down"]):
            rect = self.pg.Rect(m, y, w, h)
            self.panel(rect, i == self.sel)
            self.text(label, self.f_item, self.WARN if i else self.TEXT,
                      (rect.x + int(28 * u), rect.centery), "midleft")
            self.rects.append((rect, i))
            y += h + int(14 * u)
        self.footer([("Wait for the green light on the Pi to stop flashing before unplugging.",
                      self.DIM)])

    # -- menu loop --

    def change_setting(self, cfg, i, delta):
        key, _label, choices, _fmt = SETTINGS[i]
        cur = str(self.pending.get(key, cfg[key])).strip().lower()
        if cur in choices:
            idx = choices.index(cur)
        else:
            try:  # nearest numeric choice to a hand-edited value
                idx = min(range(len(choices)),
                          key=lambda j: abs(float(choices[j]) - float(cur)))
            except ValueError:
                idx = 0
        self.pending[key] = choices[(idx + delta) % len(choices)]

    def menu(self, get_cfg, save, countdown=0, flash=None):
        """Run the menu until the user picks something. Returns 'full',
        'sample', 'shutdown', or None if the app is exiting."""
        pg = self.pg
        clock = pg.time.Clock()
        cfg = get_cfg()
        lib = scan_library(cfg)
        self._ip = ip_address()
        self.view, self.pending = "main", {}
        self.sel = 0 if cfg["mode"] == "full" else 1
        now = time.monotonic()
        last_scan = last_input = last_ip = now
        auto_at = now + countdown if countdown > 0 else None
        flash_until = now + 8 if flash else 0

        def leave_settings():
            nonlocal cfg, flash, flash_until
            if self.pending:
                ok = save(self.pending)
                flash = ("Settings saved" if ok else "Could not save settings",
                         self.ACCENT if ok else self.WARN)
                flash_until = time.monotonic() + 4
                cfg = get_cfg()
            self.pending, self.view, self.sel = {}, "main", 2

        def activate(i):
            """Returns an action string to leave the menu, else None."""
            if self.view == "main":
                key = self.main_items(cfg)[i][0]
                if key in MODE_NAMES:
                    if cfg["mode"] != key:
                        save({"mode": key})
                    return key
                if key == "settings":
                    self.view, self.sel, self.pending = "settings", 0, {}
                elif key == "shutdown":
                    self.view, self.sel = "confirm", 0
            elif self.view == "settings":
                if i < len(SETTINGS):
                    self.change_setting(cfg, i, +1)
                else:
                    leave_settings()
            elif self.view == "confirm":
                if i == 1:
                    return "shutdown"
                self.view, self.sel = "main", 3
            return None

        def back():
            if self.view == "settings":
                leave_settings()
            elif self.view == "confirm":
                self.view, self.sel = "main", 3

        while not exit_event.is_set():
            now = time.monotonic()
            if now - last_scan > 3:
                if self.view != "settings":
                    cfg = get_cfg()  # picks up edits made over the network share
                lib = scan_library(cfg)
                last_scan = now
            if now - last_ip > 15:
                self._ip = ip_address()
                last_ip = now
            for ev in pg.event.get():
                n_rows = {"main": 4, "settings": len(SETTINGS) + 1, "confirm": 2}[self.view]
                if ev.type == pg.MOUSEMOTION:
                    if abs(ev.rel[0]) + abs(ev.rel[1]) > 4:
                        last_input, auto_at = now, None
                    for rect, i in self.rects:
                        if rect.collidepoint(ev.pos):
                            self.sel = i
                    continue
                if ev.type in (pg.KEYDOWN, pg.MOUSEBUTTONDOWN, pg.MOUSEWHEEL):
                    last_input, auto_at = now, None
                action = None
                if ev.type == pg.KEYDOWN:
                    k = ev.key
                    if k in (pg.K_UP, pg.K_w):
                        self.sel = (self.sel - 1) % n_rows
                    elif k in (pg.K_DOWN, pg.K_s, pg.K_TAB):
                        self.sel = (self.sel + 1) % n_rows
                    elif k in (pg.K_RETURN, pg.K_KP_ENTER, pg.K_SPACE):
                        action = activate(self.sel)
                    elif k in (pg.K_ESCAPE, pg.K_BACKSPACE):
                        back()
                    elif k in (pg.K_LEFT, pg.K_RIGHT) and self.view == "settings" \
                            and self.sel < len(SETTINGS):
                        self.change_setting(cfg, self.sel, 1 if k == pg.K_RIGHT else -1)
                elif ev.type == pg.MOUSEBUTTONDOWN and ev.button in (1, 3):
                    hit = next((i for r, i in self.rects if r.collidepoint(ev.pos)), None)
                    if ev.button == 3:
                        back()
                    elif hit is not None:
                        self.sel = hit
                        rect = self.rects[hit][0]
                        # clicking the left third of a setting row decreases it
                        if self.view == "settings" and hit < len(SETTINGS) \
                                and ev.pos[0] < rect.x + rect.w // 3:
                            self.change_setting(cfg, hit, -1)
                        else:
                            action = activate(hit)
                elif ev.type == pg.MOUSEWHEEL and self.view == "settings" \
                        and self.sel < len(SETTINGS):
                    self.change_setting(cfg, self.sel, 1 if ev.y > 0 else -1)
                if action:
                    return action

            # auto-start at boot / resume when idle (only if there's something to play)
            banner = None
            mode_name = MODE_NAMES[cfg["mode"]]
            if lib.root is None:
                if auto_at:
                    auto_at = now + countdown
                banner = ("Copy videos into the long, short or images folders to get started.",
                          self.WARN)
            else:
                idle = cfg_int(cfg, "idle_resume_seconds", 0)
                if auto_at:
                    left = auto_at - now
                    if left <= 0:
                        return cfg["mode"]
                    banner = (f"Starting {mode_name} in {int(left) + 1}s  —  "
                              "press any key to stay in the menu", self.ACCENT)
                elif idle and self.view == "main":
                    left = idle - (now - last_input)
                    if left <= 0:
                        log.info("Menu idle, resuming playback")
                        return cfg["mode"]
                    if left <= 15:
                        banner = (f"Resuming {mode_name} in {int(left) + 1}s", self.ACCENT)
            if flash and now < flash_until:
                banner = flash if isinstance(flash, tuple) else (flash, self.WARN)

            {"main": lambda: self.draw_main(cfg, lib, banner),
             "settings": lambda: self.draw_settings(cfg, lib),
             "confirm": lambda: self.draw_confirm(cfg, lib)}[self.view]()
            pg.display.flip()
            clock.tick(20)
        return None


# --------------------------------------------------------------------------- main

def shutdown_pi():
    try:
        r = subprocess.run(["sudo", "-n", "systemctl", "poweroff"],
                           capture_output=True, text=True, timeout=15)
        if r.returncode == 0:
            return None
        return (r.stderr or r.stdout).strip() or f"exit code {r.returncode}"
    except (OSError, subprocess.TimeoutExpired) as e:
        return str(e)


def main():
    args = parse_args()
    cfg = read_config(args)
    setup_logging(cfg)
    if not cfg["_found"]:
        log.warning("Config %s not found, using defaults", args.config)

    player = Player()
    engine = Engine(player)

    def on_signal(signum, _frame):
        log.info("Received %s, shutting down", signal.Signals(signum).name)
        exit_event.set()
        player.abort()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    def play(mode):
        watcher = InputWatcher(on_menu=player.abort, on_skip=player.skip)
        watcher.start()
        try:
            engine.run(read_config(args), mode)
        finally:
            watcher.stop()

    ui = None
    if not args.no_ui and as_bool(cfg["ui"]):
        try:
            ui = UI()
        except Exception as e:  # no pygame, no display, etc.
            log.warning("Menu unavailable (%s), playing without it", e)

    log.info("videonode starting (%s)", "menu" if ui else "no menu")

    if ui is None:
        # Headless: Esc just restarts the current mode.
        while not exit_event.is_set():
            play(read_config(args)["mode"])
        return 0

    first, flash = True, None
    while not exit_event.is_set():
        cfg = read_config(args)
        countdown = cfg_int(cfg, "autostart_seconds", 0) if first else 0
        action = ui.menu(lambda: read_config(args),
                         lambda ch: update_conf_file(args.config, ch),
                         countdown=countdown, flash=flash)
        first, flash = False, None
        if action is None:
            break
        if action == "shutdown":
            ui.message("Shutting down...", "Unplug once the green light stops flashing")
            err = shutdown_pi()
            if err is None:
                exit_event.wait(60)  # systemd will stop us
                break
            log.error("Shutdown failed: %s", err)
            flash = f"Shutdown failed: {err[:70]}"
            continue

        ui.message(f"Starting {MODE_NAMES[action]}...")
        ui.close()
        play(action)
        if exit_event.is_set():
            break
        try:
            ui.open()
        except Exception as e:
            log.error("Could not reopen the menu (%s); restarting", e)
            return 1  # systemd restarts us with a fresh display

    if ui.screen:
        ui.close()
    log.info("videonode stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
