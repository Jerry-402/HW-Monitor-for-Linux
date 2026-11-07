#!/usr/bin/env python3
"""
HW Monitor
==========
A lightweight, HWInfo-style hardware sensor monitor for Linux, built with
Qt6 (PySide6) so it's a native citizen on KDE Plasma - same toolkit as
Plasma itself, System Settings, Dolphin, etc. Tested on CachyOS (Arch-based)
running Plasma, but works on any distro with PySide6 (Debian/Ubuntu/Mint via
apt, Fedora via dnf, Arch/CachyOS via pacman). Missing-dependency messages
auto-detect the local package manager and print the right install command.

Shows a live, expandable tree of sensor readings - CPU, memory, GPU,
motherboard (voltages / fans / temps via lm-sensors), storage, network and
battery - each with Current / Minimum / Maximum / Average columns, similar to
HWInfo's "Sensors" tab.

Double-clicking a numeric sensor row pops out a small, independent live-graph
window for it - you can have several open side by side, just like HWiNFO's
floating sensor graphs. Graphs include labeled X (Time) and Y (Value) axes.

Theming: because this is a real Qt6 app, it automatically inherits whatever
color scheme, accent color, and widget style you have set in Plasma's System
Settings - no manual theme-file parsing needed. The handful of places that
draw themselves manually (the live-graph windows, category headers, and
warning/critical sensor colors) read their colors from the app's live
QPalette, and refresh immediately via Qt's paletteChanged signal whenever
you change your Plasma theme while the app is running.

Every hardware source is read defensively: if a tool or sysfs path isn't
available, that category (or row) is simply skipped rather than crashing.
"""

import shutil
import sys


def _detect_package_manager():
    """Best-effort detection of the system package manager, so dependency
    hints can be accurate on Arch/CachyOS (pacman), Debian/Ubuntu/Mint
    (apt), and Fedora (dnf) alike."""
    for pm in ("pacman", "apt", "dnf", "zypper"):
        if shutil.which(pm):
            return pm
    return None


PKG_MANAGER = _detect_package_manager()


def install_cmd(pacman=None, apt=None, dnf=None):
    """Returns a ready-to-run install command for the detected package
    manager. If none of the known ones were found, lists every option we
    have so the message is still useful."""
    if PKG_MANAGER == "pacman" and pacman:
        return f"sudo pacman -S {pacman}"
    if PKG_MANAGER == "apt" and apt:
        return f"sudo apt install {apt}"
    if PKG_MANAGER == "dnf" and dnf:
        return f"sudo dnf install {dnf}"
    options = []
    if pacman:
        options.append(f"sudo pacman -S {pacman}")
    if apt:
        options.append(f"sudo apt install {apt}")
    if dnf:
        options.append(f"sudo dnf install {dnf}")
    return " / ".join(options)


try:
    from PySide6 import QtCore, QtGui, QtWidgets
    from PySide6.QtCore import Qt
except ImportError as exc:
    sys.exit(
        "HW Monitor needs PySide6 (the official Qt6 bindings for Python).\n"
        "Install it with:\n"
        f"    {install_cmd(pacman='pyside6', apt='python3-pyside6', dnf='python3-pyside6')}\n"
        "If your distribution doesn't package PySide6, use a virtual environment and:\n"
        "    pip install PySide6\n"
        f"\n(underlying error: {exc})"
    )

import collections
import glob
import json
import os
import re
import shlex
import subprocess
import threading
import time

try:
    import psutil
except ImportError:
    psutil = None
    print(
        "Note: psutil is not installed, so CPU / memory / storage / network / "
        "battery readings will be unavailable.\n"
        f"Install it with: {install_cmd(pacman='python-psutil', apt='python3-psutil', dnf='python3-psutil')}",
        file=sys.stderr,
    )

# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def run(cmd, timeout=2):
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode == 0:
            return result.stdout
    except Exception:
        pass
    return None


def read_sysfs(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return None


def fmt(value, unit, decimals=1):
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.{decimals}f} {unit}".strip()
    return f"{value} {unit}".strip()


def read_uptime_seconds():
    """Seconds since boot. /proc/uptime counts time spent suspended too."""
    raw = read_sysfs("/proc/uptime")
    try:
        return float(raw.split()[0])
    except (AttributeError, IndexError, ValueError):
        pass
    if psutil:
        try:
            return time.time() - psutil.boot_time()
        except Exception:
            pass
    return None


def fmt_uptime(seconds):
    """3d 04:12:09, or 04:12:09 when under a day. Fixed-width fields so the
    summary card doesn't jiggle as the digits change."""
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    clock = f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{days}d {clock}" if days else clock


def get_hardware_names():
    names = {
        "cpu": "Processor",
        "mem": "Memory",
        "gpu": "Graphics",
        "mobo": "Motherboard",
        "net": "Network"
    }
    
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.lower().startswith("model name"):
                    names["cpu"] = line.split(":", 1)[1].strip()
                    break
    except Exception:
        pass

    vendor = read_sysfs("/sys/class/dmi/id/board_vendor")
    name = read_sysfs("/sys/class/dmi/id/board_name")
    if vendor and name:
        names["mobo"] = f"Motherboard [{vendor} {name}]"
    elif name:
        names["mobo"] = f"Motherboard [{name}]"

    total_gb = 0
    if psutil:
        try:
            vm = psutil.virtual_memory()
            total_gb = vm.total / (1024**3)
            names["mem"] = f"Memory [{total_gb:.0f} GB]"
        except Exception:
            pass

    try:
        out = run(["dmidecode", "-t", "memory"], timeout=2)
        if out:
            manufacturer = re.search(r"Manufacturer:\s*(.+)", out)
            part = re.search(r"Part Number:\s*(.+)", out)
            if manufacturer and part:
                m = manufacturer.group(1).strip()
                p = part.group(1).strip()
                if m not in ("Unknown", "None", "") and p not in ("Unknown", "None", ""):
                    names["mem"] = f"Memory [{m} {p} ({total_gb:.0f} GB)]"
    except Exception:
        pass

    lspci = run(["lspci"])
    if lspci:
        for line in lspci.splitlines():
            lower_line = line.lower()
            if "vga compatible controller" in lower_line or "3d controller" in lower_line or "display controller" in lower_line:
                gpu_name = re.sub(r'^.*controller: ', '', line)
                gpu_name = re.sub(r'\s*\(rev.*\)$', '', gpu_name).strip()
                if gpu_name:
                    names["gpu"] = f"Graphics [{gpu_name}]"
            elif "ethernet controller" in lower_line or "network controller" in lower_line:
                net_name = re.sub(r'^.*controller: ', '', line)
                net_name = re.sub(r'\s*\(rev.*\)$', '', net_name).strip()
                if net_name:
                    names["net"] = f"Network [{net_name}]"

    return names


# Kernel sysfs locations. Kept as module constants (rather than scattered
# string literals) so the discovery code below can be tested against a fake
# tree, and so there's one place to look when a path moves between kernels.
SYS_BLOCK = "/sys/block"
SYS_CLASS_BLOCK = "/sys/class/block"
SYS_CLASS_HWMON = "/sys/class/hwmon"
SYS_CLASS_THERMAL = "/sys/class/thermal"
SYS_CLASS_DRM = "/sys/class/drm"
SYS_CLASS_DEVFREQ = "/sys/class/devfreq"
SYS_CLASS_NVME = "/sys/class/nvme"
SYS_PCI_DEVICES = "/sys/bus/pci/devices"
SYS_POWERCAP = "/sys/class/powercap"

# Whole-disk names we treat as drives: SATA/SAS/USB (sd), legacy IDE (hd),
# virtio and Xen virtual disks (vd/xvd), NVMe namespaces, and SD/eMMC cards
# (mmcblk, but not their boot0/boot1/rpmb pseudo-devices).
DRIVE_RE = re.compile(r"^(sd[a-z]+|hd[a-z]+|vd[a-z]+|xvd[a-z]+|nvme\d+n\d+|mmcblk\d+)$")


def drive_model(base):
    """Human-readable model name for a whole-disk device like 'sda'."""
    model = read_sysfs(f"{SYS_BLOCK}/{base}/device/model")
    if not model and base.startswith("nvme"):
        model = read_sysfs(f"{SYS_CLASS_NVME}/{re.sub(r'n[0-9]+$', '', base)}/model")
    if not model and base.startswith("mmcblk"):
        name = read_sysfs(f"{SYS_BLOCK}/{base}/device/name")
        kind = read_sysfs(f"{SYS_BLOCK}/{base}/device/type")  # "SD" or "MMC"
        model = f"{kind} card {name}" if kind and name else name
    if not model and base.startswith(("vd", "xvd")):
        model = "Virtual disk"
    return (model or base).strip()


def _resolve_disks(name, depth=0):
    """Whole-disk names underneath a block device, following partitions and
    stacked devices (LVM, LUKS/dm-crypt, md RAID) down to the real disks."""
    if depth > 8:
        return []
    sys_path = os.path.realpath(f"{SYS_CLASS_BLOCK}/{name}")
    if not os.path.isdir(sys_path):
        return []
    if os.path.exists(os.path.join(sys_path, "partition")):
        sys_path = os.path.dirname(sys_path)  # a partition lives inside its disk's directory
        name = os.path.basename(sys_path)
    slaves = os.path.join(sys_path, "slaves")
    if os.path.isdir(slaves):
        found = []
        for slave in sorted(os.listdir(slaves)):
            for disk in _resolve_disks(slave, depth + 1):
                if disk not in found:
                    found.append(disk)
        if found:
            return found
    return [name]


def base_disks_for_device(device):
    """Physical disk(s) behind a mounted device path such as /dev/nvme0n1p2,
    /dev/mapper/luks-... or /dev/md0. First entry is the primary disk."""
    try:
        return _resolve_disks(os.path.basename(os.path.realpath(device)))
    except Exception:
        return []


def get_storage_groups():
    groups = {}
    try:
        for base in sorted(os.listdir(SYS_BLOCK)):
            if DRIVE_RE.match(base):
                groups.setdefault(drive_model(base), []).append(base)
    except Exception:
        pass

    group_list = list(groups.items())

    # Put the drive holding "/" first
    if psutil:
        try:
            for part in psutil.disk_partitions(all=False):
                if part.mountpoint == "/":
                    root_disks = base_disks_for_device(part.device)
                    for i, (model, devs) in enumerate(group_list):
                        if root_disks and root_disks[0] in devs:
                            group_list.insert(0, group_list.pop(i))
                            break
                    break
        except Exception:
            pass

    return group_list


# --------------------------------------------------------------------------
# Graphics devices
# --------------------------------------------------------------------------

GPU_VENDOR_SHORT = {"0x10de": "NVIDIA", "0x1002": "AMD", "0x8086": "Intel"}
# Platform (non-PCI) GPUs on ARM boards show up as devfreq devices
SOC_GPU_HINTS = ("gpu", "mali", "kgsl", "v3d", "panfrost", "agx", "adreno")


def _gpu_pci_name(addr, vendor):
    short = GPU_VENDOR_SHORT.get(vendor, "")
    out = run(["lspci", "-mm", "-s", addr])
    if out and out.strip():
        try:
            fields = shlex.split(out.strip().splitlines()[0])  # slot, class, vendor, device, ...
            device = fields[3]
            bracket = re.search(r"\[([^\]]+)\]\s*$", device)
            device = bracket.group(1) if bracket else device
            vend = short or re.sub(r"\s*(\[.*\]|Corporation|Corp\.?|Inc\.?|Ltd\.?).*$", "", fields[2]).strip()
            return f"{vend} {device}".strip()
        except Exception:
            pass
    return f"{short or 'Unknown'} graphics device ({addr})"


def get_gpus():
    """Every graphics device: PCI display controllers (class 0x03xx, which
    includes the '3D controller' class used by laptop discrete GPUs), plus
    devfreq-based GPUs on ARM SoCs."""
    gpus = []
    try:
        cards = {}
        for card in glob.glob(f"{SYS_CLASS_DRM}/card*"):
            if re.search(r"/card\d+$", card):
                cards[os.path.realpath(card + "/device")] = card
        for dev in sorted(glob.glob(f"{SYS_PCI_DEVICES}/*")):
            if not (read_sysfs(dev + "/class") or "").startswith("0x03"):
                continue
            addr = os.path.basename(dev)
            vendor = (read_sysfs(dev + "/vendor") or "").lower()
            driver = os.path.basename(os.path.realpath(dev + "/driver")) if os.path.exists(dev + "/driver") else ""
            gpus.append({
                "addr": addr, "path": dev, "vendor": vendor, "driver": driver,
                "name": _gpu_pci_name(addr, vendor), "card": cards.get(os.path.realpath(dev)),
            })
    except Exception:
        pass
    try:
        for path in sorted(glob.glob(f"{SYS_CLASS_DEVFREQ}/*")):
            base = os.path.basename(path)
            if any(hint in base.lower() for hint in SOC_GPU_HINTS):
                gpus.append({"addr": base, "path": path, "vendor": None, "driver": "",
                             "name": base, "card": None, "devfreq": path})
    except Exception:
        pass
    return gpus


def _first_int(paths):
    for path in paths:
        value = read_sysfs(path)
        if value and value.strip().isdigit():
            return int(value)
    return None


# --------------------------------------------------------------------------
# Sensor value store
# --------------------------------------------------------------------------

# How much graph history to keep per sensor. The pop-out graphs show more
# time the wider you make them (see LiveGraph), so this just needs to exceed
# the widest realistic window: 30 minutes is ~27,000 px of graph at the
# default scale.
HISTORY_SECONDS = 1800


class SensorStore:
    def __init__(self):
        self.data = {}

    def update(self, sensor_id, label, value, unit, decimals=1):
        if value is None:
            return
        is_text = isinstance(value, str)
        d = self.data.get(sensor_id)
        if d is None:
            d = {"unit": unit, "decimals": decimals, "is_text": is_text, "history": collections.deque()}
            if not is_text:
                d.update(min=value, max=value, sum=0.0, n=0)
            self.data[sensor_id] = d
        d["label"] = label
        d["cur"] = value
        if not is_text:
            d["min"] = min(d["min"], value)
            d["max"] = max(d["max"], value)
            d["sum"] += value
            d["n"] += 1
            now = time.time()
            hist = d["history"]
            hist.append((now, value))  # (timestamp, value): the graph's x-axis is real time
            cutoff = now - HISTORY_SECONDS
            while hist and hist[0][0] < cutoff:
                hist.popleft()

    def reset_minmax(self):
        for d in self.data.values():
            if d.get("is_text") or "cur" not in d:
                continue
            d["min"] = d["cur"]
            d["max"] = d["cur"]
            d["sum"] = d["cur"]
            d["n"] = 1
            d["history"] = collections.deque([(time.time(), d["cur"])])

    def row(self, sensor_id):
        d = self.data[sensor_id]
        if d.get("is_text"):
            return (str(d["cur"]), "", "", "")
        avg = d["sum"] / d["n"] if d["n"] else d["cur"]
        dec = d["decimals"]
        return (
            fmt(d["cur"], d["unit"], dec),
            fmt(d["min"], d["unit"], dec),
            fmt(d["max"], d["unit"], dec),
            fmt(avg, d["unit"], dec),
        )

    def get_history(self, sensor_id):
        d = self.data.get(sensor_id)
        if d and not d.get("is_text"):
            return d.get("history", [])
        return []


# --------------------------------------------------------------------------
# Hardware collectors
# --------------------------------------------------------------------------

class RaplHelper:
    """Reads the CPU's energy counters as root, for machines where the kernel
    only lets root read them. That covers most AMD Ryzen systems and recent
    Intel ones: since 2020 the kernel restricts these files because they
    could be used as a side channel to leak secrets.

    One polkit password prompt starts a tiny fixed shell loop. All it does is
    `cat` the energy_uj files once a second and print them. It is a fixed
    command run through /bin/sh (never a user-writable script), and it ends
    on its own when this app closes, because its output pipe breaks."""

    def __init__(self):
        self.state = "idle"   # idle | starting | running | denied | stopped | failed
        self.watts = {}       # zone name -> latest watts
        self._prev = {}
        self._lock = threading.Lock()
        self._proc = None

    @staticmethod
    def script():
        return (f'while :; do echo "T $(date +%s.%N)"; '
                f'for z in {SYS_POWERCAP}/intel-rapl:?; do '
                f'[ -r "$z/energy_uj" ] && echo "Z $(cat "$z/name") $(cat "$z/energy_uj")"; '
                f'done; sleep 1; done')

    def start(self):
        if self.state in ("starting", "running"):
            return
        pkexec = shutil.which("pkexec")
        if not pkexec:
            self.state = "failed"
            return
        with self._lock:
            self.watts, self._prev = {}, {}
        self.state = "starting"
        try:
            self._proc = subprocess.Popen(
                [pkexec, "/bin/sh", "-c", self.script()],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                text=True, bufsize=1)
        except Exception:
            self.state = "failed"
            return
        threading.Thread(target=self._read_loop, args=(self._proc,), daemon=True).start()

    def _read_loop(self, proc):
        stamp = time.time()
        got_data = False
        try:
            for line in proc.stdout:
                parts = line.split()
                if not parts:
                    continue
                if parts[0] == "T" and len(parts) > 1:
                    try:
                        stamp = float(parts[1])
                    except ValueError:
                        stamp = time.time()  # `date` without nanosecond support
                elif parts[0] == "Z" and len(parts) >= 3 and parts[2].isdigit():
                    got_data = True
                    self.state = "running"
                    name, energy = parts[1], int(parts[2])
                    prev = self._prev.get(name)
                    self._prev[name] = (energy, stamp)
                    if prev and stamp > prev[1] and energy >= prev[0]:
                        with self._lock:
                            self.watts[name] = (energy - prev[0]) / 1_000_000 / (stamp - prev[1])
        except (ValueError, OSError):
            pass  # pipe closed by stop()
        code = proc.wait()
        if self.state != "stopped":
            self.state = "stopped" if got_data else ("denied" if code in (126, 127) else "failed")

    def snapshot(self):
        with self._lock:
            return dict(self.watts)

    def stop(self):
        self.state = "stopped"
        try:
            self._proc.stdout.close()  # the root loop then dies of SIGPIPE at its next write
        except Exception:
            pass


