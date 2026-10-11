"""Read-only resource sampler for the explicitly supplied test process PID."""
import argparse
import csv
import ctypes
import json
import time
from pathlib import Path

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--pid", type=int, required=True)
    p.add_argument("--output", default=str(Path(__file__).resolve().parents[1] / "运行日志/resource_samples.csv"))
    args = p.parse_args()
    kernel = ctypes.windll.kernel32
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(ctypes.c_ulonglong)] * 4
    class Counters(ctypes.Structure):
        _fields_ = [("cb",ctypes.c_ulong),("faults",ctypes.c_ulong)] + [(x,ctypes.c_size_t) for x in ["peak_working_set", "working_set", "peak_pool_paged", "pool_paged", "peak_pool_nonpaged", "pool_nonpaged", "pagefile", "peak_pagefile"]]
    ctypes.windll.psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(Counters), ctypes.c_ulong]
    handle = kernel.OpenProcess(0x0400 | 0x0010, 0, args.pid)
    if not handle:
        raise RuntimeError("无法读取指定进程")
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    try:
        with path.open("a", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["time", "pid", "working_set_bytes", "peak_working_set_bytes", "cpu_seconds"])
            if not existed:
                writer.writeheader()
            while True:
                exit_code = ctypes.c_ulong()
                if not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)) or exit_code.value != 259:
                    break
                mem = Counters()
                mem.cb = ctypes.sizeof(mem)
                if not ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(mem), mem.cb):
                    break
                created, exited, ktime, utime = [ctypes.c_ulonglong() for _ in range(4)]
                kernel.GetProcessTimes(handle, *[ctypes.byref(x) for x in [created, exited, ktime, utime]])
                writer.writerow({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "pid": args.pid, "working_set_bytes": mem.working_set, "peak_working_set_bytes": mem.peak_working_set, "cpu_seconds": (ktime.value + utime.value) / 10000000})
                f.flush()
                time.sleep(10)
    finally:
        kernel.CloseHandle(handle)
    print("资源采样完成")

if __name__ == "__main__":
    main()
