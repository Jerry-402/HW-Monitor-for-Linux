# HW Monitor

A live hardware monitor for Linux made on Claude Sonnet 5.5 completely free (I'm not a coder at all but really wanted an app like HWiNFO for Linux). It shows you what your computer is doing right now: how hot the processor is, how hard the graphics card is working, how fast a drive is reading, how much power the CPU is using, and much more, all in one window.

It is built with Qt 6, the same toolkit KDE Plasma uses, so it matches your desktop theme, colors, fonts and accent color automatically, and follows along if you change them while it's running.

> **Not affiliated with HWiNFO.** This project is inspired by monitoring tools like HWiNFO on Windows, but it is a separate program and shares no code with it.

<img width="2560" height="1395" alt="HW Monitor 1" src="https://github.com/user-attachments/assets/47789158-0159-45c8-a877-857c2b3e0120" />

<img width="2557" height="1390" alt="HW Monitor 2" src="https://github.com/user-attachments/assets/83a98413-b86a-40a2-8663-7acf855bd242" />

<img width="2560" height="1394" alt="HW Monitor 3" src="https://github.com/user-attachments/assets/f1028a4c-e3c9-4b55-bee5-ab54667f9f12" />

<img width="2560" height="1391" alt="HW Monitor 4" src="https://github.com/user-attachments/assets/41e2a421-a357-4302-ab98-0937670e8514" />


## What it does

**Live sensors, grouped by device.** The main window is a list you can expand and collapse. Each reading shows its current value plus the lowest, highest and average since you opened the app.

- **Processor:** usage and clock speed for every core and thread, temperatures, and power draw
- **Memory:** RAM and swap usage
- **Graphics:** usage, memory, temperature, fan speed, clock speed and power for each graphics card (NVIDIA, AMD and Intel, including systems with more than one)
- **Motherboard:** fan speeds, voltages and temperatures
- **Storage:** space used and free, temperature, and **live read speed, write speed and activity** for each drive
- **Network:** upload and download speed in Mbit/s
- **Battery:** charge level and status on laptops

**A summary bar** along the top shows CPU load, CPU temperature, memory use, GPU load, GPU temperature and system uptime at a glance.

**Pop-out live graphs.** Double-click any number in the list to open its own graph window. You can have several open side by side.

- Resize a graph window to change how much time it shows. The default size shows 60 seconds, and every time you make it that much wider it shows another 60 seconds.
- The app remembers the last 30 minutes. Scroll the mouse wheel (up goes back in time, down goes forward) or drag the scrollbar underneath to look back. Scroll all the way down, or double-click the graph, to return to live.

**Device Details.** Click **Device Details...** in the toolbar (or press `Ctrl+I`, or right-click any device in the list) for an in-depth look at your hardware, with a tab each for System, CPU, Memory, GPU, Storage, Network and Battery. It shows things like your CPU model and cache sizes, graphics card and link speed, drive models and firmware, network driver and addresses, and battery wear. **Copy report** and **Save report...** export everything as plain text, which is handy when asking for help online.

**Choose Sensors.** Click **Choose Sensors...** in the toolbar to open a checklist of every sensor, grouped by device, and check only the ones you want in the main list. There's a filter box for finding sensors quickly, and checking or unchecking a device's box applies to all of its sensors. You can also **change the order**: drag devices up and down among the other devices, and drag sensors up and down within their own device (or select a row and use **Move Up / Move Down**). Hold **Ctrl** and click to select several rows and move them together as a group; **Check selected / Uncheck selected** act on just the rows you've picked. **Restore default order** puts everything back. Hidden sensors are still recorded, so graphs and the summary bar keep working, and your choices and order are remembered the next time you open the app.

**Reset Min/Max** clears the lowest/highest/average numbers and the graph history so you can start measuring fresh.

## Requirements

- A Linux computer. It was built on Arch-based CachyOS with KDE Plasma, and should work on other distributions and desktops that can run Qt apps.
- Python 3
- PySide6 (the Qt 6 library for Python)
- psutil (reads basic system information)

## Install

### CachyOS / Arch Linux

Open a terminal and run:

```
sudo pacman -S pyside6 python-psutil
```

### Other distributions

The package names vary. If your distribution offers PySide6 and psutil, install them with its package manager (for example, `python3-pyside6` and `python3-psutil` on Fedora). If PySide6 isn't packaged for your system, use a Python virtual environment:

```
python3 -m venv venv
source venv/bin/activate
pip install PySide6 psutil
```

Run the app from inside that environment.

### Download and run

```
git clone https://github.com/YOUR-USERNAME/hwmonitor.git
cd hwmonitor
python3 hwmonitor.py
```

(Or just download `hwmonitor.py` and run it with `python3 hwmonitor.py`. It's a single file.)

If something needed is missing, the app tells you what to install and shows the right command for your distribution.

## Add it to your application menu (optional)

To launch it by clicking instead of typing, create a file at `~/.local/share/applications/hwmonitor.desktop` containing the text below. Change the path on the `Exec` line to wherever you saved `hwmonitor.py`.

```
[Desktop Entry]
Type=Application
Name=HW Monitor
Comment=Live hardware sensor monitor
Exec=python3 /home/YOUR-USERNAME/hwmonitor/hwmonitor.py
Icon=utilities-system-monitor
Terminal=false
Categories=System;Monitor;
StartupNotify=true
```

HW Monitor should then show up when you search for it in your application launcher.

## Getting more sensors (optional extras)

The app works with what it finds, and quietly skips anything your computer doesn't have. These extras unlock more detail. Package names below are for Arch/CachyOS.

| To get this | Install this |
|---|---|
| Motherboard fans, voltages and temperatures | `sudo pacman -S lm_sensors`, then run `sudo sensors-detect` once and answer the questions |
| Graphics card details (NVIDIA) | `sudo pacman -S nvidia-utils` |
| Graphics card names | `sudo pacman -S pciutils` |
| Memory module details and serial numbers | `sudo pacman -S dmidecode` |
| Drive health (SMART) | `sudo pacman -S smartmontools` |
| Wi-Fi signal strength and speed | `sudo pacman -S iw` |
| **SATA hard drive and SSD temperatures** | Run `sudo modprobe drivetemp`. To make it permanent, run `echo drivetemp \| sudo tee /etc/modules-load.d/drivetemp.conf` |

### CPU power draw

Showing how many watts your CPU uses is trickier, because Linux only lets the administrator read the CPU's energy counter. The app tries several ways, in this order:

1. It reads the counter directly, on systems that allow it.
2. It uses AMD's `amd_energy` driver, on the few CPUs that support it.
3. It uses the **zenpower3** driver, if you have installed it. This gives separate core and SoC power for **Ryzen 1000 to 5000 series** CPUs. On Arch-based systems it's in the AUR as `zenpower3-dkms`.
4. If the counter exists but is admin-only, an **Enable CPU Power...** button appears in the toolbar. Click it and enter your password once. The power reading then works until you close the app.

If you have a newer Ryzen (7000 or 9000 series), zenpower3 may not support it, so the toolbar button is the way to go.

## About the password prompts

A few features need administrator access, because Linux only shows that information to the administrator. The app asks through your system's standard password dialog (polkit), and only when you click the button for it:

- **Load extended details...** (in Device Details) runs `dmidecode` and `smartctl` to read memory module details, serial numbers and drive health. These only read information.
- **Enable CPU Power...** runs a small fixed loop that reads the CPU's energy counter once a second. It stops by itself when you close HW Monitor.

Nothing runs as administrator unless you click one of these. The whole program is a single readable file, so you can check exactly what it does.

## Known limitations

- **Linux only.** It works best on ordinary Intel and AMD computers.
- **Sensors depend on your hardware and Linux kernel.** If the kernel doesn't expose a reading, the app can't show it. This is most common for motherboard fans and voltages on very new boards.
- **Intel graphics** show clock speed and an "activity" percentage. Intel's drivers don't give normal users a true load figure, so activity is estimated from how much time the GPU spends out of its idle state.
- **CPU power on Intel** systems usually needs the **Enable CPU Power...** button, because of the same admin restriction described above.
- **A reading that disappears shows a dash** (—) instead of an old frozen value. This happens, for example, when a graphics card on a laptop goes to sleep to save power. The app deliberately doesn't wake it up.
- **Drive speeds are in MB/s** (1 MB = 1,000,000 bytes) and **network speeds are in Mbit/s**, matching how those are usually advertised.

## What it's been tested on

I wrote and use this on **CachyOS with KDE Plasma and an AMD Ryzen 5 5600X3D** (with zenpower3). <!-- Add your graphics card and drives here, for example: "NVIDIA GeForce RTX ..., one NVMe SSD and two SATA hard drives." -->

Support for the following was written from documentation and checked only against simulated data, **not on real hardware**, so there may be bugs: Intel CPUs and graphics, laptops and batteries, systems with more than one graphics card, SD/eMMC/virtual drives, LVM/encrypted/RAID setups, and ARM boards such as the Raspberry Pi. Reports from people who try it on those are especially welcome.

## Troubleshooting

- **It won't start and mentions PySide6 or psutil.** Install them as described under [Install](#install).
- **No Motherboard section, or no fan speeds.** Install `lm_sensors` and run `sudo sensors-detect` (see the table above).
- **A reading shows —.** The device stopped reporting it, or is asleep. It comes back on its own when the device does.
- **No CPU power.** Open **Device Details**, go to the **CPU** tab and look at the **Power sensors** group. It shows exactly which sources your computer has.
- **No SATA drive temperature.** See the `drivetemp` row in the table above.

## Reporting a problem

Open an issue and include:

1. Your distribution and desktop (for example, "CachyOS, KDE Plasma").
2. What you expected to see and what you saw instead.
3. A report from **Device Details → Save report...**. Open it in a text editor first and delete the serial numbers if you don't want to share them.

## License

See the [LICENSE](LICENSE) file.