RAPL_HELPER = RaplHelper()


class Collectors:
    def __init__(self):
        self.rapl_prev = {}
        self.rapl_helper = RAPL_HELPER
        self.power_status = "unknown"  # ok | helper | needs_admin | unsupported
        self.net_prev = None
        self.net_prev_time = None
        self.disk_prev = {}  # dev -> (io counters, timestamp) for speed deltas
        self.gpu_prev = {}   # per-GPU counters for rate calculations (energy, RC6 residency)
        self._nv_all = None  # (timestamp, lines) cache of one nvidia-smi call covering all GPUs
        self._nvsmi_id_ok = None  # does this nvidia-smi accept -i <pci address>?

    CPU_SYSFS = "/sys/devices/system/cpu"

    @staticmethod
    def _parse_cpu_list(text):
        """Parses sysfs lists like '0,6' or '0-1' into a list of ints."""
        out = []
        for part in (text or "").split(","):
            part = part.strip()
            if "-" in part:
                lo, _, hi = part.partition("-")
                if lo.isdigit() and hi.isdigit():
                    out.extend(range(int(lo), int(hi) + 1))
            elif part.isdigit():
                out.append(int(part))
        return out

    def _cpu_topology(self):
        """Maps logical CPU number -> (physical core index, thread index).
        psutil reports one entry per hardware thread, so on an SMT/
        Hyper-Threading CPU a 6-core chip shows 12 entries. Reading the real
        layout from sysfs lets us label them as Core N / Thread M."""
        if getattr(self, "_topo_cache", None) is not None:
            return self._topo_cache
        cores = {}
        for path in glob.glob(os.path.join(self.CPU_SYSFS, "cpu[0-9]*")):
            n = os.path.basename(path)[3:]
            if not n.isdigit():
                continue
            pkg = read_sysfs(os.path.join(path, "topology/physical_package_id"))
            core = read_sysfs(os.path.join(path, "topology/core_id"))
            sibs = self._parse_cpu_list(read_sysfs(os.path.join(path, "topology/thread_siblings_list")))
            if pkg is None or core is None or int(n) not in sibs:
                continue
            cores[int(n)] = ((pkg, core), sorted(sibs))
        keys = sorted({k for k, _ in cores.values()}, key=lambda k: (int(k[0]), int(k[1])))
        index = {k: i for i, k in enumerate(keys)}
        topo = {n: (index[k], sibs.index(n)) for n, (k, sibs) in cores.items()}
        self._topo_smt = any(t > 0 for _, t in topo.values())
        self._topo_cache = topo
        return topo

    @staticmethod
    def _cpu_sort_key(i, topo):
        return topo.get(i, (10**6 + i, 0))

    def _cpu_name(self, i, topo):
        if i not in topo:
            return f"CPU {i}"  # no topology info: honest logical-CPU label
        core, thread = topo[i]
        return f"Core {core} Thread {thread}" if self._topo_smt else f"Core {core}"

    def collect_cpu(self):
        rows = []
        if psutil is None:
            return rows
        try:
            overall = psutil.cpu_percent(interval=None)
            rows.append(("total", "Total CPU Usage", overall, "%", 0))
            loads = psutil.cpu_percent(interval=None, percpu=True)
            topo = self._cpu_topology()
            for i in sorted(range(len(loads)), key=lambda n: self._cpu_sort_key(n, topo)):
                rows.append((f"core{i}.load", f"{self._cpu_name(i, topo)} Usage", loads[i], "%", 0))
        except Exception:
            pass
        try:
            freqs = psutil.cpu_freq(percpu=True)
            if freqs:
                topo = self._cpu_topology()
                for i in sorted(range(len(freqs)), key=lambda n: self._cpu_sort_key(n, topo)):
                    rows.append((f"core{i}.freq", f"{self._cpu_name(i, topo)} Clock", freqs[i].current, "MHz", 0))
        except Exception:
            pass
        try:
            temps = psutil.sensors_temperatures()
            for chip in ("coretemp", "k10temp", "zenpower"):
                for t in temps.get(chip, []):
                    label = t.label or chip
                    rows.append((f"temp.{label}", label, t.current, "°C", 1))
            if not any(r[0].startswith("temp.") for r in rows):
                # Not an Intel/AMD x86 sensor: Raspberry Pi and other ARM
                # SoCs call theirs things like cpu_thermal / soc_thermal
                for chip, entries in temps.items():
                    if any(k in chip.lower() for k in ("cpu", "soc")):
                        for t in entries:
                            label = t.label or chip
                            rows.append((f"temp.{label}", label, t.current, "°C", 1))
        except Exception:
            pass
        if not any(r[0].startswith("temp.") for r in rows):
            try:
                for zone in sorted(glob.glob(f"{SYS_CLASS_THERMAL}/thermal_zone*")):
                    ztype = (read_sysfs(zone + "/type") or "").lower()
                    raw = read_sysfs(zone + "/temp")
                    if raw and raw.lstrip("-").isdigit() and any(k in ztype for k in ("x86_pkg_temp", "cpu", "soc")):
                        rows.append((f"temp.{ztype}", ztype, int(raw) / 1000, "°C", 1))
            except Exception:
                pass
        try:
            rows.extend(self._cpu_power_rows())
        except Exception:
            pass
        return rows

    # Pretty names for the zenpower driver's SVI2 telemetry channels
    ZEN_POWER_NAMES = {"SVI2_P_Core": "CPU Core Power", "SVI2_P_SoC": "CPU SoC Power"}

    def _cpu_power_rows(self):
        """CPU power draw, from the first sources that work without root:
        RAPL energy counters (Intel, and AMD Zen on kernel 5.8+), AMD's
        amd_energy driver, or the out-of-tree zenpower driver. If the kernel
        only lets root read them, the optional admin helper is used."""
        rows = []
        zones_present = unreadable = False

        for zone in sorted(glob.glob(f"{SYS_POWERCAP}/intel-rapl:?")):
            if not os.path.exists(zone + "/energy_uj"):
                continue
            zones_present = True
            energy = read_sysfs(zone + "/energy_uj")
            if energy is None or not energy.isdigit():
                unreadable = True
                continue
            name = read_sysfs(zone + "/name") or os.path.basename(zone)
            energy, now = int(energy), time.time()
            prev = self.rapl_prev.get(zone)
            self.rapl_prev[zone] = (energy, now)
            if prev and now > prev[1] and energy >= prev[0]:
                rows.append((f"power.{name}", f"{name.title()} Power",
                             (energy - prev[0]) / 1_000_000 / (now - prev[1]), "W", 1))

        # AMD's amd_energy hwmon driver (per-socket totals only)
        for hwmon in sorted(glob.glob(f"{SYS_CLASS_HWMON}/hwmon*")):
            if read_sysfs(hwmon + "/name") != "amd_energy":
                continue
            for label_file in sorted(glob.glob(hwmon + "/energy*_label")):
                label = read_sysfs(label_file) or ""
                if not label.lower().startswith("esocket"):
                    continue
                n = os.path.basename(label_file).split("_")[0]
                energy = read_sysfs(f"{hwmon}/{n}_input")
                if not energy or not energy.isdigit():
                    continue
                energy, now = int(energy), time.time()
                prev = self.rapl_prev.get((hwmon, n))
                self.rapl_prev[(hwmon, n)] = (energy, now)
                if prev and now > prev[1] and energy >= prev[0]:
                    rows.append((f"power.{label}", f"CPU Socket {label[7:]} Power",
                                 (energy - prev[0]) / 1_000_000 / (now - prev[1]), "W", 1))

        # zenpower / zenpower3: live power straight from the Ryzen SVI2 interface
        for hwmon in sorted(glob.glob(f"{SYS_CLASS_HWMON}/hwmon*")):
            if read_sysfs(hwmon + "/name") != "zenpower":
                continue
            total = 0.0
            found = []
            for pf in sorted(glob.glob(hwmon + "/power*_input")):
                value = read_sysfs(pf)
                if not value or not value.isdigit():
                    continue
                label = read_sysfs(pf.replace("_input", "_label")) or os.path.basename(pf)
                watts = int(value) / 1_000_000
                total += watts
                found.append((f"power.zen.{label}", self.ZEN_POWER_NAMES.get(label, f"{label} Power"), watts, "W", 1))
            rows.extend(found)
            if len(found) > 1:
                rows.append(("power.zen.total", "CPU Total Power (SVI2)", total, "W", 1))

        if rows:
            self.power_status = "ok"
            return rows

        helper = self.rapl_helper
        if helper.state in ("starting", "running"):
            self.power_status = "helper"
            for name, watts in sorted(helper.snapshot().items()):
                rows.append((f"power.{name}", f"{name.title()} Power", watts, "W", 1))
            if not rows:
                rows.append(("power_hint", "CPU Power", "waiting for admin authentication...", "", None))
        elif unreadable:
            self.power_status = "needs_admin"
            reason = {"denied": "authentication cancelled - click 'Enable CPU Power' to retry",
                      "stopped": "admin helper stopped - click 'Enable CPU Power' to retry",
                      "failed": "could not start admin helper (is polkit/pkexec installed?)"}.get(
                          helper.state, "needs admin access - click 'Enable CPU Power' in the toolbar")
            rows.append(("power_hint", "CPU Power", reason, "", None))
        else:
            self.power_status = "unsupported"
            rows.append(("power_hint", "CPU Power",
                         "not exposed by this CPU/kernel - see Device Details > CPU > Power sensors", "", None))
        return rows

    def collect_memory(self):
        rows = []
        if psutil is None:
            return rows
        try:
            vm = psutil.virtual_memory()
            rows.append(("used_pct", "Memory Load", vm.percent, "%", 0))
            rows.append(("used", "Memory Used", vm.used / 1024**3, "GB", 2))
            rows.append(("avail", "Memory Available", vm.available / 1024**3, "GB", 2))
            rows.append(("total", "Memory Total", vm.total / 1024**3, "GB", 2))
        except Exception:
            pass
        try:
            sm = psutil.swap_memory()
            if sm.total > 0:
                rows.append(("swap_pct", "Swap Load", sm.percent, "%", 0))
                rows.append(("swap_used", "Swap Used", sm.used / 1024**3, "GB", 2))
        except Exception:
            pass
        return rows

    # ---- Graphics -----------------------------------------------------------

    NVIDIA_FIELDS = ["pci.bus_id", "utilization.gpu", "utilization.memory", "memory.used",
                     "memory.total", "temperature.gpu", "power.draw", "fan.speed",
                     "clocks.sm", "clocks.mem"]

    def collect_gpu_device(self, gpu):
        """Live sensors for one graphics device (see get_gpus())."""
        path = gpu["path"]
        if gpu.get("devfreq"):
            rows = self._soc_gpu_rows(path)
        elif read_sysfs(os.path.join(path, "power", "runtime_status")) == "suspended":
            # Reading a sleeping GPU's sensors wakes it up, which would defeat
            # the power saving on hybrid-graphics laptops. Leave it alone.
            return [("state", "Status", "Suspended (power saving)", "", None),
                    ("load", "GPU Usage", 0, "%", 0),
                    ("power", "GPU Power", 0.0, "W", 1)]
        elif gpu["vendor"] == "0x10de":
            rows = self._nvidia_rows(gpu)
        elif gpu["vendor"] == "0x1002":
            rows = self._amd_rows(gpu)
        elif gpu["vendor"] == "0x8086":
            rows = self._intel_rows(gpu)
        else:
            rows = self._gpu_hwmon_rows(gpu)
        if not rows:
            rows.append(("note", "Status", "No live sensors exposed for this device", "", None))
        return rows

    def _gpu_hwmon_rows(self, gpu):
        """Temperature / power / fan / clock rows from the GPU's hwmon node.
        Used by every vendor, since amdgpu, nouveau, xe and i915 all publish
        what they have there in the same format."""
        rows = []

        def val(hwmon, name):
            v = read_sysfs(os.path.join(hwmon, name))
            return int(v) if v and v.lstrip("-").isdigit() else None

        for hwmon in sorted(glob.glob(os.path.join(gpu["path"], "hwmon", "hwmon*"))):
            for idx in (1, 2, 3):
                t = val(hwmon, f"temp{idx}_input")
                if t is None or not -50_000 < t < 150_000:
                    continue
                label = (read_sysfs(os.path.join(hwmon, f"temp{idx}_label")) or "").lower()
                if idx == 1 or label == "edge":
                    rows.append(("temp", "GPU Temperature", t / 1000, "°C", 1))
                elif label == "junction":
                    rows.append(("temp_junction", "GPU Hot Spot Temperature", t / 1000, "°C", 1))
                elif label == "mem":
                    rows.append(("temp_mem", "GPU Memory Temperature", t / 1000, "°C", 1))
                else:
                    rows.append((f"temp{idx}", f"GPU Temperature {idx}", t / 1000, "°C", 1))

            power = val(hwmon, "power1_average")
            if power is None:
                power = val(hwmon, "power1_input")  # some AMD generations only provide this one
            if power is not None and 0 <= power / 1_000_000 <= 1000:
                rows.append(("power", "GPU Power", power / 1_000_000, "W", 1))
            else:
                energy = val(hwmon, "energy1_input")  # cumulative microjoules (Intel Arc / xe)
                if energy is not None:
                    now = time.time()
                    prev = self.gpu_prev.get(("energy", gpu["addr"]))
                    self.gpu_prev[("energy", gpu["addr"])] = (energy, now)
                    if prev and now > prev[1] and energy >= prev[0]:
                        rows.append(("power", "GPU Power", (energy - prev[0]) / 1_000_000 / (now - prev[1]), "W", 1))

            fan = val(hwmon, "fan1_input")
            if fan is not None:
                rows.append(("fan", "GPU Fan Speed", fan, "RPM", 0))
            for idx, sid, label in ((1, "clk_core", "GPU Core Clock"), (2, "clk_mem", "GPU Memory Clock")):
                hz = val(hwmon, f"freq{idx}_input")
                if hz:
                    rows.append((sid, label, hz / 1_000_000, "MHz", 0))
        return rows

    def _nvidia_all(self, query, fmt_arg):
        """One nvidia-smi call covering every GPU, cached for under a second
        so a multi-GPU system doesn't spawn one process per GPU per tick."""
        if self._nv_all and time.time() - self._nv_all[0] < 0.6:
            return self._nv_all[1]
        out = run(["nvidia-smi", query, fmt_arg], timeout=3)
        lines = out.strip().splitlines() if out and out.strip() else []
        self._nv_all = (time.time(), lines)
        return lines

    def _nvidia_query(self, gpu):
        query = "--query-gpu=" + ",".join(self.NVIDIA_FIELDS)
        fmt_arg = "--format=csv,noheader,nounits"
        lines = []
        if self._nvsmi_id_ok is not False:
            # Ask about just this GPU, so other (possibly sleeping) ones aren't woken
            out = run(["nvidia-smi", "-i", gpu["addr"], query, fmt_arg], timeout=3)
            if out and out.strip():
                self._nvsmi_id_ok = True
                lines = out.strip().splitlines()
            elif self._nvsmi_id_ok is None:
                self._nvsmi_id_ok = False
        if not lines:
            everything = self._nvidia_all(query, fmt_arg)
            tail = gpu["addr"].lower()[-7:]
            lines = [l for l in everything if l.split(",")[0].strip().lower()[-7:] == tail]
            if not lines and len(everything) == 1:
                lines = everything
        for line in lines[:1]:
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= len(self.NVIDIA_FIELDS):
                return parts
        return None

    def _nvidia_rows(self, gpu):
        if not shutil.which("nvidia-smi"):
            rows = self._gpu_hwmon_rows(gpu)  # nouveau exposes a little through hwmon
            if not rows and gpu.get("driver") != "nouveau":
                rows.append(("hint", "NVIDIA tools", f"nvidia-smi not found: {install_cmd(pacman='nvidia-utils')}", "", None))
            return rows
        parts = self._nvidia_query(gpu)
        if not parts:
            return self._gpu_hwmon_rows(gpu)
        _, u_gpu, u_mem, mem_used, mem_total, temp, power, fan, clk_sm, clk_mem = parts[:10]
        return [
            ("load", "GPU Usage", _num(u_gpu), "%", 0),
            ("memload", "GPU Memory Usage", _num(u_mem), "%", 0),
            ("memused", "GPU Memory Used", _num(mem_used), "MB", 0),
            ("memtotal", "GPU Memory Total", _num(mem_total), "MB", 0),
            ("temp", "GPU Temperature", _num(temp), "°C", 1),
            ("power", "GPU Power", _num(power), "W", 1),
            ("fan", "GPU Fan Speed", _num(fan), "%", 0),  # laptops report N/A: skipped, not an error
            ("clk_core", "GPU Core Clock", _num(clk_sm), "MHz", 0),
            ("clk_mem", "GPU Memory Clock", _num(clk_mem), "MHz", 0),
        ]

    def _amd_rows(self, gpu):
        dev = gpu["path"]
        rows = []
        busy = read_sysfs(os.path.join(dev, "gpu_busy_percent"))
        if busy is not None:
            rows.append(("load", "GPU Usage", _num(busy), "%", 0))
        for sid, label, name in (("memused", "GPU Memory Used", "mem_info_vram_used"),
                                 ("memtotal", "GPU Memory Total", "mem_info_vram_total")):
            v = read_sysfs(os.path.join(dev, name))
            if v and v.isdigit():
                rows.append((sid, label, int(v) / 1024**2, "MB", 0))
        rows.extend(self._gpu_hwmon_rows(gpu))
        # Older kernels have no freq*_input: fall back to the DPM level table
        for sid, label, name in (("clk_core", "GPU Core Clock", "pp_dpm_sclk"),
                                 ("clk_mem", "GPU Memory Clock", "pp_dpm_mclk")):
            if not any(r[0] == sid for r in rows):
                current, _ = _dpm_levels(os.path.join(dev, name))
                m = re.search(r"\d+", current or "")
                if m:
                    rows.append((sid, label, int(m.group()), "MHz", 0))
        return rows

    def _intel_rows(self, gpu):
        """Intel's kernel drivers (i915 and xe) don't publish a load percentage
        to ordinary users, so we report the clock plus 'activity', worked out
        from how much of the time the GPU was NOT in its RC6 idle state."""
        rows = []
        card, dev = gpu.get("card"), gpu["path"]
        if card:
            mhz = _first_int([
                card + "/gt_act_freq_mhz", card + "/gt/gt0/rps_act_freq_mhz", dev + "/tile0/gt0/freq0/act_freq",
                card + "/gt_cur_freq_mhz", card + "/gt/gt0/rps_cur_freq_mhz", dev + "/tile0/gt0/freq0/cur_freq"])
            if mhz is not None:
                rows.append(("clk_core", "GPU Core Clock", mhz, "MHz", 0))
            idle_ms = _first_int([
                card + "/gt/gt0/rc6_residency_ms", card + "/power/rc6_residency_ms",
                dev + "/tile0/gt0/gtidle/idle_residency_ms"])
            if idle_ms is not None:
                now = time.time()
                prev = self.gpu_prev.get(("rc6", gpu["addr"]))
                self.gpu_prev[("rc6", gpu["addr"])] = (idle_ms, now)
                if prev and now > prev[1]:
                    idle_pct = max(0.0, idle_ms - prev[0]) / ((now - prev[1]) * 1000) * 100
                    rows.append(("activity", "GPU Activity (non-idle time)", min(100.0, max(0.0, 100 - idle_pct)), "%", 0))
        rows.extend(self._gpu_hwmon_rows(gpu))
        return rows

    def _soc_gpu_rows(self, path):
        rows = []
        hz = read_sysfs(os.path.join(path, "cur_freq"))
        if hz and hz.isdigit():
            rows.append(("clk_core", "GPU Core Clock", int(hz) / 1_000_000, "MHz", 0))
        m = re.match(r"\d+", read_sysfs(os.path.join(path, "load")) or "")
        if m:
            rows.append(("load", "GPU Usage", int(m.group()), "%", 0))
        return rows

    def collect_lmsensors(self):
        rows = []
        if not shutil.which("sensors"):
            cmd = install_cmd(pacman="lm_sensors", apt="lm-sensors", dnf="lm_sensors")
            rows.append(("hint", "Setup needed", f"{cmd} && sudo sensors-detect", "", None))
            return rows
        out = run(["sensors", "-j"], timeout=3)
        if not out:
            return rows
        try:
            data = json.loads(out)
        except Exception:
            return rows

        skip = ("coretemp", "k10temp", "zenpower", "amd_energy", "nvme", "drivetemp",
                "amdgpu", "nouveau", "radeon", "i915", "xe-pci")
        for chip, features in data.items():
            if any(s in chip.lower() for s in skip) or not isinstance(features, dict):
                continue
            for feat_name, feat_val in features.items():
                if feat_name == "Adapter" or not isinstance(feat_val, dict):
                    continue
                for sub_key, value in feat_val.items():
                    if not sub_key.endswith("_input") or not isinstance(value, (int, float)):
                        continue
                    base = sub_key[:-6]
                    if base.startswith("temp"):
                        unit, dec = "°C", 1
                    elif base.startswith("in"):
                        unit, dec = "V", 2
                    elif base.startswith("fan"):
                        unit, dec = "RPM", 0
                    elif base.startswith("power"):
                        unit, dec = "W", 1
                    elif base.startswith("curr"):
                        unit, dec = "A", 2
                    else:
                        unit, dec = "", 2
                    rows.append((f"{chip}.{feat_name}.{sub_key}", feat_name, value, unit, dec))
        if not rows:
            rows.append(("hint", "No chips detected", "run: sudo sensors-detect", "", None))
        return rows

    def _drive_activity_rows(self, dev, label, io):
        """Live read / write / total transfer speed and busy % for one drive,
        from the kernel's cumulative I/O counters (the same ones iostat
        uses). Speeds are the change since the previous sample, so the first
        sample after startup produces no rows. MB/s here is decimal
        (1 MB = 1,000,000 bytes), matching dd, drive spec sheets and
        CrystalDiskMark/HWiNFO."""
        rows = []
        cur = io.get(dev)
        if cur is None:
            return rows
        now = time.time()
        prev = self.disk_prev.get(dev)
        self.disk_prev[dev] = (cur, now)
        if not prev:
            return rows
        old, then = prev
        dt = now - then
        if dt <= 0:
            return rows
        read = max(0, cur.read_bytes - old.read_bytes) / dt / 1_000_000
        write = max(0, cur.write_bytes - old.write_bytes) / dt / 1_000_000
        rows.append((f"{dev}.read_speed", f"{label} Read Speed", read, "MB/s", 1))
        rows.append((f"{dev}.write_speed", f"{label} Write Speed", write, "MB/s", 1))
        rows.append((f"{dev}.total_speed", f"{label} Total Transfer Speed", read + write, "MB/s", 1))
        busy_now = getattr(cur, "busy_time", None)
        busy_old = getattr(old, "busy_time", None)
        if busy_now is not None and busy_old is not None:
            busy = min(100.0, max(0.0, (busy_now - busy_old) / (dt * 1000) * 100))
            rows.append((f"{dev}.activity", f"{label} Activity (busy time)", busy, "%", 0))
        return rows

    def _drivetemp(self, dev):
        """Temperature of a SATA/SAS drive from the kernel's drivetemp hwmon
        driver, matched to the drive by walking hwmon -> device -> block/<dev>."""
        for hwmon in glob.glob(f"{SYS_CLASS_HWMON}/hwmon*"):
            if read_sysfs(hwmon + "/name") != "drivetemp":
                continue
            if os.path.isdir(os.path.join(os.path.realpath(hwmon + "/device"), "block", dev)):
                t = read_sysfs(hwmon + "/temp1_input")
                if t and t.lstrip("-").isdigit():
                    return int(t) / 1000
        return None

    def collect_storage_group(self, drive_names=None):
        rows = []
        if psutil is None or not drive_names:
            return rows
        try:
            io = psutil.disk_io_counters(perdisk=True) or {}
        except Exception:
            io = {}

        mounted_devs = set()
        try:
            for part in psutil.disk_partitions(all=False):
                if part.fstype in ("squashfs", "tmpfs", "devtmpfs", "overlay"):
                    continue
                # Follow partitions, LVM, LUKS and md RAID down to the physical
                # disk, so usage shows up under the drive it really lives on.
                base_dev = next((d for d in base_disks_for_device(part.device)[:1] if d in drive_names), None)
                if base_dev:
                    mounted_devs.add(base_dev)
                    try:
                        usage = psutil.disk_usage(part.mountpoint)
                        label_name = part.mountpoint
                        tag = re.sub(r"\W+", "_", part.mountpoint) or part.device
                        rows.append((f"{tag}.pct", f"{label_name} Usage", usage.percent, "%", 0))
                        rows.append((f"{tag}.used", f"{label_name} Used", usage.used / 1024**3, "GB", 1))
                        rows.append((f"{tag}.free", f"{label_name} Free", usage.free / 1024**3, "GB", 1))
                    except Exception:
                        pass
        except Exception:
            pass

        for i, dev in enumerate(drive_names):
            label = f"Drive {i+1} ({dev})"
            if dev not in mounted_devs:
                try:
                    size_str = read_sysfs(f"{SYS_BLOCK}/{dev}/size")
                    if size_str:
                        rows.append((f"{dev}.cap", f"{label} Total Capacity", int(size_str) * 512 / 1024**3, "GB", 1))
                except Exception:
                    pass

            try:
                if dev.startswith("nvme"):
                    nvme_ctrl = re.sub(r"n\d+$", "", dev)
                    for hwmon in glob.glob(f"{SYS_CLASS_NVME}/{nvme_ctrl}/hwmon*"):
                        t = read_sysfs(os.path.join(hwmon, "temp1_input"))
                        if t is not None:
                            rows.append((f"{dev}.temp", f"{label} Temperature", int(t) / 1000, "°C", 1))
                else:
                    t = self._drivetemp(dev)
                    if t is not None:
                        rows.append((f"{dev}.temp", f"{label} Temperature", t, "°C", 1))
                    elif (read_sysfs(f"{SYS_BLOCK}/{dev}/device/vendor") or "").strip() == "ATA" \
                            and not os.path.isdir("/sys/module/drivetemp"):
                        # SATA drive, and the kernel module that exposes its
                        # temperature without root isn't loaded
                        rows.append((f"{dev}.temp_hint", f"{label} Temperature",
                                     "unavailable - run: sudo modprobe drivetemp", "", None))
            except Exception:
                pass

            try:
                rows.extend(self._drive_activity_rows(dev, label, io))
            except Exception:
                pass

        return rows

    def collect_network(self):
        rows = []
        if psutil is None:
            return rows
        try:
            counters = psutil.net_io_counters(pernic=True)
        except Exception:
            return rows
        now = time.time()
        if self.net_prev and self.net_prev_time:
            dt = now - self.net_prev_time
            if dt > 0:
                for nic, c in counters.items():
                    if nic == "lo" or nic not in self.net_prev:
                        continue
                    p = self.net_prev[nic]
                    # Megabits per second (1 Mbit = 1,000,000 bits), the networking
                    # convention used by ISPs and link speeds.
                    up = max(0, c.bytes_sent - p.bytes_sent) * 8 / dt / 1_000_000
                    down = max(0, c.bytes_recv - p.bytes_recv) * 8 / dt / 1_000_000
                    rows.append((f"{nic}.up", f"{nic} Upload", up, "Mbit/s", 2))
                    rows.append((f"{nic}.down", f"{nic} Download", down, "Mbit/s", 2))
        self.net_prev = counters
        self.net_prev_time = now
        return rows

    def collect_battery(self):
        rows = []
        if psutil is None:
            return rows
        try:
            batt = psutil.sensors_battery()
            if batt is not None:
                rows.append(("pct", "Battery Charge", batt.percent, "%", 0))
                status = "Charging" if batt.power_plugged else "Discharging"
                rows.append(("status", "Battery Status", status, "", None))
        except Exception:
            pass
        return rows


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


