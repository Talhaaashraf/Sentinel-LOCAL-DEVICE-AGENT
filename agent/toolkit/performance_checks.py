"""Active, on-demand performance benchmarks.

Unlike collectors.py (which reads passive OS counters), these functions run a
small real workload — writing/reading a temp file, hashing data, opening a TCP
connection — so they measure actual throughput and latency rather than
current utilization. Because they briefly use CPU, disk, and network, they are
meant to be triggered on demand or on an infrequent background cadence, never
on every monitoring tick.
"""

import hashlib
import os
import platform
import socket
import tempfile
import time

DISK_TEST_SIZE_BYTES = 16 * 1024 * 1024
DISK_TEST_CHUNK_BYTES = 1 * 1024 * 1024
CPU_BENCHMARK_SECONDS = 0.5
NETWORK_TIMEOUT_SECONDS = 2
NETWORK_TARGETS = (
    {"name": "Cloudflare DNS", "host": "1.1.1.1", "port": 53},
    {"name": "Google DNS", "host": "8.8.8.8", "port": 53},
)


def benchmark_cpu():
    """Hash a fixed payload as fast as possible for a fixed duration.

    This is a stable, comparable single-core throughput figure — not a
    hardware FLOPS rating — useful for spotting a regression against a
    device's own history (thermal throttling, background load).
    """
    payload = os.urandom(1024)
    operations = 0
    start = time.perf_counter()
    while time.perf_counter() - start < CPU_BENCHMARK_SECONDS:
        hashlib.sha256(payload).digest()
        operations += 1
    elapsed = time.perf_counter() - start
    ops_per_second = round(operations / elapsed) if elapsed else 0

    return {
        "available": True,
        "ops_per_second": ops_per_second,
        "duration_seconds": round(elapsed, 3),
        "detail": f"{ops_per_second:,} SHA-256 hashes/sec (single core, {round(elapsed, 2)}s sample)",
    }


def benchmark_disk():
    """Write then read a temp file to measure real throughput.

    The file lives in the OS temp directory for the duration of the test and
    is always deleted afterward; nothing user-owned is touched.
    """
    chunk = os.urandom(DISK_TEST_CHUNK_BYTES)
    chunk_count = DISK_TEST_SIZE_BYTES // DISK_TEST_CHUNK_BYTES
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, prefix="sentinel_disk_test_") as temp_file:
            temp_path = temp_file.name
            write_start = time.perf_counter()
            for _ in range(chunk_count):
                temp_file.write(chunk)
            temp_file.flush()
            os.fsync(temp_file.fileno())
            write_elapsed = time.perf_counter() - write_start

        read_start = time.perf_counter()
        with open(temp_path, "rb") as temp_file:
            while temp_file.read(DISK_TEST_CHUNK_BYTES):
                pass
        read_elapsed = time.perf_counter() - read_start

        test_size_mb = DISK_TEST_SIZE_BYTES / (1024 * 1024)
        write_mbps = round(test_size_mb / write_elapsed, 2) if write_elapsed else None
        read_mbps = round(test_size_mb / read_elapsed, 2) if read_elapsed else None
        return {
            "available": True,
            "write_mbps": write_mbps,
            "read_mbps": read_mbps,
            "test_size_mb": round(test_size_mb),
            "detail": f"Wrote {write_mbps} MB/s, read {read_mbps} MB/s ({round(test_size_mb)} MB sample)",
        }
    except OSError as error:
        return {"available": False, "write_mbps": None, "read_mbps": None, "detail": f"Disk benchmark failed: {error}"}
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def benchmark_network():
    """Time a raw TCP handshake to a couple of well-known, reliable hosts.

    Uses TCP connect rather than ICMP ping so it works without elevated
    privileges on every platform.
    """
    results = []
    for target in NETWORK_TARGETS:
        started = time.perf_counter()
        try:
            with socket.create_connection((target["host"], target["port"]), timeout=NETWORK_TIMEOUT_SECONDS):
                latency_ms = round((time.perf_counter() - started) * 1000, 1)
            results.append({"name": target["name"], "host": target["host"], "reachable": True, "latency_ms": latency_ms})
        except OSError:
            results.append({"name": target["name"], "host": target["host"], "reachable": False, "latency_ms": None})

    reachable = [target for target in results if target["reachable"]]
    average_latency_ms = round(sum(target["latency_ms"] for target in reachable) / len(reachable), 1) if reachable else None
    detail = (
        f"Average latency {average_latency_ms} ms across {len(reachable)}/{len(results)} reachable targets"
        if reachable
        else "No network targets were reachable"
    )
    return {
        "available": bool(reachable),
        "average_latency_ms": average_latency_ms,
        "targets": results,
        "detail": detail,
    }


def collect_performance_report():
    """Run the full benchmark suite and return a single combined report."""
    return {
        "platform": platform.system(),
        "cpu": benchmark_cpu(),
        "disk": benchmark_disk(),
        "network": benchmark_network(),
    }
