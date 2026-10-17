"""What this computer can offer the network: a GPU slices can run on, and its memory."""

from pathlib import Path
import subprocess

DRM = Path("/sys/class/drm")


def nvidia():
    """Total and free MiB across NVIDIA GPUs, or None w/o one."""
    try:
        output = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total,memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10, check=True).stdout
        gpus = [[int(value) for value in line.split(",")] for line in output.splitlines() if line.strip()]
    except (OSError, subprocess.SubprocessError, ValueError):
        return None  # no nvidia-smi, or it failed

    if not gpus:
        return None
    return sum(total for total, _ in gpus), sum(free for _, free in gpus)


def intel():
    """Total MiB across Intel GPUs w/ their own memory (Arc), or None.

    Intel's drivers don't report memory size. W/ Resizable BAR on (which Arc wants
    anyway), a GPU's largest PCI memory window spans all of its memory; integrated
    GPUs only show a small one.
    """
    total = 0
    for card in DRM.glob("card*"):
        device = card / "device"
        try:
            if not card.name[4:].isdigit() or (device / "vendor").read_text().strip() != "0x8086" \
                    or not (device / "class").read_text().startswith("0x03"):  # an Intel display controller
                continue
            windows = [int(end, 16) - int(start, 16) + 1 for start, end, _ in
                       (line.split() for line in (device / "resource").read_text().splitlines()) if int(end, 16)]
        except (OSError, ValueError):
            continue

        largest = max(windows, default=0) // 2**20
        if largest >= 2048:  # integrated GPUs only show a small window
            total += largest
    return total or None


def gpu():
    """The GPU slices run on: ("cuda" or "xpu", total MiB, free MiB), or (None, None, None)."""
    if memory := nvidia():
        return ("cuda", *memory)
    if total := intel():
        return "xpu", total, total  # free memory isn't reported either; count it all
    return None, None, None


def gpu_memory():
    """Total and free MiB of that GPU, or (None, None) w/o one."""
    return gpu()[1:]