COLUMN_TITLES = ["Sensor", "Current", "Minimum", "Maximum", "Average"]
# --------------------------------------------------------------------------
# Theming - reads straight from Qt's live QPalette, which Plasma's platform
# theme plugin keeps in sync with your color scheme and accent color. No
# manual config-file parsing needed; this is what every native Qt/KDE app
# does. We only pick fixed warning/critical colors ourselves, since Qt's
# base QPalette doesn't carry semantic "this means trouble" colors the way
# KDE's own extended color scheme does - and we tune those two by whether
# the current theme is dark or light, so they still read clearly either way.
# --------------------------------------------------------------------------

def theme_colors(palette: "QtGui.QPalette") -> dict:
    window = palette.color(QtGui.QPalette.ColorRole.Window)
    is_dark = window.lightness() < 128
    if is_dark:
        warn, crit = QtGui.QColor("#ffb86c"), QtGui.QColor("#ff6b6b")
    else:
        warn, crit = QtGui.QColor("#a8580f"), QtGui.QColor("#c62828")
    return {
        "window": window,
        "text": palette.color(QtGui.QPalette.ColorRole.WindowText),
        "muted": palette.color(QtGui.QPalette.ColorGroup.Disabled, QtGui.QPalette.ColorRole.Text),
        "grid": palette.color(QtGui.QPalette.ColorRole.Mid),
        "accent": palette.color(QtGui.QPalette.ColorRole.Highlight),
        "warn": warn,
        "crit": crit,
    }


def monospace_font(point_size=10):
    font = QtGui.QFont("monospace")
    font.setStyleHint(QtGui.QFont.StyleHint.Monospace)
    font.setPointSize(point_size)
    return font


COLUMN_TITLES_QT = COLUMN_TITLES  # ["Sensor", "Current", "Minimum", "Maximum", "Average"]


# --------------------------------------------------------------------------
# Custom Graph Widget
# --------------------------------------------------------------------------

def fmt_ago(seconds):
    """Axis label for 'N seconds ago': -45s, -60s, -1m 15s, -2m ..."""
    seconds = int(round(seconds))
    if seconds == 0:
        return "0s"
    if seconds <= 60:
        return f"-{seconds}s"
    m, sec = divmod(seconds, 60)
    return f"-{m}m" if sec == 0 else f"-{m}m {sec:02d}s"


def fmt_dur(seconds):
    """Plain duration: 45s, 2m, 2m 30s."""
    seconds = int(round(seconds))
    m, sec = divmod(seconds, 60)
    if m == 0:
        return f"{sec}s"
    return f"{m}m" if sec == 0 else f"{m}m {sec:02d}s"


