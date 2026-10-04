"""Performance stress tests with hard limits and a thermal auto-stop.

Each test streams progress through the ctx object and stops early when the
server cancels it, when its time limit is reached, or when CPU temperature
crosses max_temp_c. They never run unless the technician approves them.
"""

import multiprocessing
import os
import random
import tempfile
import time

import psutil

from tools_common import human, max_cpu_temperature, common_temperatures


def _cpu_burn(stop_time):
    value = 0.0
    while time.time() < stop_time:
        for index in range(1, 50000):
            value += (index ** 0.5) * 1.000001
    return value


def stress_cpu(seconds=60, workers=0, max_temp_c=95, ctx=None, **_):
    workers = workers or (psutil.cpu_count() or 1)
    stop_time = time.time() + seconds
    pool = []
    for _ in range(workers):
        process = multiprocessing.Process(target=_cpu_burn, args=(stop_time,), daemon=True)
        process.start()
        pool.append(process)
    samples = []
    stopped_reason = "completed"
    start_clock = (psutil.cpu_freq().current if psutil.cpu_freq() else None)
    min_clock = max_clock = start_clock
    try:
        while time.time() < stop_time:
            time.sleep(1)
            load = psutil.cpu_percent(interval=None)
            temperature = max_cpu_temperature()
            frequency = psutil.cpu_freq()
            clock = frequency.current if frequency else None
            if clock:
                min_clock = min(min_clock or clock, clock)
                max_clock = max(max_clock or clock, clock)
            sample = {"t": round(stop_time - time.time()), "cpu_percent": load, "temp_c": temperature, "clock_mhz": round(clock) if clock else None}
            samples.append(sample)
            if ctx and ctx.progress({"sample": sample}):
                stopped_reason = "cancelled"
                break
            if temperature and temperature >= max_temp_c:
                stopped_reason = "thermal-cutoff"
                break
    finally:
        for process in pool:
            process.terminate()
        for process in pool:
            process.join(timeout=3)
    temps = [s["temp_c"] for s in samples if s["temp_c"]]
    throttled = bool(min_clock and max_clock and min_clock < max_clock * 0.85)
    return {
        "test": "cpu", "workers": workers, "seconds_run": len(samples), "stopped_reason": stopped_reason,
        "max_temp_c": max(temps) if temps else None, "avg_cpu_percent": round(sum(s["cpu_percent"] for s in samples) / max(1, len(samples)), 1),
        "min_clock_mhz": round(min_clock) if min_clock else None, "max_clock_mhz": round(max_clock) if max_clock else None,
        "throttling_suspected": throttled, "samples": samples[-120:],
    }


def stress_ram(percent_of_free=60, seconds=60, ctx=None, **_):
    available = psutil.virtual_memory().available
    target = int(available * percent_of_free / 100)
    block = 64 * 1024 * 1024
    blocks, allocated = [], 0
    mismatches = 0
    stop_time = time.time() + seconds
    stopped_reason = "completed"
    try:
        while allocated < target:
            pattern = bytes([random.randint(0, 255)]) * block
            try:
                buffer = bytearray(pattern)
            except MemoryError:
                stopped_reason = "memory-limit"
                break
            blocks.append((buffer, pattern[0]))
            allocated += block
            if ctx and ctx.progress({"allocated": human(allocated), "target": human(target)}):
                stopped_reason = "cancelled"
                break
        passes = 0
        while time.time() < stop_time and stopped_reason == "completed":
            for buffer, expected in blocks:
                if buffer[0] != expected or buffer[-1] != expected:
                    mismatches += 1
                mid = len(buffer) // 2
                buffer[mid] = buffer[mid]
            passes += 1
            if ctx and ctx.progress({"verify_pass": passes, "mismatches": mismatches}):
                stopped_reason = "cancelled"
                break
    finally:
        blocks.clear()
    return {"test": "ram", "allocated": human(allocated), "allocated_bytes": allocated, "verify_mismatches": mismatches,
            "stopped_reason": stopped_reason, "healthy": mismatches == 0,
            "note": "verify_mismatches > 0 can indicate faulty RAM; run a dedicated memtest to confirm." if mismatches else None}


def stress_disk(size_mb=1024, random_seconds=10, ctx=None, **_):
    path = os.path.join(tempfile.gettempdir(), f"sentinel_disk_{os.getpid()}.tmp")
    chunk = os.urandom(1024 * 1024)
    total = size_mb * 1024 * 1024
    result = {"test": "disk", "size_mb": size_mb}
    try:
        start = time.perf_counter()
        with open(path, "wb") as handle:
            written = 0
            while written < total:
                handle.write(chunk)
                written += len(chunk)
                if ctx and written % (64 * 1024 * 1024) == 0:
                    ctx.progress({"stage": "write", "mb": written // (1024 * 1024)})
            handle.flush()
            os.fsync(handle.fileno())
        write_seconds = time.perf_counter() - start
        result["write_mbps"] = round(size_mb / write_seconds, 1)

        _drop_caches(path)
        start = time.perf_counter()
        read = 0
        with open(path, "rb") as handle:
            while handle.read(1024 * 1024):
                read += 1
                if ctx and read % 64 == 0:
                    ctx.progress({"stage": "read", "mb": read})
        read_seconds = time.perf_counter() - start
        result["read_mbps"] = round(size_mb / read_seconds, 1)

        iops = _random_iops(path, total, random_seconds, ctx)
        result["random_read_iops"] = iops
    except OSError as error:
        result["error"] = str(error)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    return result


def _random_iops(path, total, seconds, ctx):
    operations = 0
    block = 4096
    count = max(1, total // block)
    stop = time.time() + seconds
    try:
        with open(path, "rb") as handle:
            while time.time() < stop:
                handle.seek(random.randint(0, count - 1) * block)
                handle.read(block)
                operations += 1
                if ctx and operations % 5000 == 0:
                    ctx.progress({"stage": "random", "ops": operations})
    except OSError:
        return None
    return round(operations / seconds)


def _drop_caches(path):
    try:
        if os.name == "posix":
            fd = os.open(path, os.O_RDONLY)
            try:
                os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
            finally:
                os.close(fd)
    except (OSError, AttributeError):
        pass


def temperatures(**_):
    return common_temperatures()