class LiveGraph(QtWidgets.QWidget):
    # The time axis has a fixed scale: a default-sized pop-out window (520 px
    # wide, so a 452 px plot area) shows 60 seconds, and every further 452 px
    # of width reveals another 60 seconds. Shrinking the window shows less.
    #
    # The graph is also scrollable through all kept history (30 minutes):
    # mouse wheel, the scrollbar under the graph, or a horizontal wheel.
    DEFAULT_SPAN_SECONDS = 60
    DEFAULT_PLOT_WIDTH = 452
    TICK_SECONDS = 15
    PAD_TOP, PAD_BOTTOM, PAD_LEFT, PAD_RIGHT = 32, 32, 52, 16

    # True: wheel up scrolls the view left (back in time), wheel down scrolls
    # right (toward live). Set to False to flip the direction.
    WHEEL_UP_SCROLLS_LEFT = True

    view_changed = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.sensor_id = None
        self.label = ""
        self.unit = ""
        self.history = []
        # None = live: the right edge follows the newest sample. Otherwise an
        # absolute timestamp the right edge is pinned to, so the picture
        # stays still while you look back through history.
        self.view_end = None
        self.setMinimumHeight(170)
        self.setMinimumWidth(240)

    def set_sensor(self, sensor_id, label, unit, history):
        self.sensor_id = sensor_id
        self.label = label
        self.unit = unit
        self.history = history
        self.view_end = None
        self.update()

    def changeEvent(self, event):
        # Fires when Plasma's theme changes and Qt updates our palette.
        if event.type() == QtCore.QEvent.Type.PaletteChange:
            self.update()
        super().changeEvent(event)

    # -- time window / scrolling --------------------------------------------

    def span_seconds(self):
        graph_w = max(20, self.width() - self.PAD_LEFT - self.PAD_RIGHT)
        return graph_w * self.DEFAULT_SPAN_SECONDS / self.DEFAULT_PLOT_WIDTH

    @staticmethod
    def wheel_step(span):
        """Seconds moved per wheel notch."""
        return max(5.0, span / 4)

    def _bounds(self):
        if not self.history:
            return None
        return self.history[0][0], self.history[-1][0]

    def _resolve_view_end(self, t0, t_now, span):
        """Timestamp at the graph's right edge, kept within the real data."""
        if self.view_end is None:
            return t_now
        end = max(min(t_now, t0 + span), min(self.view_end, t_now))
        if end >= t_now - 1e-6:
            self.view_end = None
            return t_now
        self.view_end = end
        return end

    def scroll_info(self):
        """Scrollbar numbers in tenths of a second, or None without data."""
        bounds = self._bounds()
        if bounds is None:
            return None
        t0, t_now = bounds
        span = self.span_seconds()
        t_ref = self._resolve_view_end(t0, t_now, span)
        max_back = max(0.0, (t_now - t0) - span)
        left = min(max(0.0, (t_ref - span) - t0), max_back)
        return {
            "value": round(left * 10),
            "maximum": round(max_back * 10),
            "page": max(1, round(span * 10)),
            "step": max(1, round(self.wheel_step(span) * 10)),
        }

    def scroll_by(self, delta_seconds):
        """Moves the view; negative = toward older data, positive = toward live."""
        bounds = self._bounds()
        if bounds is None:
            return
        t0, t_now = bounds
        span = self.span_seconds()
        current = self._resolve_view_end(t0, t_now, span)
        self.view_end = current + delta_seconds
        self._resolve_view_end(t0, t_now, span)
        self.update()
        self.view_changed.emit()

    def set_left_offset(self, offset_seconds):
        """Puts the view's left edge `offset_seconds` after the oldest sample
        (used by the scrollbar). The far-right position means live."""
        bounds = self._bounds()
        if bounds is None:
            return
        t0, t_now = bounds
        span = self.span_seconds()
        max_back = max(0.0, (t_now - t0) - span)
        self.view_end = None if offset_seconds >= max_back - 0.05 else t0 + offset_seconds + span
        self.update()

    def go_live(self):
        self.view_end = None
        self.update()
        self.view_changed.emit()

    def wheelEvent(self, event):
        angle = event.angleDelta()
        delta = angle.y() or angle.x()
        if not delta:
            event.ignore()
            return
        notches = delta / 120.0
        if not self.WHEEL_UP_SCROLLS_LEFT:
            notches = -notches
        # wheel up (positive) -> scroll left -> older -> negative time delta
        self.scroll_by(-notches * self.wheel_step(self.span_seconds()))
        event.accept()

    def mouseDoubleClickEvent(self, event):
        self.go_live()
        super().mouseDoubleClickEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.view_changed.emit()  # span changed, so the scrollbar's page size did too

    # -- painting -----------------------------------------------------------

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        colors = theme_colors(self.palette())

        painter.fillRect(0, 0, w, h, colors["window"])

        body_font = monospace_font(10)
        painter.setFont(body_font)

        if not self.sensor_id or not self.history:
            painter.setPen(colors["muted"])
            painter.drawText(15, h // 2, "Double-click a numeric sensor row to plot its history...")
            painter.end()
            return

        padding_top, padding_bottom = self.PAD_TOP, self.PAD_BOTTOM
        padding_left, padding_right = self.PAD_LEFT, self.PAD_RIGHT
        graph_w = max(20, w - padding_left - padding_right)
        graph_h = max(20, h - padding_top - padding_bottom)
        right = padding_left + graph_w

        # Time axis: constant pixels-per-second, so the visible span follows
        # the window width (default size = 60 s, +60 s per extra 452 px).
        px_per_sec = self.DEFAULT_PLOT_WIDTH / self.DEFAULT_SPAN_SECONDS
        span = self.span_seconds()

        samples = list(self.history)  # [(timestamp, value), ...] oldest first
        t0, t_now = samples[0][0], samples[-1][0]
        t_ref = self._resolve_view_end(t0, t_now, span)  # timestamp at the right edge
        live = self.view_end is None
        t_left = t_ref - span
        visible = [v for t, v in samples if t_left <= t <= t_ref] or [samples[-1][1]]
        mn, mx = min(visible), max(visible)
        if mx == mn:
            mx += 1.0

        title = f"Graphing: {self.label} ({self.unit})"
        painter.setPen(colors["text"])
        painter.drawText(10, 20, title)
        if live:
            span_text = f"showing last {span:.0f} s" if span < 120 else f"showing last {span / 60:.1f} min"
            note_color = colors["muted"]
        else:
            span_text = f"history: ends {fmt_dur(t_now - t_ref)} ago"
            note_color = colors["warn"]
        metrics_body = QtGui.QFontMetrics(body_font)
        if metrics_body.horizontalAdvance(title) + metrics_body.horizontalAdvance(span_text) + 40 < w:
            painter.setPen(note_color)
            painter.drawText(w - 10 - metrics_body.horizontalAdvance(span_text), 20, span_text)

        axis_pen = QtGui.QPen(colors["muted"])
        axis_pen.setWidthF(1.0)
        painter.setPen(axis_pen)
        painter.drawLine(padding_left, padding_top, padding_left, padding_top + graph_h)
        painter.drawLine(padding_left, padding_top + graph_h, right, padding_top + graph_h)

        small_font = monospace_font(8)
        painter.setFont(small_font)
        metrics = QtGui.QFontMetrics(small_font)

        y_steps = 4
        for i in range(y_steps + 1):
            val = mn + (mx - mn) * (i / y_steps)
            y = padding_top + graph_h - (graph_h / y_steps) * i
            if 0 < i < y_steps:
                grid_pen = QtGui.QPen(colors["grid"])
                grid_pen.setWidthF(0.5)
                painter.setPen(grid_pen)
                painter.drawLine(padding_left, round(y), right, round(y))
            painter.setPen(axis_pen)
            painter.drawLine(padding_left - 4, round(y), padding_left, round(y))
            text = f"{val:.1f}"
            tw = metrics.horizontalAdvance(text)
            painter.drawText(padding_left - 8 - tw, round(y) + metrics.ascent() // 2, text)

        # X ticks every TICK_SECONDS. Live: "N seconds ago", counted back from
        # the right edge. Looking at history: fixed clock times, so the ticks
        # stay glued to the data while you scroll.
        tick = self.TICK_SECONDS
        ticks = []
        if live:
            for k in range(int(span / tick + 1e-6) + 1):
                ticks.append((right - k * tick * px_per_sec, fmt_ago(k * tick)))
        else:
            tt = -(-t_left // tick) * tick
            while tt <= t_ref + 1e-6:
                ticks.append((right - (t_ref - tt) * px_per_sec, time.strftime("%H:%M:%S", time.localtime(tt))))
                tt += tick
        for x, text in ticks:
            if x < padding_left - 0.5 or x > right + 0.5:
                continue
            if padding_left + 1 < x < right - 1:
                grid_pen = QtGui.QPen(colors["grid"])
                grid_pen.setWidthF(0.5)
                painter.setPen(grid_pen)
                painter.drawLine(round(x), padding_top, round(x), padding_top + graph_h)
            painter.setPen(axis_pen)
            painter.drawLine(round(x), padding_top + graph_h, round(x), padding_top + graph_h + 4)
            tw = metrics.horizontalAdvance(text)
            label_x = min(max(2, round(x - tw / 2)), w - tw - 2)
            painter.drawText(label_x, padding_top + graph_h + 18, text)

        # Data line: start one sample before the left edge and end one sample
        # after the right edge so it enters and leaves the plot continuously;
        # everything is clipped to the plot area.
        start = len(samples) - 1
        for idx, (t, _) in enumerate(samples):
            if t >= t_left:
                start = max(0, idx - 1)
                break
        end = len(samples)
        for idx in range(start, len(samples)):
            if samples[idx][0] > t_ref:
                end = idx + 1
                break
        points = []
        for t, v in samples[start:end]:
            x = right - (t_ref - t) * px_per_sec
            y = padding_top + graph_h - ((v - mn) / (mx - mn)) * graph_h
            points.append(QtCore.QPointF(x, y))
        painter.setClipRect(QtCore.QRectF(padding_left, padding_top - 2, graph_w, graph_h + 4))
        line_pen = QtGui.QPen(colors["accent"])
        line_pen.setWidthF(1.6)
        painter.setPen(line_pen)
        if len(points) == 1:
            painter.setBrush(QtGui.QBrush(colors["accent"]))
            painter.drawEllipse(points[0], 2.5, 2.5)
        else:
            path = QtGui.QPainterPath(points[0])
            for p in points[1:]:
                path.lineTo(p)
            painter.drawPath(path)
        painter.setClipping(False)
        painter.end()


class GraphWindow(QtWidgets.QWidget):
    def __init__(self, sensor_id, label, unit, history, on_close=None):
        super().__init__()
        self.sensor_id = sensor_id
        self._on_close = on_close
        self.setWindowTitle(label)
        self.setWindowIcon(QtGui.QIcon.fromTheme("utilities-system-monitor"))
        self.resize(520, 300)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.graph = LiveGraph()
        self.graph.set_sensor(sensor_id, label, unit, history)
        layout.addWidget(self.graph, 1)

        self.scrollbar = QtWidgets.QScrollBar(Qt.Orientation.Horizontal)
        self.scrollbar.setToolTip(
            f"Scroll through the last {HISTORY_SECONDS // 60} minutes. Mouse wheel works too;\n"
            "scroll down to the end, or double-click the graph, to return to live.")
        self.scrollbar.valueChanged.connect(self._on_scrollbar)
        layout.addWidget(self.scrollbar)
        self.graph.view_changed.connect(self._sync_scrollbar)
        self._sync_scrollbar()
        self.show()

    def _sync_scrollbar(self):
        info = self.graph.scroll_info()
        sb = self.scrollbar
        sb.blockSignals(True)
        if info is None:
            sb.setRange(0, 0)
        else:
            sb.setRange(0, info["maximum"])
            sb.setPageStep(info["page"])
            sb.setSingleStep(info["step"])
            sb.setValue(info["value"])
        sb.setEnabled(bool(info and info["maximum"] > 0))
        sb.blockSignals(False)

    def _on_scrollbar(self, value):
        self.graph.set_left_offset(value / 10.0)

    def refresh(self, history):
        self.graph.history = history
        self.graph.update()
        self._sync_scrollbar()

    def closeEvent(self, event):
        if self._on_close:
            self._on_close(self.sensor_id)
        super().closeEvent(event)


# --------------------------------------------------------------------------
# Background data bridge (worker thread -> Qt main thread, via a signal)
# --------------------------------------------------------------------------

class DataBridge(QtCore.QObject):
    data_ready = QtCore.Signal(object)  # object: keeps category order (dict would be re-sorted by Qt)


# --------------------------------------------------------------------------
# Device details - the HWiNFO-style "what exactly IS this hardware" view.
# Each collector returns a list of groups: [(group_title, [(key, value)])].
# Everything here is read defensively; a missing tool or sysfs file just
# means that row (or group) is left out.
# --------------------------------------------------------------------------

def run_c(cmd, timeout=4):
    """Like run(), but forces the C locale so tool output is parseable."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            env={**os.environ, "LC_ALL": "C"},
        )
        if result.returncode == 0:
            return result.stdout
    except Exception:
        pass
    return None


def human_bytes(n, decimal=False):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "-"
    base = 1000.0 if decimal else 1024.0
    units = ("B", "KB", "MB", "GB", "TB", "PB") if decimal else ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    for unit in units:
        if abs(n) < base or unit == units[-1]:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= base


def cap_str(n):
    return f"{human_bytes(n, True)} ({human_bytes(n)})"


def _add(rows, key, value):
    if value is None:
        return
    value = str(value).strip()
    if value:
        rows.append((key, value))


def _kv_lines(text, sep=":"):
    out = {}
    for line in (text or "").splitlines():
        if sep in line:
            key, _, value = line.partition(sep)
            out[key.strip()] = value.strip()
    return out


def _dig(data, *keys):
    for key in keys:
        if not isinstance(data, dict) or key not in data:
            return None
        data = data[key]
    return data


def _meminfo():
    info = {}
    for line in (read_sysfs("/proc/meminfo") or "").splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if not parts:
            continue
        try:
            value = int(parts[0])
        except ValueError:
            continue
        info[key.strip()] = value * 1024 if len(parts) > 1 and parts[1] == "kB" else value
    return info


# -- privileged (admin) data: dmidecode + SMART --------------------------------

# Fixed command string, run through /bin/sh by pkexec, so the admin prompt
# appears once and no user-writable script is ever run as root.
PRIV_SCRIPT = (
    'echo "@@@DMIDECODE"; dmidecode 2>/dev/null; '
    'for d in /dev/sd? /dev/nvme?n1; do [ -e "$d" ] || continue; '
    'echo "@@@SMART $d"; smartctl -a -j "$d" 2>/dev/null; done; true'
)


def parse_dmidecode(text):
    """Turns dmidecode output into [(block_title, [(key, value)])]."""
    blocks = []
    rows = None
    expect_title = False
    for line in text.splitlines():
        if line.startswith("Handle "):
            expect_title = True
            rows = None
            continue
        if not line.strip():
            continue
        if expect_title and not line[0].isspace():
            rows = []
            blocks.append((line.strip(), rows))
            expect_title = False
            continue
        if rows is None:
            continue
        if line.startswith("\t\t"):
            if rows:
                key, value = rows[-1]
                item = line.strip()
                rows[-1] = (key, f"{value}, {item}" if value else item)
        elif line.startswith("\t"):
            key, _, value = line.strip().partition(":")
            rows.append((key.strip(), value.strip()))
    return blocks


def fetch_privileged():
    """Runs dmidecode + smartctl as root via polkit (graphical password
    prompt). Returns (data_or_None, error_message)."""
    missing = [t for t in ("dmidecode", "smartctl") if not shutil.which(t)]
    pkexec = shutil.which("pkexec")
    if not pkexec:
        return None, "pkexec (polkit) was not found, so admin-only details can't be loaded."
    try:
        result = subprocess.run(
            [pkexec, "/bin/sh", "-c", PRIV_SCRIPT],
            capture_output=True, text=True, timeout=180,
        )
    except Exception as exc:
        return None, f"Could not run pkexec: {exc}"
    if result.returncode in (126, 127):
        return None, "Authentication was cancelled or denied."

    sections = {}
    current = None
    for line in result.stdout.splitlines():
        if line.startswith("@@@"):
            current = line[3:].strip()
            sections[current] = []
        elif current is not None:
            sections[current].append(line)

    data = {"dmi": [], "smart": {}, "missing": missing}
    if "DMIDECODE" in sections:
        data["dmi"] = parse_dmidecode("\n".join(sections["DMIDECODE"]))
    for name, lines in sections.items():
        if name.startswith("SMART "):
            try:
                data["smart"][name.split(" ", 1)[1]] = json.loads("\n".join(lines))
            except ValueError:
                pass
    return data, ""


def _priv_hint(priv, what):
    """A group telling the user how to unlock admin-only details."""
    rows = []
    if priv is None:
        _add(rows, "Not loaded", f"Click 'Load extended details...' (asks for your admin password) to read {what}.")
    elif priv.get("missing"):
        tools = " ".join(priv["missing"])
        _add(rows, "Missing tools", f"{tools} not installed: {install_cmd(pacman=tools, apt=tools, dnf=tools)}")
    return [("Extended details", rows)] if rows else []


def _dmi_blocks(priv, title):
    return [rows for t, rows in (priv or {}).get("dmi", []) if t == title]


def _pick(rows, keys):
    wanted = dict(rows)
    return [(k, wanted[k]) for k in keys if wanted.get(k) not in (None, "", "Not Specified", "Unknown")]


# -- System --------------------------------------------------------------------

CHASSIS_TYPES = {
    1: "Other", 2: "Unknown", 3: "Desktop", 4: "Low Profile Desktop", 5: "Pizza Box",
    6: "Mini Tower", 7: "Tower", 8: "Portable", 9: "Laptop", 10: "Notebook",
    11: "Handheld", 12: "Docking Station", 13: "All-in-One", 14: "Sub Notebook",
    15: "Space-saving", 16: "Lunch Box", 17: "Main Server Chassis", 23: "Rack Mount",
    24: "Sealed-case PC", 30: "Tablet", 31: "Convertible", 32: "Detachable",
    35: "Mini PC", 36: "Stick PC",
}


def system_details(priv):
    import platform
    groups = []

    os_info = {}
    for line in (read_sysfs("/etc/os-release") or "").splitlines():
        key, _, value = line.partition("=")
        os_info[key] = value.strip('"')
    rows = []
    _add(rows, "Operating system", os_info.get("PRETTY_NAME"))
    _add(rows, "Distribution ID", os_info.get("ID"))
    _add(rows, "Kernel", platform.release())
    _add(rows, "Kernel build", platform.version())
    _add(rows, "Architecture", platform.machine())
    _add(rows, "Kernel command line", read_sysfs("/proc/cmdline"))
    tainted = read_sysfs("/proc/sys/kernel/tainted")
    if tainted is not None:
        _add(rows, "Kernel tainted", "No" if tainted == "0" else f"Yes (flags: {tainted})")
    _add(rows, "Hostname", platform.node())
    up = (read_sysfs("/proc/uptime") or "").split()
    if up:
        secs = int(float(up[0]))
        _add(rows, "Uptime", f"{secs // 86400}d {secs % 86400 // 3600}h {secs % 3600 // 60}m")
    groups.append(("Operating system", rows))

    rows = []
    _add(rows, "Desktop", os.environ.get("XDG_CURRENT_DESKTOP"))
    _add(rows, "Session type", os.environ.get("XDG_SESSION_TYPE"))
    _add(rows, "Qt version", QtCore.qVersion())
    try:
        import PySide6
        _add(rows, "PySide6 version", PySide6.__version__)
    except Exception:
        pass
    _add(rows, "Python version", platform.python_version())
    groups.append(("Session & runtime", rows))

    rows = []
    efi = os.path.isdir("/sys/firmware/efi")
    _add(rows, "Boot mode", "UEFI" if efi else "Legacy BIOS")
    if efi:
        secure = "Unknown"
        for path in glob.glob("/sys/firmware/efi/efivars/SecureBoot-*"):
            try:
                with open(path, "rb") as f:
                    secure = "Enabled" if f.read()[-1:] == b"\x01" else "Disabled"
            except Exception:
                pass
        _add(rows, "Secure Boot", secure)
    tpms = sorted(glob.glob("/sys/class/tpm/tpm*"))
    if tpms:
        ver = read_sysfs(tpms[0] + "/tpm_version_major")
        _add(rows, "TPM", f"Present (version {ver}.x)" if ver else "Present")
    else:
        _add(rows, "TPM", "Not detected")
    groups.append(("Firmware & security", rows))

    smbios = {
        "System Information": ("Manufacturer", "Product Name", "Version", "Serial Number", "UUID", "SKU Number", "Family"),
        "BIOS Information": ("Vendor", "Version", "Release Date", "ROM Size", "BIOS Revision", "Firmware Revision"),
        "Base Board Information": ("Manufacturer", "Product Name", "Version", "Serial Number", "Asset Tag"),
        "Chassis Information": ("Manufacturer", "Type", "Version", "Serial Number"),
    }
    titles = {"System Information": "System", "BIOS Information": "BIOS / UEFI firmware",
              "Base Board Information": "Motherboard", "Chassis Information": "Chassis"}
    if priv and priv.get("dmi"):
        for block_title, keys in smbios.items():
            for rows_in in _dmi_blocks(priv, block_title)[:1]:
                groups.append((titles[block_title], _pick(rows_in, keys)))
    else:
        def dmi(name):
            return read_sysfs(f"/sys/class/dmi/id/{name}")
        rows = []
        _add(rows, "Manufacturer", dmi("sys_vendor"))
        _add(rows, "Product", dmi("product_name"))
        _add(rows, "Version", dmi("product_version"))
        _add(rows, "Family", dmi("product_family"))
        chassis = dmi("chassis_type")
        if chassis and chassis.isdigit():
            _add(rows, "Chassis type", CHASSIS_TYPES.get(int(chassis), chassis))
        groups.append(("System", rows))
        rows = []
        _add(rows, "Manufacturer", dmi("board_vendor"))
        _add(rows, "Model", dmi("board_name"))
        _add(rows, "Version", dmi("board_version"))
        groups.append(("Motherboard", rows))
        rows = []
        _add(rows, "Vendor", dmi("bios_vendor"))
        _add(rows, "Version", dmi("bios_version"))
        _add(rows, "Release date", dmi("bios_date"))
        _add(rows, "Release", dmi("bios_release"))
        groups.append(("BIOS / UEFI firmware", rows))
        groups.extend(_priv_hint(priv, "serial numbers, UUID and full SMBIOS data"))
    return groups


# -- CPU -----------------------------------------------------------------------

CPU_FEATURES = [
    ("SSE4.2", ("sse4_2",)), ("AVX", ("avx",)), ("AVX2", ("avx2",)),
    ("AVX-512", ("avx512f",)), ("FMA3", ("fma",)), ("AES-NI", ("aes",)),
    ("SHA extensions", ("sha_ni", "sha")), ("BMI2", ("bmi2",)), ("RDRAND", ("rdrand",)),
    ("Hardware virtualization (VT-x / AMD-V)", ("vmx", "svm")),
]


def cpu_details(priv):
    groups = []
    ls = _kv_lines(run_c(["lscpu"]))
    cpuinfo = read_sysfs("/proc/cpuinfo") or ""
    info = _kv_lines(cpuinfo.split("\n\n")[0], ":") if cpuinfo else {}

    rows = []
    _add(rows, "Model name", ls.get("Model name") or info.get("model name"))
    _add(rows, "Vendor", ls.get("Vendor ID") or info.get("vendor_id"))
    _add(rows, "Architecture", ls.get("Architecture"))
    _add(rows, "Family / Model / Stepping",
         " / ".join(v for v in (ls.get("CPU family"), ls.get("Model"), ls.get("Stepping")) if v))
    _add(rows, "Microcode", info.get("microcode"))
    _add(rows, "Sockets", ls.get("Socket(s)"))
    _add(rows, "Cores per socket", ls.get("Core(s) per socket"))
    _add(rows, "Threads per core", ls.get("Thread(s) per core"))
    _add(rows, "Logical CPUs", ls.get("CPU(s)"))
    _add(rows, "Address sizes", ls.get("Address sizes"))
    groups.append(("Processor", rows))

    rows = []
    for label, key in (("Minimum frequency", "CPU min MHz"), ("Maximum frequency", "CPU max MHz")):
        try:
            _add(rows, label, f"{float(ls[key]):.0f} MHz")
        except (KeyError, ValueError):
            pass
    if psutil:
        try:
            freq = psutil.cpu_freq()
            if freq:
                _add(rows, "Current frequency (average)", f"{freq.current:.0f} MHz")
        except Exception:
            pass
    cpufreq = "/sys/devices/system/cpu/cpu0/cpufreq/"
    _add(rows, "Scaling driver", read_sysfs(cpufreq + "scaling_driver"))
    _add(rows, "Governor", read_sysfs(cpufreq + "scaling_governor"))
    _add(rows, "Available governors", read_sysfs(cpufreq + "scaling_available_governors"))
    _add(rows, "Energy performance preference", read_sysfs(cpufreq + "energy_performance_preference"))
    for label, path in (("Intel P-state mode", "/sys/devices/system/cpu/intel_pstate/status"),
                        ("AMD P-state mode", "/sys/devices/system/cpu/amd_pstate/status")):
        _add(rows, label, read_sysfs(path))
    no_turbo = read_sysfs("/sys/devices/system/cpu/intel_pstate/no_turbo")
    boost = read_sysfs("/sys/devices/system/cpu/cpufreq/boost")
    if no_turbo is not None:
        _add(rows, "Turbo / boost", "Disabled" if no_turbo == "1" else "Enabled")
    elif boost is not None:
        _add(rows, "Turbo / boost", "Enabled" if boost == "1" else "Disabled")
    groups.append(("Frequency & power management", rows))

    rows = []
    for label, key in (("L1 data cache", "L1d cache"), ("L1 instruction cache", "L1i cache"),
                       ("L2 cache", "L2 cache"), ("L3 cache", "L3 cache")):
        _add(rows, label, ls.get(key))
    groups.append(("Cache", rows))

    flags_text = ls.get("Flags") or info.get("flags") or info.get("Features") or ""
    flags = set(flags_text.split())
    rows = []
    if flags:
        for label, names in CPU_FEATURES:
            rows.append((label, "Yes" if flags.intersection(names) else "No"))
        rows.append(("Total CPU flags", str(len(flags))))
        rows.append(("All CPU flags", " ".join(sorted(flags))))
    for key in ("Virtualization", "Hypervisor vendor", "Virtualization type"):
        _add(rows, key, ls.get(key))
    groups.append(("Features", rows))

    rows = [(k[len("Vulnerability "):], v) for k, v in ls.items() if k.startswith("Vulnerability ")]
    groups.append(("Security mitigations", rows))

    rows = []
    zones = sorted(glob.glob(f"{SYS_POWERCAP}/intel-rapl:?"))
    if zones:
        readable = read_sysfs(zones[0] + "/energy_uj") is not None
        _add(rows, "RAPL energy counters", "Present and readable" if readable
             else "Present, but only root can read them (kernel security restriction)")
    else:
        _add(rows, "RAPL energy counters", "Not present (CPU or kernel doesn't provide them)")
    chips = {read_sysfs(h + "/name") for h in glob.glob(f"{SYS_CLASS_HWMON}/hwmon*")}
    _add(rows, "amd_energy driver", "Loaded" if "amd_energy" in chips else "Not loaded (only some Zen CPUs support it)")
    _add(rows, "zenpower driver", "Loaded" if "zenpower" in chips else "Not loaded (optional out-of-tree driver for Ryzen)")
    _add(rows, "Admin power helper", {"idle": "Not started", "starting": "Waiting for authentication",
                                      "running": "Running", "denied": "Authentication cancelled",
                                      "stopped": "Stopped", "failed": "Could not start"}.get(RAPL_HELPER.state, RAPL_HELPER.state))
    groups.append(("Power sensors", rows))

    for proc in _dmi_blocks(priv, "Processor Information"):
        groups.append(("Processor (SMBIOS)", _pick(proc, (
            "Socket Designation", "Manufacturer", "Version", "Family", "Voltage",
            "External Clock", "Max Speed", "Current Speed", "Status",
            "Core Count", "Core Enabled", "Thread Count", "Part Number", "Serial Number"))))
    if priv is None:
        groups.extend(_priv_hint(priv, "socket and SMBIOS processor data"))
    return groups


# -- Memory --------------------------------------------------------------------

def memory_details(priv):
    groups = []
    mi = _meminfo()
    rows = []
    for label, key in (("Total RAM", "MemTotal"), ("Available", "MemAvailable"), ("Free", "MemFree"),
                       ("Buffers", "Buffers"), ("Page cache", "Cached"), ("Shared memory", "Shmem"),
                       ("Dirty pages", "Dirty"), ("Transparent huge pages in use", "AnonHugePages"),
                       ("Kernel slab", "Slab")):
        if key in mi:
            _add(rows, label, human_bytes(mi[key]))
    if mi.get("Hugepagesize"):
        _add(rows, "Huge page size", human_bytes(mi["Hugepagesize"]))
        _add(rows, "Huge pages reserved", mi.get("HugePages_Total"))
    thp = read_sysfs("/sys/kernel/mm/transparent_hugepage/enabled")
    if thp:
        _add(rows, "Transparent huge pages", thp)
    groups.append(("Memory usage", rows))

    rows = []
    swaps = (read_sysfs("/proc/swaps") or "").splitlines()[1:]
    for line in swaps:
        parts = line.split()
        if len(parts) >= 5:
            name, kind, size, used, prio = parts[:5]
            _add(rows, f"{name} ({kind})",
                 f"{human_bytes(int(size) * 1024)} total, {human_bytes(int(used) * 1024)} used, priority {prio}")
    if not rows:
        _add(rows, "Swap", "None configured")
    zswap = read_sysfs("/sys/module/zswap/parameters/enabled")
    if zswap:
        _add(rows, "zswap", "Enabled" if zswap in ("Y", "1") else "Disabled")
    groups.append(("Swap", rows))

    if priv and priv.get("dmi"):
        for arr in _dmi_blocks(priv, "Physical Memory Array"):
            groups.append(("Memory array", _pick(arr, (
                "Location", "Use", "Error Correction Type", "Maximum Capacity", "Number Of Devices"))))
        for dev in _dmi_blocks(priv, "Memory Device"):
            d = dict(dev)
            locator = d.get("Locator", "Slot")
            if d.get("Size", "").startswith("No Module"):
                groups.append((f"{locator} - empty", [("Status", "No module installed")]))
                continue
            title = f"{locator} - {d.get('Size', '?')} {d.get('Type', '')}".strip()
            groups.append((title, _pick(dev, (
                "Size", "Type", "Form Factor", "Speed", "Configured Memory Speed", "Manufacturer",
                "Part Number", "Serial Number", "Rank", "Bank Locator", "Data Width", "Total Width",
                "Configured Voltage", "Minimum Voltage", "Maximum Voltage"))))
    else:
        groups.extend(_priv_hint(priv, "per-DIMM details (size, speed, manufacturer, part number)"))
    return groups


# -- GPU -----------------------------------------------------------------------

PCI_VENDORS = {"0x10de": "NVIDIA", "0x1002": "AMD", "0x8086": "Intel"}


def _nvidia_query():
    if not shutil.which("nvidia-smi"):
        return {}
    fields = ["pci.bus_id", "name", "driver_version", "vbios_version", "memory.total",
              "pcie.link.gen.current", "pcie.link.gen.max", "pcie.link.width.current",
              "pcie.link.width.max", "power.limit", "power.max_limit",
              "clocks.max.graphics", "clocks.max.memory"]
    out = run_c(["nvidia-smi", "--query-gpu=" + ",".join(fields), "--format=csv,noheader,nounits"], timeout=6)
    result = {}
    for line in (out or "").splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == len(fields):
            entry = dict(zip(fields, parts))
            result[entry["pci.bus_id"].lower()[-7:]] = entry
    return result


def _dpm_levels(path):
    """Parses amdgpu pp_dpm_* files into (current, max) strings."""
    lines = (read_sysfs(path) or "").splitlines()
    levels = [l.split(":", 1)[1].strip().rstrip("*").strip() for l in lines if ":" in l]
    current = next((l.split(":", 1)[1].strip().rstrip("*").strip() for l in lines if l.rstrip().endswith("*")), None)
    return current, (levels[-1] if levels else None)


def gpu_details(priv):
    groups = []
    nvidia = None
    for dev in sorted(glob.glob("/sys/bus/pci/devices/*")):
        cls = read_sysfs(dev + "/class") or ""
        if not cls.startswith("0x03"):
            continue
        addr = os.path.basename(dev)
        vendor = read_sysfs(dev + "/vendor") or ""
        device = read_sysfs(dev + "/device") or ""
        title = f"{PCI_VENDORS.get(vendor, 'Unknown vendor')} graphics device ({addr})"
        rows = []

        lspci = run_c(["lspci", "-nnk", "-s", addr])
        lines = (lspci or "").splitlines()
        if lines and "]: " in lines[0]:
            title = lines[0].split("]: ", 1)[1]
            _add(rows, "Device class", lines[0].split(" ", 1)[1].split(" [", 1)[0])
        for line in lines[1:]:
            line = line.strip()
            if line.startswith("Subsystem:"):
                _add(rows, "Subsystem", line.split(":", 1)[1])
            elif line.startswith("Kernel driver in use:"):
                _add(rows, "Kernel driver in use", line.split(":", 1)[1])
            elif line.startswith("Kernel modules:"):
                _add(rows, "Available kernel modules", line.split(":", 1)[1])
        _add(rows, "PCI address", addr)
        _add(rows, "PCI ID (vendor:device)", f"{vendor[2:]}:{device[2:]}")
        _add(rows, "Revision", read_sysfs(dev + "/revision"))
        if read_sysfs(dev + "/boot_vga") == "1":
            _add(rows, "Role", "Primary (boot) GPU")
        group_link = os.path.realpath(dev + "/iommu_group") if os.path.exists(dev + "/iommu_group") else None
        _add(rows, "IOMMU group", os.path.basename(group_link) if group_link else None)

        cur_speed = read_sysfs(dev + "/current_link_speed")
        cur_width = read_sysfs(dev + "/current_link_width")
        max_speed = read_sysfs(dev + "/max_link_speed")
        max_width = read_sysfs(dev + "/max_link_width")
        if cur_speed:
            _add(rows, "PCIe link (current)", f"{cur_speed} x{cur_width}")
        if max_speed:
            _add(rows, "PCIe link (maximum)", f"{max_speed} x{max_width}")
        _add(rows, "Runtime power state", read_sysfs(dev + "/power/runtime_status"))

        # AMD (amdgpu) details straight from sysfs
        for label, name in (("VRAM total", "mem_info_vram_total"), ("VRAM used", "mem_info_vram_used"),
                            ("GTT (shared system memory) total", "mem_info_gtt_total")):
            value = read_sysfs(f"{dev}/{name}")
            if value and value.isdigit():
                _add(rows, label, human_bytes(int(value)))
        _add(rows, "VBIOS version", read_sysfs(dev + "/vbios_version"))
        _add(rows, "Performance level", read_sysfs(dev + "/power_dpm_force_performance_level"))
        cur, top = _dpm_levels(dev + "/pp_dpm_sclk")
        if top:
            _add(rows, "Core clock (current / max level)", f"{cur or '?'} / {top}")
        cur, top = _dpm_levels(dev + "/pp_dpm_mclk")
        if top:
            _add(rows, "Memory clock (current / max level)", f"{cur or '?'} / {top}")
        for cap_file, label in (("power1_cap", "Power cap"), ("power1_cap_max", "Power cap (maximum)")):
            for path in glob.glob(f"{dev}/hwmon/hwmon*/{cap_file}"):
                value = read_sysfs(path)
                if value and value.isdigit():
                    _add(rows, label, f"{int(value) / 1_000_000:.0f} W")

        # DRM card: Intel frequencies, connected outputs
        for card in sorted(glob.glob("/sys/class/drm/card[0-9]")):
            if os.path.realpath(card + "/device") != os.path.realpath(dev):
                continue
            _add(rows, "DRM device", "/dev/dri/" + os.path.basename(card))
            for label, name in (("GPU frequency (current)", "gt_cur_freq_mhz"), ("GPU frequency (min)", "gt_min_freq_mhz"),
                                ("GPU frequency (max)", "gt_max_freq_mhz"), ("GPU frequency (boost)", "gt_boost_freq_mhz")):
                value = read_sysfs(f"{card}/{name}")
                if value:
                    _add(rows, label, f"{value} MHz")
            outputs = []
            for conn in sorted(glob.glob(card + "-*")):
                if read_sysfs(conn + "/status") == "connected":
                    outputs.append(os.path.basename(conn)[len(os.path.basename(card)) + 1:])
            _add(rows, "Connected outputs", ", ".join(outputs) if outputs else "None")

        if vendor == "0x10de":
            if nvidia is None:
                nvidia = _nvidia_query()
            nv = nvidia.get(addr.lower()[-7:])
            if nv:
                _add(rows, "NVIDIA driver version", nv["driver_version"])
                _add(rows, "VBIOS version", nv["vbios_version"])
                _add(rows, "VRAM total", f"{nv['memory.total']} MiB")
                _add(rows, "PCIe link (current, per driver)",
                     f"Gen {nv['pcie.link.gen.current']} x{nv['pcie.link.width.current']}")
                _add(rows, "PCIe link (maximum, per driver)",
                     f"Gen {nv['pcie.link.gen.max']} x{nv['pcie.link.width.max']}")
                _add(rows, "Power limit", f"{nv['power.limit']} W (max {nv['power.max_limit']} W)")
                _add(rows, "Max clocks (core / memory)",
                     f"{nv['clocks.max.graphics']} / {nv['clocks.max.memory']} MHz")
            elif not shutil.which("nvidia-smi"):
                _add(rows, "NVIDIA tools", f"nvidia-smi not found: {install_cmd(pacman='nvidia-utils')}")
        groups.append((title, rows))
    if not groups:
        groups.append(("Graphics", [("Status", "No PCI graphics device found")]))
    if not shutil.which("lspci"):
        groups.append(("Missing tool", [("pciutils", f"lspci not installed: {install_cmd(pacman='pciutils', apt='pciutils', dnf='pciutils')}")]))
    return groups


# -- Storage -------------------------------------------------------------------

def _smart_groups(js):
    groups = []
    rows = []
    passed = _dig(js, "smart_status", "passed")
    if passed is not None:
        _add(rows, "Overall health", "PASSED" if passed else "FAILED")
    _add(rows, "Temperature", f"{_dig(js, 'temperature', 'current')} \u00b0C" if _dig(js, "temperature", "current") is not None else None)
    hours = _dig(js, "power_on_time", "hours")
    if hours is not None:
        _add(rows, "Power-on time", f"{hours} h ({hours / 24:.0f} days)")
    _add(rows, "Power cycles", js.get("power_cycle_count"))
    _add(rows, "Form factor", _dig(js, "form_factor", "name"))
    _add(rows, "Interface speed", _dig(js, "interface_speed", "current", "string"))

    nvme = js.get("nvme_smart_health_information_log")
    if nvme:
        _add(rows, "Critical warning flags", nvme.get("critical_warning"))
        _add(rows, "Percentage used (wear)", f"{nvme['percentage_used']} %" if "percentage_used" in nvme else None)
        if "available_spare" in nvme:
            _add(rows, "Available spare", f"{nvme['available_spare']} % (threshold {nvme.get('available_spare_threshold', '?')} %)")
        if "data_units_written" in nvme:
            _add(rows, "Total data written", human_bytes(nvme["data_units_written"] * 512000, True))
        if "data_units_read" in nvme:
            _add(rows, "Total data read", human_bytes(nvme["data_units_read"] * 512000, True))
        _add(rows, "Unsafe shutdowns", nvme.get("unsafe_shutdowns"))
        _add(rows, "Media / data integrity errors", nvme.get("media_errors"))
        _add(rows, "Error log entries", nvme.get("num_err_log_entries"))
    groups.append(("SMART health", rows))

    table = _dig(js, "ata_smart_attributes", "table")
    if table:
        attr_rows = []
        for a in table:
            raw = _dig(a, "raw", "string") or _dig(a, "raw", "value")
            flag = f"  [FAILING: {a['when_failed']}]" if a.get("when_failed") not in (None, "", "-") else ""
            attr_rows.append((f"{a['id']:>3}  {a['name']}",
                              f"value {a['value']} / worst {a['worst']} / threshold {a['thresh']} / raw {raw}{flag}"))
        groups.append(("SMART attributes", attr_rows))
    return groups


def storage_details(priv):
    groups = []
    skip = ("loop", "ram", "zram", "sr", "fd", "dm-", "md")
    for blk in sorted(os.listdir("/sys/block")) if os.path.isdir("/sys/block") else []:
        if blk.startswith(skip) or re.match(r"mmcblk\d+(boot\d+|rpmb)$", blk):
            continue
        base = f"/sys/block/{blk}"
        dev_path = "/dev/" + blk
        props = {}
        for line in (run_c(["udevadm", "info", "--query=property", "--name=" + dev_path]) or "").splitlines():
            k, _, v = line.partition("=")
            props[k] = v

        nvme_ctrl = None
        m = re.match(r"(nvme\d+)n\d+", blk)
        if m:
            nvme_ctrl = f"/sys/class/nvme/{m.group(1)}"

        rows = []
        model = (read_sysfs(nvme_ctrl + "/model") if nvme_ctrl else None) or read_sysfs(base + "/device/model") \
            or props.get("ID_MODEL", "").replace("_", " ")
        _add(rows, "Model", model)
        _add(rows, "Vendor", read_sysfs(base + "/device/vendor"))
        _add(rows, "Firmware", (read_sysfs(nvme_ctrl + "/firmware_rev") if nvme_ctrl else None)
             or read_sysfs(base + "/device/rev") or props.get("ID_REVISION"))
        _add(rows, "Serial number", (read_sysfs(nvme_ctrl + "/serial") if nvme_ctrl else None)
             or props.get("ID_SERIAL_SHORT"))
        _add(rows, "WWN", props.get("ID_WWN"))

        size = int(read_sysfs(base + "/size") or 0) * 512
        _add(rows, "Capacity", cap_str(size) if size else None)
        rotational = read_sysfs(base + "/queue/rotational")
        if nvme_ctrl:
            kind = "NVMe SSD"
        elif rotational == "1":
            kind = "Hard disk (rotational)"
        elif rotational == "0":
            kind = "Solid-state drive"
        else:
            kind = None
        _add(rows, "Type", kind)
        bus = props.get("ID_BUS") or (read_sysfs(nvme_ctrl + "/transport") if nvme_ctrl else None)
        _add(rows, "Interface", bus)
        if nvme_ctrl:
            speed = read_sysfs(nvme_ctrl + "/device/current_link_speed")
            width = read_sysfs(nvme_ctrl + "/device/current_link_width")
            if speed:
                _add(rows, "PCIe link", f"{speed} x{width}")
            _add(rows, "Controller state", read_sysfs(nvme_ctrl + "/state"))
        _add(rows, "Logical / physical sector size",
             f"{read_sysfs(base + '/queue/logical_block_size')} / {read_sysfs(base + '/queue/physical_block_size')} bytes")
        sched = read_sysfs(base + "/queue/scheduler")
        _add(rows, "I/O scheduler", sched)
        _add(rows, "Removable", "Yes" if read_sysfs(base + "/removable") == "1" else "No")
        _add(rows, "Read-only", "Yes" if read_sysfs(base + "/ro") == "1" else "No")
        _add(rows, "Partition table", props.get("ID_PART_TABLE_TYPE"))
        title = f"{dev_path} - {model or 'Unknown drive'}"
        groups.append((title, rows))

        lsblk = run_c(["lsblk", "-J", "-o", "NAME,SIZE,FSTYPE,LABEL,MOUNTPOINT", dev_path])
        if lsblk:
            try:
                parts = []

                def walk(nodes, depth=0):
                    for n in nodes:
                        if depth:
                            desc = " ".join(x for x in (n.get("size"), n.get("fstype"),
                                                        f"\"{n['label']}\"" if n.get("label") else None) if x)
                            if n.get("mountpoint"):
                                desc += f" -> {n['mountpoint']}"
                            parts.append((n["name"], desc))
                        walk(n.get("children", []), depth + 1)
                walk(json.loads(lsblk).get("blockdevices", []))
                if parts:
                    groups.append((f"{dev_path} partitions", parts))
            except (ValueError, KeyError):
                pass

        smart = (priv or {}).get("smart", {}).get(dev_path)
        if smart:
            groups.extend((f"{dev_path} - {t}", r) for t, r in _smart_groups(smart))
    if priv is None or priv.get("missing"):
        groups.extend(_priv_hint(priv, "SMART health, wear and error counters"))
    return groups


# -- Network -------------------------------------------------------------------

def network_details(priv):
    groups = []
    if not os.path.isdir("/sys/class/net"):
        return groups
    addrs = psutil.net_if_addrs() if psutil else {}
    counters = psutil.net_io_counters(pernic=True) if psutil else {}
    for name in sorted(os.listdir("/sys/class/net")):
        if name == "lo" or name.startswith("veth"):
            continue
        base = f"/sys/class/net/{name}"
        rows = []
        wireless = os.path.isdir(base + "/wireless") or os.path.isdir(base + "/phy80211")
        has_device = os.path.exists(base + "/device")
        _add(rows, "Type", "Wi-Fi" if wireless else ("Ethernet" if has_device else "Virtual / software interface"))
        _add(rows, "State", read_sysfs(base + "/operstate"))
        _add(rows, "MAC address", read_sysfs(base + "/address"))
        _add(rows, "MTU", read_sysfs(base + "/mtu"))
        speed = read_sysfs(base + "/speed")
        if speed and speed.lstrip("-").isdigit() and int(speed) > 0:
            _add(rows, "Link speed", f"{speed} Mb/s")
        _add(rows, "Duplex", read_sysfs(base + "/duplex"))
        if has_device:
            driver = os.path.realpath(base + "/device/driver") if os.path.exists(base + "/device/driver") else None
            _add(rows, "Driver", os.path.basename(driver) if driver else None)
            _add(rows, "Bus path", os.path.basename(os.path.realpath(base + "/device")))
            vendor, device = read_sysfs(base + "/device/vendor"), read_sysfs(base + "/device/device")
            if vendor and device:
                _add(rows, "Hardware ID (vendor:device)", f"{vendor[2:]}:{device[2:]}")
        for a in addrs.get(name, []):
            fam = getattr(a.family, "name", str(a.family))
            if fam == "AF_INET":
                _add(rows, "IPv4 address", f"{a.address} / {a.netmask}")
            elif fam == "AF_INET6":
                _add(rows, "IPv6 address", a.address.split("%")[0])
        c = counters.get(name)
        if c:
            _add(rows, "Received", f"{human_bytes(c.bytes_recv)} ({c.packets_recv} packets, {c.errin} errors, {c.dropin} dropped)")
            _add(rows, "Sent", f"{human_bytes(c.bytes_sent)} ({c.packets_sent} packets, {c.errout} errors, {c.dropout} dropped)")
        if wireless and shutil.which("iw"):
            link = _kv_lines(run_c(["iw", "dev", name, "link"]))
            for key, label in (("SSID", "Connected network"), ("freq", "Frequency (MHz)"),
                               ("signal", "Signal"), ("rx bitrate", "Receive bitrate"), ("tx bitrate", "Transmit bitrate")):
                _add(rows, label, link.get(key))
        groups.append((name, rows))
    return groups


# -- Battery -------------------------------------------------------------------

def battery_details(priv):
    groups = []
    for supply in sorted(glob.glob("/sys/class/power_supply/*")):
        if read_sysfs(supply + "/type") != "Battery":
            continue

        def val(name):
            return read_sysfs(f"{supply}/{name}")

        def num(name):
            v = val(name)
            return int(v) if v and v.lstrip("-").isdigit() else None

        rows = []
        _add(rows, "Manufacturer", val("manufacturer"))
        _add(rows, "Model", val("model_name"))
        _add(rows, "Serial number", val("serial_number"))
        _add(rows, "Chemistry", val("technology"))
        _add(rows, "Status", val("status"))
        _add(rows, "Charge level", f"{val('capacity')} %" if val("capacity") else None)
        _add(rows, "Capacity level", val("capacity_level"))
        volt_design = num("voltage_min_design") or num("voltage_now")
        design = num("energy_full_design")
        full = num("energy_full")
        if design is None and num("charge_full_design") and volt_design:
            design = num("charge_full_design") * volt_design / 1_000_000
            full = num("charge_full") * volt_design / 1_000_000 if num("charge_full") else None
        if design:
            _add(rows, "Design capacity", f"{design / 1_000_000:.2f} Wh")
        if full:
            _add(rows, "Full-charge capacity", f"{full / 1_000_000:.2f} Wh")
        if design and full:
            _add(rows, "Wear level", f"{max(0.0, 100 - full / design * 100):.1f} % (health {full / design * 100:.1f} %)")
        _add(rows, "Cycle count", val("cycle_count"))
        if num("voltage_now"):
            _add(rows, "Voltage", f"{num('voltage_now') / 1_000_000:.2f} V")
        power = num("power_now")
        if power is None and num("current_now") and num("voltage_now"):
            power = abs(num("current_now")) * num("voltage_now") / 1_000_000
        if power:
            _add(rows, "Power draw", f"{abs(power) / 1_000_000:.2f} W")
        _add(rows, "Charge start threshold", f"{val('charge_control_start_threshold')} %" if val("charge_control_start_threshold") else None)
        _add(rows, "Charge stop threshold", f"{val('charge_control_end_threshold')} %" if val("charge_control_end_threshold") else None)
        groups.append((f"Battery ({os.path.basename(supply)})", rows))
    for supply in sorted(glob.glob("/sys/class/power_supply/*")):
        if read_sysfs(supply + "/type") == "Mains":
            online = read_sysfs(supply + "/online")
            groups.append((f"AC adapter ({os.path.basename(supply)})",
                           [("Connected", "Yes" if online == "1" else "No")]))
    return groups


# -- Collect everything --------------------------------------------------------

DETAIL_TABS = [
    ("System", system_details), ("CPU", cpu_details), ("Memory", memory_details),
    ("GPU", gpu_details), ("Storage", storage_details), ("Network", network_details),
    ("Battery", battery_details),
]


def collect_all(priv):
    data = {}
    for name, func in DETAIL_TABS:
        try:
            groups = [(t, r) for t, r in func(priv) if r]
        except Exception as exc:
            groups = [("Error", [("Could not collect", f"{type(exc).__name__}: {exc}")])]
        data[name] = groups
    return data


def report_text(data):
    lines = ["HW Monitor - device report", time.strftime("Generated %Y-%m-%d %H:%M:%S"), ""]
    for tab, groups in data.items():
        if not groups:
            continue
        lines += [f"===== {tab} =====", ""]
        for title, rows in groups:
            lines.append(f"[{title}]")
            width = max((len(k) for k, _ in rows), default=0)
            lines += [f"  {k.ljust(width)} : {v}" for k, v in rows]
            lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Device details window
# --------------------------------------------------------------------------

class DetailsBridge(QtCore.QObject):
    done = QtCore.Signal(object, str)  # object: keeps tab order (dict would be re-sorted by Qt)


class DeviceDetailsDialog(QtWidgets.QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Device Details")
        self.setWindowIcon(QtGui.QIcon.fromTheme("utilities-system-monitor"))
        self.resize(940, 680)

        self.priv = None
        self.data = {}
        self.busy = False
        self.wanted_tab = "System"
        self._group_items = []

        self.bridge = DetailsBridge()
        self.bridge.done.connect(self._on_done)

        layout = QtWidgets.QVBoxLayout(self)
        top = QtWidgets.QHBoxLayout()
        self.status = QtWidgets.QLabel("Collecting device information...")
        top.addWidget(self.status, 1)
        self.priv_btn = QtWidgets.QPushButton("Load extended details...")
        self.priv_btn.setToolTip(
            "Asks for your admin password (via polkit) to read DIMM modules, serial numbers\n"
            "and SMART drive health, which Linux only exposes to root.")
        self.priv_btn.clicked.connect(lambda: self._start(want_priv=True))
        top.addWidget(self.priv_btn)
        layout.addLayout(top)

        self.tabs = QtWidgets.QTabWidget()
        layout.addWidget(self.tabs, 1)

        buttons = QtWidgets.QHBoxLayout()
        self.refresh_btn = QtWidgets.QPushButton("Refresh")
        self.refresh_btn.clicked.connect(lambda: self._start(want_priv=False))
        copy_btn = QtWidgets.QPushButton("Copy report")
        copy_btn.clicked.connect(self._copy_report)
        save_btn = QtWidgets.QPushButton("Save report...")
        save_btn.clicked.connect(self._save_report)
        close_btn = QtWidgets.QPushButton("Close")
        close_btn.clicked.connect(self.close)
        for b in (self.refresh_btn, copy_btn, save_btn):
            buttons.addWidget(b)
        buttons.addStretch(1)
        buttons.addWidget(close_btn)
        layout.addLayout(buttons)

        QtWidgets.QApplication.instance().paletteChanged.connect(self._recolor)

    # -- public ------------------------------------------------------------

    def open_tab(self, name=None):
        if name:
            self.wanted_tab = name
            for i in range(self.tabs.count()):
                if self.tabs.tabText(i) == name:
                    self.tabs.setCurrentIndex(i)
        self.show()
        self.raise_()
        self.activateWindow()
        self._start(want_priv=False)

    # -- collection --------------------------------------------------------

    def _start(self, want_priv):
        if self.busy:
            return
        self.busy = True
        self.priv_btn.setEnabled(False)
        self.refresh_btn.setEnabled(False)
        self.status.setText("Waiting for admin authentication..." if want_priv else "Collecting device information...")
        threading.Thread(target=self._worker, args=(want_priv,), daemon=True).start()

    def _worker(self, want_priv):
        message = ""
        if want_priv:
            priv, err = fetch_privileged()
            if priv is not None:
                self.priv = priv
                message = "Extended details loaded."
            else:
                message = err
        self.bridge.done.emit(collect_all(self.priv), message)

    def _on_done(self, data, message):
        self.busy = False
        self.data = data
        self.priv_btn.setEnabled(True)
        self.refresh_btn.setEnabled(True)
        self.priv_btn.setText("Reload extended details..." if self.priv else "Load extended details...")
        self.status.setText(message or ("Updated " + time.strftime("%H:%M:%S")
                                        + ("" if self.priv else " - some details need admin access")))
        self._rebuild()

    # -- rendering ---------------------------------------------------------

    def _rebuild(self):
        if self.tabs.count():
            self.wanted_tab = self.tabs.tabText(self.tabs.currentIndex())
        while self.tabs.count():
            widget = self.tabs.widget(0)
            self.tabs.removeTab(0)
            widget.deleteLater()
        self._group_items = []
        for name, groups in self.data.items():
            if groups:
                self.tabs.addTab(self._make_tree(groups), name)
        for i in range(self.tabs.count()):
            if self.tabs.tabText(i) == self.wanted_tab:
                self.tabs.setCurrentIndex(i)

    def _make_tree(self, groups):
        tree = QtWidgets.QTreeWidget()
        tree.setColumnCount(2)
        tree.setHeaderLabels(["Property", "Value"])
        tree.setUniformRowHeights(True)
        tree.setAlternatingRowColors(True)
        tree.setFont(monospace_font(10))
        header = tree.header()
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.Stretch)
        tree.setColumnWidth(0, 340)
        accent = QtGui.QBrush(theme_colors(self.palette())["accent"])
        for title, rows in groups:
            group = QtWidgets.QTreeWidgetItem([title])
            font = group.font(0)
            font.setBold(True)
            group.setFont(0, font)
            group.setForeground(0, accent)
            tree.addTopLevelItem(group)
            group.setFirstColumnSpanned(True)
            self._group_items.append(group)
            for key, value in rows:
                item = QtWidgets.QTreeWidgetItem([key, value])
                item.setToolTip(1, value)
                group.addChild(item)
            group.setExpanded(True)
        tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        tree.customContextMenuRequested.connect(lambda pos, t=tree: self._tree_menu(t, pos))
        shortcut = QtGui.QShortcut(QtGui.QKeySequence.StandardKey.Copy, tree)
        shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        shortcut.activated.connect(lambda t=tree: self._copy_selected(t))
        return tree

    def _recolor(self, palette=None):
        accent = QtGui.QBrush(theme_colors(self.palette())["accent"])
        for group in self._group_items:
            try:
                group.setForeground(0, accent)
            except RuntimeError:
                pass  # item already deleted by a rebuild

    # -- clipboard / export ------------------------------------------------

    def _tree_menu(self, tree, pos):
        item = tree.itemAt(pos)
        if item is None or item.childCount():
            return
        menu = QtWidgets.QMenu(self)
        copy_value = menu.addAction("Copy value")
        copy_row = menu.addAction("Copy row")
        chosen = menu.exec(tree.viewport().mapToGlobal(pos))
        clipboard = QtGui.QGuiApplication.clipboard()
        if chosen == copy_value:
            clipboard.setText(item.text(1))
        elif chosen == copy_row:
            clipboard.setText(f"{item.text(0)}: {item.text(1)}")

    def _copy_selected(self, tree):
        item = tree.currentItem()
        if item is not None and not item.childCount():
            QtGui.QGuiApplication.clipboard().setText(f"{item.text(0)}: {item.text(1)}")

    def _copy_report(self):
        QtGui.QGuiApplication.clipboard().setText(report_text(self.data))
        self.status.setText("Report copied to clipboard.")

    def _save_report(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save device report", time.strftime("hwmonitor-report-%Y%m%d.txt"), "Text files (*.txt)")
        if path:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(report_text(self.data))
                self.status.setText(f"Report saved to {path}")
            except OSError as exc:
                self.status.setText(f"Could not save report: {exc}")


# --------------------------------------------------------------------------
# "Choose Sensors" window: which rows appear in the main list, and in what order
# --------------------------------------------------------------------------

def apply_saved_order(current, saved):
    """`current` items sorted by their position in `saved`; anything not in
    `saved` (a brand-new sensor, say) keeps its relative order at the end."""
    rank = {item: i for i, item in enumerate(saved)}
    known = sorted((x for x in current if x in rank), key=rank.get)
    return known + [x for x in current if x not in rank]


class _ReorderTree(QtWidgets.QTreeWidget):
    """A tree you can rearrange by dragging: devices move among devices, and a
    sensor moves only among the other sensors of its own device (never into
    another device, and never turning into a device itself)."""

    order_changed = QtCore.Signal()

    def __init__(self):
        super().__init__()
        # ExtendedSelection: Ctrl+Click toggles extra rows into the selection
        # (Shift+Click picks a range), so Move Up / Move Down and dragging can
        # act on several sensors at once.
        self.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QtWidgets.QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)

    @staticmethod
    def drop_allowed(source, target, indicator):
        Position = QtWidgets.QAbstractItemView.DropIndicatorPosition
        between = indicator in (Position.AboveItem, Position.BelowItem)
        if source is None:
            return False
        if source.parent() is None:  # a device
            if target is None:
                return indicator == Position.OnViewport  # empty space below the list: move to the end
            return target.parent() is None and between
        return target is not None and target.parent() is source.parent() and between

    def _screen_position(self, item):
        """Sort key: where a row sits on screen (device number, then row in device)."""
        parent = item.parent()
        if parent is None:
            return (self.indexOfTopLevelItem(item), -1)
        return (self.indexOfTopLevelItem(parent), parent.indexOfChild(item))

    def dropEvent(self, event):
        source = self.currentItem()
        target = self.itemAt(event.position().toPoint())
        if not self.drop_allowed(source, target, self.dropIndicatorPosition()):
            event.ignore()
            return
        # A drag with several rows selected moves them all, so every dragged
        # row has to be allowed to travel with the current one: sensors of one
        # device, or devices among devices. A mixed selection is refused
        # rather than letting a device end up inside another device, or a
        # sensor jump across devices.
        dragged = [item for item in self.selectedItems() if item is not source]
        for item in dragged:
            if item.parent() is not source.parent():
                event.ignore()
                return
        expanded = [(item, item.isExpanded()) for item in dragged]
        source_expanded = source.isExpanded()
        # Qt moves a multi-row drag in the order the rows were *selected*, not
        # the order they appear in, so Ctrl+Clicking bottom-to-top would flip
        # the group. Re-selecting them top-to-bottom first keeps their on-screen
        # order intact when they land.
        for item in sorted(self.selectedItems(), key=self._screen_position):
            item.setSelected(False)
            item.setSelected(True)
        super().dropEvent(event)
        source.setExpanded(source_expanded)
        for item, was_expanded in expanded:
            item.setExpanded(was_expanded)
        # Keep the moved group selected so it can be dragged again immediately
        for item in dragged:
            item.setSelected(True)
        source.setSelected(True)
        self.order_changed.emit()


class SensorChooserDialog(QtWidgets.QDialog):
    """A checklist of every sensor, grouped by device, that also sets the order
    they appear in. `groups` is [(device_id, device_title, [(sensor_id, label,
    current_value_text)])] in the order currently shown, and `hidden` is the
    set of sensor ids that are currently unchecked. `defaults` is
    (device_ids, {device_id: [sensor_ids]}) in the app's natural order, used
    by the Restore default order button."""

    def __init__(self, groups, hidden, parent=None, defaults=None):
        super().__init__(parent)
        self.setWindowTitle("Choose Sensors")
        self.setWindowIcon(QtGui.QIcon.fromTheme("utilities-system-monitor"))
        self.resize(640, 720)
        self._busy = False
        self._leaves = []
        self._defaults = defaults or ([], {})

        layout = QtWidgets.QVBoxLayout(self)
        intro = QtWidgets.QLabel(
            "Check the sensors you want to see in the main list. Unchecked sensors are hidden, but "
            "are still recorded, so graphs and the summary bar keep working.\n"
            "To change the order, drag rows up or down, or select one or more rows (Ctrl+Click picks "
            "several at once) and use Move Up / Move Down. "
            "Devices can be moved among devices, and sensors within their own device.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.filter = QtWidgets.QLineEdit()
        self.filter.setPlaceholderText("Filter sensors (for example: temp, core, drive)...")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self._apply_filter)
        layout.addWidget(self.filter)

        self.tree = _ReorderTree()
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels(["Sensor", "Value now"])
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(True)
        header = self.tree.header()
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.tree, 1)

        movable = (Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable
                   | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsDragEnabled)
        for device_id, title, sensors in groups:
            device = QtWidgets.QTreeWidgetItem([title])
            device.setFlags(movable | Qt.ItemFlag.ItemIsDropEnabled)
            device.setData(0, Qt.ItemDataRole.UserRole, device_id)
            font = device.font(0)
            font.setBold(True)
            device.setFont(0, font)
            self.tree.addTopLevelItem(device)
            for sensor_id, label, value in sensors:
                item = QtWidgets.QTreeWidgetItem([label, value])
                item.setFlags(movable | Qt.ItemFlag.ItemNeverHasChildren)
                item.setCheckState(0, Qt.CheckState.Unchecked if sensor_id in hidden else Qt.CheckState.Checked)
                item.setData(0, Qt.ItemDataRole.UserRole, sensor_id)
                item.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                device.addChild(item)
                self._leaves.append(item)
            device.setExpanded(True)
            self._refresh_device(device)
        self.tree.itemChanged.connect(self._on_item_changed)
        self.tree.order_changed.connect(lambda: self._apply_filter(self.filter.text()))

        order_row = QtWidgets.QHBoxLayout()
        for text, tip, slot in (
            ("Move Up", "Move the selected rows up", lambda: self._move_selected(-1)),
            ("Move Down", "Move the selected rows down", lambda: self._move_selected(+1)),
            ("Collapse all", "Fold every device, which makes dragging devices around easier",
             lambda: self._set_expanded(False)),
            ("Expand all", "Unfold every device", lambda: self._set_expanded(True)),
            ("Restore default order", "Put devices and sensors back in the app's original order",
             self.restore_default_order),
        ):
            button = QtWidgets.QPushButton(text)
            button.setToolTip(tip)
            button.clicked.connect(slot)
            order_row.addWidget(button)
        order_row.addStretch(1)
        layout.addLayout(order_row)

        row = QtWidgets.QHBoxLayout()
        check_all = QtWidgets.QPushButton("Check all")
        check_all.setToolTip("Checks every sensor shown right now (respects the filter)")
        check_all.clicked.connect(lambda: self._set_visible(Qt.CheckState.Checked))
        uncheck_all = QtWidgets.QPushButton("Uncheck all")
        uncheck_all.setToolTip("Unchecks every sensor shown right now (respects the filter)")
        uncheck_all.clicked.connect(lambda: self._set_visible(Qt.CheckState.Unchecked))
        check_selected = QtWidgets.QPushButton("Check selected")
        check_selected.setToolTip(
            "Checks the selected rows (Ctrl+Click picks several); a selected device checks all its "
            "sensors; respects the filter")
        check_selected.clicked.connect(lambda: self._set_selected(Qt.CheckState.Checked))
        uncheck_selected = QtWidgets.QPushButton("Uncheck selected")
        uncheck_selected.setToolTip(
            "Unchecks the selected rows (Ctrl+Click picks several); a selected device unchecks all its "
            "sensors; respects the filter")
        uncheck_selected.clicked.connect(lambda: self._set_selected(Qt.CheckState.Unchecked))
        row.addWidget(check_all)
        row.addWidget(uncheck_all)
        row.addWidget(check_selected)
        row.addWidget(uncheck_selected)
        row.addStretch(1)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        row.addWidget(buttons)
        layout.addLayout(row)

    # -- check boxes -----------------------------------------------------

    def _refresh_device(self, device):
        """A device's own box mirrors its sensors: checked, unchecked, or partial."""
        states = [device.child(i).checkState(0) for i in range(device.childCount())]
        if states and all(s == Qt.CheckState.Checked for s in states):
            state = Qt.CheckState.Checked
        elif states and all(s == Qt.CheckState.Unchecked for s in states):
            state = Qt.CheckState.Unchecked
        else:
            state = Qt.CheckState.PartiallyChecked
        was, self._busy = self._busy, True
        device.setCheckState(0, state)
        self._busy = was

    def _on_item_changed(self, item, column):
        if column != 0 or self._busy:
            return
        self._busy = True
        try:
            if item.parent() is None:  # clicked a device: apply to all its sensors
                state = item.checkState(0)
                if state != Qt.CheckState.PartiallyChecked:
                    for i in range(item.childCount()):
                        item.child(i).setCheckState(0, state)
            else:
                self._refresh_device(item.parent())
        finally:
            self._busy = False

    def _set_visible(self, state):
        self._busy = True
        try:
            for item in self._leaves:
                if not item.isHidden() and not item.parent().isHidden():
                    item.setCheckState(0, state)
        finally:
            self._busy = False
        for i in range(self.tree.topLevelItemCount()):
            self._refresh_device(self.tree.topLevelItem(i))

    def _set_selected(self, state):
        """Checks or unchecks only the rows picked in the tree: Ctrl+Click puts
        several in the selection at once, and a picked device row stands for
        all of its sensors. With nothing picked there is nothing to do."""
        items = self.tree.selectedItems()
        if not items:
            return
        leaves = []
        for item in items:
            if item.parent() is None:  # a selected device means all of its sensors
                leaves.extend(item.child(i) for i in range(item.childCount()))
            else:
                leaves.append(item)
        # Respect the filter: only toggle rows that are currently visible
        leaves = [leaf for leaf in leaves if not leaf.isHidden() and not leaf.parent().isHidden()]
        self._busy = True
        try:
            for leaf in leaves:
                leaf.setCheckState(0, state)
        finally:
            self._busy = False
        for i in range(self.tree.topLevelItemCount()):
            self._refresh_device(self.tree.topLevelItem(i))

    def _apply_filter(self, text):
        text = text.strip().lower()
        for i in range(self.tree.topLevelItemCount()):
            device = self.tree.topLevelItem(i)
            device_matches = text in device.text(0).lower()
            any_shown = False
            for j in range(device.childCount()):
                child = device.child(j)
                sensor_id = (child.data(0, Qt.ItemDataRole.UserRole) or "").lower()
                shown = not text or device_matches or text in child.text(0).lower() or text in sensor_id
                child.setHidden(not shown)
                any_shown = any_shown or shown
            device.setHidden(not any_shown)
            if text:
                device.setExpanded(True)

    # -- ordering ---------------------------------------------------------

    def _set_expanded(self, expanded):
        for i in range(self.tree.topLevelItemCount()):
            self.tree.topLevelItem(i).setExpanded(expanded)

    def _move_selected(self, direction):
        """Moves every selected row one place up (-1) or down (+1) among its
        siblings, hopping over rows the filter is hiding. Ctrl+Click puts
        several rows in the selection at once; they then travel as a block.
        Sensors still only ever move within their own device and devices
        only among devices, so a selection spanning several devices (or
        mixing devices and sensors) simply moves each part on its own."""
        selected = self.tree.selectedItems()
        if not selected:
            item = self.tree.currentItem()  # nothing picked with Ctrl: fall back to the current row
            if item is None:
                return
            selected = [item]
        current = self.tree.currentItem()

        # Rows with different parents can never swap places, so each parent's
        # selected rows are moved as their own block.
        groups = {}
        for item in selected:
            groups.setdefault(item.parent(), []).append(item)

        moved = False
        for parent, items in groups.items():
            count = parent.childCount() if parent else self.tree.topLevelItemCount()

            def index_of(item):
                return parent.indexOfChild(item) if parent else self.tree.indexOfTopLevelItem(item)

            def sibling(i):
                return parent.child(i) if parent else self.tree.topLevelItem(i)

            # Walk the rows in the direction of travel, so the leading row of
            # a block moves first and the rest fall in behind it; a row whose
            # way is blocked by another selected row stays put (that row has
            # already moved, or couldn't).
            items.sort(key=index_of)
            if direction > 0:
                items.reverse()
            for item in items:
                index = index_of(item)
                target = index + direction
                while 0 <= target < count and sibling(target).isHidden():
                    target += direction
                if not 0 <= target < count:
                    continue  # already at the top / bottom
                if any(sibling(target) is other for other in items):
                    continue  # the row in the way is part of the moving block
                expanded = item.isExpanded()
                if parent:
                    parent.takeChild(index)
                    parent.insertChild(target, item)
                else:
                    self.tree.takeTopLevelItem(index)
                    self.tree.insertTopLevelItem(target, item)
                item.setExpanded(expanded)
                moved = True

        if not moved:
            return
        # Taking a row out of the tree deselects it, so pick the group again
        # to keep it ready for the next click of Move Up / Move Down.
        for item in selected:
            item.setSelected(True)
        if any(current is item for item in selected):
            self.tree.setCurrentItem(
                current, 0, QtCore.QItemSelectionModel.SelectionFlag.NoUpdate)
        self._apply_filter(self.filter.text())

    def restore_default_order(self):
        default_devices, default_sensors = self._defaults
        device_rank = {d: i for i, d in enumerate(default_devices)}
        sensor_rank = {s: i for ids in default_sensors.values() for i, s in enumerate(ids)}
        far = 10**9  # anything unknown sorts to the end, keeping its relative order
        devices = [self.tree.takeTopLevelItem(0) for _ in range(self.tree.topLevelItemCount())]
        devices.sort(key=lambda d: device_rank.get(d.data(0, Qt.ItemDataRole.UserRole), far))
        for device in devices:
            kids = device.takeChildren()
            kids.sort(key=lambda k: sensor_rank.get(k.data(0, Qt.ItemDataRole.UserRole), far))
            device.addChildren(kids)
            self.tree.addTopLevelItem(device)
            device.setExpanded(True)
        self._apply_filter(self.filter.text())

    # -- results ----------------------------------------------------------

    def listed_ids(self):
        return {item.data(0, Qt.ItemDataRole.UserRole) for item in self._leaves}

    def unchecked_ids(self):
        return {item.data(0, Qt.ItemDataRole.UserRole) for item in self._leaves
                if item.checkState(0) == Qt.CheckState.Unchecked}

    def device_order(self):
        return [self.tree.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole)
                for i in range(self.tree.topLevelItemCount())]

    def sensor_order(self):
        order = {}
        for i in range(self.tree.topLevelItemCount()):
            device = self.tree.topLevelItem(i)
            order[device.data(0, Qt.ItemDataRole.UserRole)] = [
                device.child(j).data(0, Qt.ItemDataRole.UserRole) for j in range(device.childCount())]
        return order


# --------------------------------------------------------------------------
# Qt application
# --------------------------------------------------------------------------

class HWMonitorWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("HW Monitor")
        self.setWindowIcon(QtGui.QIcon.fromTheme("utilities-system-monitor"))
        self.resize(1100, 800)

        self.collectors = Collectors()
        self.hw_names = get_hardware_names()

        self.categories = [
            ("cpu", f"Processor [{self.hw_names['cpu']}]", self.collectors.collect_cpu),
            ("mem", self.hw_names["mem"], self.collectors.collect_memory),
        ]

        self.gpus = get_gpus()
        for i, gpu in enumerate(self.gpus):
            title = f"Graphics [{gpu['name']}]" if len(self.gpus) == 1 else f"Graphics {i+1} [{gpu['name']}]"
            self.categories.append((f"gpu{i}", title, lambda g=gpu: self.collectors.collect_gpu_device(g)))
        self.categories.append(("mobo", self.hw_names["mobo"], self.collectors.collect_lmsensors))

        groups = get_storage_groups()
        for i, (model_name, dev_names) in enumerate(groups):
            cat_id = f"disk_{i}"
            if len(dev_names) > 1:
                cat_title = f"Storage {i+1} [{model_name} ({len(dev_names)} Drives)]"
            else:
                cat_title = f"Storage {i+1} [{model_name}]"
            self.categories.append((cat_id, cat_title, lambda dns=dev_names: self.collectors.collect_storage_group(drive_names=dns)))

        self.categories.extend([
            ("net", self.hw_names["net"], self.collectors.collect_network),
            ("batt", "Battery", self.collectors.collect_battery),
        ])
        self._category_titles = {cid: title for cid, title, _ in self.categories}

        self.store = SensorStore()
        self.category_items = {}
        self.leaf_items = {}
        self.open_graphs = {}
        self.details_dialog = None
        self.settings = QtCore.QSettings("hwmonitor", "hwmonitor")
        self.hidden_sensors = set(self._load_setting("hidden_sensors", []))
        self.category_order = list(self._load_setting("category_order", []))
        self.sensor_order = dict(self._load_setting("sensor_order", {}))
        self._default_sensor_order = {}  # the app's natural order, for "Restore default order"
        self.is_updating = False
        self.is_alive = True

        self.bridge = DataBridge()
        self.bridge.data_ready.connect(self._apply_data)

        self._build_ui()
        self._update_uptime()

        app = QtWidgets.QApplication.instance()
        app.paletteChanged.connect(self._on_palette_changed)

        if psutil:
            psutil.cpu_percent(interval=None)
            psutil.cpu_percent(interval=None, percpu=True)

        self.tick_timer = QtCore.QTimer(self)
        self.tick_timer.timeout.connect(self._on_tick)
        self.tick_timer.start(1000)
        self._start_update_thread()

    # -- UI construction ---------------------------------------------------

    def _build_ui(self):
        central = QtWidgets.QWidget()
        root = QtWidgets.QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.setCentralWidget(central)

        toolbar = QtWidgets.QToolBar()
        toolbar.setMovable(False)
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.addToolBar(toolbar)
        reset_action = toolbar.addAction("Reset Min/Max")
        reset_action.triggered.connect(self._on_reset_clicked)
        details_action = toolbar.addAction("Device Details...")
        details_action.setShortcut("Ctrl+I")
        details_action.setToolTip("In-depth info on every device (Ctrl+I)")
        details_action.triggered.connect(lambda: self.show_details())
        choose_action = toolbar.addAction("Choose Sensors...")
        choose_action.setToolTip("Pick which sensors appear in the list")
        choose_action.triggered.connect(self._on_choose_sensors)
        self.power_action = toolbar.addAction("Enable CPU Power...")
        self.power_action.setToolTip(
            "Your kernel only lets root read the CPU's energy counters.\n"
            "This asks for your admin password once (per session) to show CPU power draw.")
        self.power_action.triggered.connect(self._on_enable_power)
        self.power_action.setVisible(False)

        self.summary_bar = QtWidgets.QFrame()
        self.summary_bar.setFrameShape(QtWidgets.QFrame.Shape.StyledPanel)
        summary_layout = QtWidgets.QHBoxLayout(self.summary_bar)
        summary_layout.setContentsMargins(8, 10, 8, 10)
        root.addWidget(self.summary_bar)

        self.summary_labels = {}
        for key, title in (
            ("cpu_load", "CPU Load"), ("cpu_temp", "CPU Temp"),
            ("mem_load", "Memory"), ("gpu_load", "GPU Load"),
            ("gpu_temp", "GPU Temp"), ("uptime", "Uptime"),
        ):
            card = QtWidgets.QVBoxLayout()
            val = QtWidgets.QLabel("--")
            val.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            val_font = monospace_font(15)
            val_font.setBold(True)
            val.setFont(val_font)
            cap = QtWidgets.QLabel(title.upper())
            cap.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            cap_font = cap.font()
            cap_font.setPointSize(8)
            cap.setFont(cap_font)
            card.addWidget(val)
            card.addWidget(cap)
            summary_layout.addLayout(card)
            self.summary_labels[key] = val

        self.tree = QtWidgets.QTreeWidget()
        self.tree.setColumnCount(len(COLUMN_TITLES_QT))
        self.tree.setHeaderLabels(COLUMN_TITLES_QT)
        self.tree.setUniformRowHeights(True)
        self.tree.setFont(monospace_font(10))
        # Like Dolphin's "Automatic Column Width": the Sensor column stretches
        # to soak up all spare space, so the info columns stay pinned to the
        # right edge whether the window is maximized or not. The info columns
        # are sized to fit their content (see _fit_info_columns).
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        for i in range(1, len(COLUMN_TITLES_QT)):
            header.setSectionResizeMode(i, QtWidgets.QHeaderView.ResizeMode.Fixed)
            self.tree.setColumnWidth(i, 100)
        self.tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._on_tree_menu)
        root.addWidget(self.tree, 1)

        self.status_label = QtWidgets.QLabel("")
        self.status_label.setContentsMargins(8, 4, 8, 4)
        status_font = self.status_label.font()
        status_font.setPointSize(9)
        self.status_label.setFont(status_font)
        root.addWidget(self.status_label)

    # -- Interaction ---------------------------------------------------

    # -- choosing which sensors are shown ----------------------------------

    def _load_setting(self, key, default):
        """Saved settings are kept as JSON text: QSettings mangles one-item lists."""
        try:
            value = json.loads(str(self.settings.value(key, "")))
            return value if isinstance(value, type(default)) else default
        except (ValueError, TypeError):
            return default

    def _save_settings(self):
        self.settings.setValue("hidden_sensors", json.dumps(sorted(self.hidden_sensors)))
        self.settings.setValue("category_order", json.dumps(self.category_order))
        self.settings.setValue("sensor_order", json.dumps(self.sensor_order))
        self.settings.sync()

    def _sensor_groups(self):
        """Every device and sensor seen so far, in the order currently shown:
        [(device id, device title, [(sensor id, label, value)])]."""
        groups = []
        for i in range(self.tree.topLevelItemCount()):
            cat = self.tree.topLevelItem(i)
            sensors = []
            for j in range(cat.childCount()):
                item = cat.child(j)
                sensors.append((item.data(0, Qt.ItemDataRole.UserRole), item.text(0), item.text(1)))
            groups.append((cat.data(0, Qt.ItemDataRole.UserRole + 1), cat.text(0), sensors))
        return groups

    def _apply_hidden_sensors(self, hidden):
        self.hidden_sensors = set(hidden)
        for item in self.leaf_items.values():
            item.setHidden(item.data(0, Qt.ItemDataRole.UserRole) in self.hidden_sensors)
        self._refresh_category_visibility()

    def _refresh_category_visibility(self):
        """A device whose sensors are all hidden disappears from the list too."""
        for cat in self.category_items.values():
            any_shown = any(not cat.child(i).isHidden() for i in range(cat.childCount()))
            if cat.isHidden() == any_shown:
                cat.setHidden(not any_shown)

    def _reapply_hidden_flags(self):
        """Taking rows out of the tree and putting them back (to reorder them)
        forgets which were hidden, so set that again from the saved choices."""
        for item in self.leaf_items.values():
            item.setHidden(item.data(0, Qt.ItemDataRole.UserRole) in self.hidden_sensors)
        self._refresh_category_visibility()

    def _apply_order(self):
        """Puts devices and their sensors into the saved order. Does nothing
        (cheaply) when they already are, so it's fine to call every update;
        sensors that show up later are slotted in as soon as they appear."""
        changed = False
        for cat_id, cat in self.category_items.items():
            saved = self.sensor_order.get(cat_id)
            if not saved:
                continue
            current = [cat.child(i).data(0, Qt.ItemDataRole.UserRole) for i in range(cat.childCount())]
            wanted = apply_saved_order(current, saved)
            if wanted != current:
                by_id = {item.data(0, Qt.ItemDataRole.UserRole): item for item in cat.takeChildren()}
                cat.addChildren([by_id[sid] for sid in wanted])
                changed = True
        if self.category_order:
            current = [self.tree.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole + 1)
                       for i in range(self.tree.topLevelItemCount())]
            wanted = apply_saved_order(current, self.category_order)
            if wanted != current:
                expanded = {cid: item.isExpanded() for cid, item in self.category_items.items()}
                by_id = {}
                while self.tree.topLevelItemCount():
                    item = self.tree.takeTopLevelItem(0)
                    by_id[item.data(0, Qt.ItemDataRole.UserRole + 1)] = item
                for cid in wanted:
                    self.tree.addTopLevelItem(by_id[cid])
                for cid, item in self.category_items.items():
                    item.setExpanded(expanded.get(cid, True))
                changed = True
        if changed:
            self._reapply_hidden_flags()

    def _on_choose_sensors(self):
        defaults = ([cid for cid, _, _ in self.categories], self._default_sensor_order)
        dialog = SensorChooserDialog(self._sensor_groups(), self.hidden_sensors, self, defaults)
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        # keep choices for things that aren't on screen right now (a drive that's
        # unplugged, a GPU that's asleep), and replace the rest
        self.hidden_sensors = (self.hidden_sensors - dialog.listed_ids()) | dialog.unchecked_ids()
        shown_devices = dialog.device_order()
        self.category_order = shown_devices + [c for c in self.category_order if c not in shown_devices]
        for cat_id, ids in dialog.sensor_order().items():
            self.sensor_order[cat_id] = ids + [s for s in self.sensor_order.get(cat_id, []) if s not in ids]
        self._apply_order()
        self._apply_hidden_sensors(self.hidden_sensors)
        self._save_settings()

    def _on_enable_power(self):
        self.collectors.rapl_helper.start()
        self.power_action.setVisible(False)
        self.status_label.setText("Waiting for admin authentication for CPU power readings...")

    def _on_reset_clicked(self):
        self.store.reset_minmax()
        for sensor_id, win in self.open_graphs.items():
            win.refresh(self.store.get_history(sensor_id))

    def _on_item_double_clicked(self, item, column):
        full_id = item.data(0, Qt.ItemDataRole.UserRole)
        if not full_id:
            return
        d = self.store.data.get(full_id)
        if not d or d.get("is_text"):
            return
        existing = self.open_graphs.get(full_id)
        if existing is not None:
            existing.raise_()
            existing.activateWindow()
            return
        history = self.store.get_history(full_id)
        win = GraphWindow(full_id, d["label"], d["unit"], history, on_close=self._on_graph_closed)
        self.open_graphs[full_id] = win

    def _on_tree_menu(self, pos):
        item = self.tree.itemAt(pos)
        if item is None:
            return
        top = item.parent() or item
        cat_id = top.data(0, Qt.ItemDataRole.UserRole + 1)
        menu = QtWidgets.QMenu(self)
        action = menu.addAction("Device details...")
        if menu.exec(self.tree.viewport().mapToGlobal(pos)) == action:
            self.show_details(self._tab_for_category(cat_id))

    @staticmethod
    def _tab_for_category(cat_id):
        if not cat_id:
            return None
        if cat_id.startswith("disk_"):
            return "Storage"
        if cat_id.startswith("gpu"):
            return "GPU"
        return {"cpu": "CPU", "mem": "Memory", "mobo": "System",
                "net": "Network", "batt": "Battery"}.get(cat_id)

    def show_details(self, tab=None):
        if self.details_dialog is None:
            self.details_dialog = DeviceDetailsDialog(self)
        self.details_dialog.open_tab(tab)

    def _on_graph_closed(self, sensor_id):
        self.open_graphs.pop(sensor_id, None)

    def _on_palette_changed(self, palette):
        # Plasma theme (or accent color) changed while we're running -
        # recolor the bits we draw or color manually; everything else is
        # native Qt widgetry and re-themes itself automatically.
        colors = theme_colors(self.palette())
        for cat_id, item in self.category_items.items():
            item.setForeground(0, QtGui.QBrush(colors["accent"]))
        for win in self.open_graphs.values():
            win.graph.update()

    # -- Data loop ---------------------------------------------------

    def _on_tick(self):
        self._update_uptime()
        self._start_update_thread()

    def _update_uptime(self):
        seconds = read_uptime_seconds()
        self.summary_labels["uptime"].setText(fmt_uptime(seconds) if seconds is not None else "--")

    def _start_update_thread(self):
        if self.is_updating:
            return
        self.is_updating = True
        thread = threading.Thread(target=self._gather_data, daemon=True)
        thread.start()

    def _gather_data(self):
        data = {}
        for cat_id, cat_title, collector in self.categories:
            try:
                rows = collector()
            except Exception:
                rows = []
            if rows:
                data[cat_id] = (cat_title, rows)
        self.bridge.data_ready.emit(data)

    def _apply_data(self, data):
        if not self.is_alive:
            return

        self.is_updating = False
        colors = theme_colors(self.palette())

        for cat_id, (cat_title, rows) in data.items():
            cat_item = self.category_items.get(cat_id)
            is_new_category = cat_item is None
            if is_new_category:
                cat_item = QtWidgets.QTreeWidgetItem([cat_title])
                font = cat_item.font(0)
                font.setBold(True)
                cat_item.setFont(0, font)
                cat_item.setForeground(0, QtGui.QBrush(colors["accent"]))
                self.tree.addTopLevelItem(cat_item)
                cat_item.setData(0, Qt.ItemDataRole.UserRole + 1, cat_id)
                self.category_items[cat_id] = cat_item

            seen = set()
            for sid, label, value, unit, dec in rows:
                if value is None:  # sensor present but reporting nothing (e.g. "[N/A]")
                    continue
                seen.add(sid)
                full_id = f"{cat_id}.{sid}"
                self.store.update(full_id, label, value, unit, dec if dec is not None else 1)
                cur, mn, mx, avg = self.store.row(full_id)
                color = self._severity_color(value, unit, colors)
                key = (cat_id, sid)
                leaf_item = self.leaf_items.get(key)
                if leaf_item is None:
                    leaf_item = QtWidgets.QTreeWidgetItem([label, cur, mn, mx, avg])
                    for col in range(1, len(COLUMN_TITLES_QT)):
                        leaf_item.setTextAlignment(col, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                    leaf_item.setData(0, Qt.ItemDataRole.UserRole, full_id)
                    cat_item.addChild(leaf_item)
                    self.leaf_items[key] = leaf_item
                    leaf_item.setHidden(full_id in self.hidden_sensors)
                    self._default_sensor_order.setdefault(cat_id, []).append(full_id)
                else:
                    leaf_item.setText(0, label)
                    leaf_item.setText(1, cur)
                    leaf_item.setText(2, mn)
                    leaf_item.setText(3, mx)
                    leaf_item.setText(4, avg)
                leaf_item.setForeground(1, QtGui.QBrush(color))
                if isinstance(value, str):
                    leaf_item.setToolTip(1, value)  # long hint text gets elided in the column

            self._mark_missing(cat_id, seen)
            if is_new_category:
                cat_item.setExpanded(True)

        for cat_id in self.category_items:
            if cat_id not in data:
                self._mark_missing(cat_id, set())
        self._apply_order()
        self._refresh_category_visibility()

        self._fit_info_columns()
        self.power_action.setVisible(self.collectors.power_status == "needs_admin")
        self._update_summary()
        self.status_label.setText("Last updated " + time.strftime("%H:%M:%S"))

        for sensor_id, win in list(self.open_graphs.items()):
            win.refresh(self.store.get_history(sensor_id))

    def _mark_missing(self, cat_id, seen):
        """Rows a device stopped reporting (drive unplugged, GPU asleep, ...)
        show a dash instead of freezing on their last value."""
        muted = QtGui.QBrush(theme_colors(self.palette())["muted"])
        for (c, sid), item in list(self.leaf_items.items()):
            if c != cat_id or sid in seen:
                continue
            if sid.endswith("hint"):  # a how-to-enable note that is no longer needed
                item.parent().removeChild(item)
                del self.leaf_items[(c, sid)]
            elif item.text(1) != "\u2014":
                item.setText(1, "\u2014")
                item.setForeground(1, muted)

    def _fit_info_columns(self):
        """Sizes the info columns to their content, but only ever grows them
        so values changing every second don't make the columns jitter."""
        header = self.tree.header()
        for col in range(1, len(COLUMN_TITLES_QT)):
            wanted = max(self.tree.sizeHintForColumn(col), header.sectionSizeHint(col)) + 16
            wanted = min(wanted, 180)  # long one-off hint text shouldn't hog the row
            if wanted > self.tree.columnWidth(col):
                self.tree.setColumnWidth(col, wanted)

    def _severity_color(self, value, unit, colors):
        if unit == "\u00b0C" and isinstance(value, (int, float)):
            if value >= 90:
                return colors["crit"]
            if value >= 75:
                return colors["warn"]
        return colors["text"]

    def _find_current(self, cat_id, sid):
        item = self.leaf_items.get((cat_id, sid))
        return item.text(1) if item else None

    def _find_cpu_temp(self):
        candidates = [(sid, item) for (c, sid), item in self.leaf_items.items() if c == "cpu" and sid.startswith("temp.")]
        if not candidates:
            return None
        for sid, item in candidates:
            if "package" in sid.lower():
                return item.text(1)
        return candidates[0][1].text(1)

    def _gpu_value(self, *sensor_ids):
        """Current text of a sensor from the main GPU: the one with the most
        dedicated memory (so a discrete GPU wins over an integrated one),
        falling back to any other GPU that reports it."""
        gpu_cats = [c for c, _, _ in self.categories if c.startswith("gpu")]
        gpu_cats.sort(key=lambda c: -(self.store.data.get(f"{c}.memtotal", {}).get("cur") or 0))
        for cat in gpu_cats:
            for sid in sensor_ids:
                text = self._find_current(cat, sid)
                if text and text != "\u2014":
                    return text
        return None

    def _update_summary(self):
        self.summary_labels["cpu_load"].setText(self._find_current("cpu", "total") or "--")
        self.summary_labels["cpu_temp"].setText(self._find_cpu_temp() or "--")
        self.summary_labels["mem_load"].setText(self._find_current("mem", "used_pct") or "--")
        self.summary_labels["gpu_load"].setText(self._gpu_value("load", "activity") or "--")
        self.summary_labels["gpu_temp"].setText(self._gpu_value("temp") or "--")

    def closeEvent(self, event):
        self.is_alive = False
        self.tick_timer.stop()
        for win in list(self.open_graphs.values()):
            win.close()
        if self.details_dialog is not None:
            self.details_dialog.close()
        self.collectors.rapl_helper.stop()
        super().closeEvent(event)


def main():
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("HW Monitor")
    win = HWMonitorWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
