#!/usr/bin/env python3
"""Validate effective Compose limits without printing its secret environment values."""
import argparse
import json
import os
import subprocess
import time

LIMITS = {"telegram": 576 * 1024**2, "agent": 896 * 1024**2, "proxy": 64 * 1024**2}


def command(args):
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode:
        raise SystemExit(f"Command failed: {' '.join(args[:3])}; check Docker and deployment configuration")
    return result.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default="deployment/compose.env")
    parser.add_argument("--compose-file", default="compose.yaml")
    parser.add_argument("--override-file", action="append", default=[], help="Additional Compose override; may be repeated")
    parser.add_argument("--config-only", action="store_true")
    parser.add_argument("--sample-seconds", type=int, default=0)
    parser.add_argument("--interval", type=int, default=10)
    args = parser.parse_args()
    if args.sample_seconds < 0 or args.interval < 1:
        parser.error("sampling duration must be nonnegative and interval positive")
    compose = ["docker", "compose", "--env-file", args.env_file, "-f", args.compose_file]
    for override in args.override_file:
        compose.extend(["-f", override])
    compose.extend(["--project-directory", os.getcwd()])
    config = json.loads(command(compose + ["config", "--format", "json"]))
    for service, limit in LIMITS.items():
        spec = config["services"][service]
        assert spec.get("restart") == "unless-stopped", f"{service}: restart policy must be unless-stopped"
        assert int(spec["mem_limit"]) == limit, f"{service}: incorrect memory limit"
        assert int(spec["memswap_limit"]) == limit, f"{service}: swap must be disabled"
        assert spec["read_only"] and spec["init"], f"{service}: missing filesystem/init controls"
        assert "ALL" in spec["cap_drop"], f"{service}: capabilities must be dropped"
        if service != "proxy":
            assert not spec.get("ports"), f"{service}: application ports must remain private"
    assert sum(LIMITS.values()) == 1536 * 1024**2
    print("Compose resource/security configuration passed: 1536 MiB total")
    if args.config_only:
        return
    ids = {}
    for service, limit in LIMITS.items():
        cid = command(compose + ["ps", "-q", service]).strip()
        assert cid, f"{service}: container missing"
        container = json.loads(command(["docker", "inspect", cid]))[0]
        assert container["HostConfig"]["RestartPolicy"]["Name"] == "unless-stopped", f"{service}: live restart policy differs from Compose"
        assert container["HostConfig"]["Memory"] == limit
        assert container["HostConfig"]["MemorySwap"] == limit
        assert container["State"]["Running"] and not container["State"]["OOMKilled"]
        assert container["Config"]["User"] not in ("", "0", "root", "0:0")
        assert container["RestartCount"] == 0, f"{service}: investigate restart history"
        health = container["State"].get("Health")
        assert not health or health["Status"] == "healthy", f"{service}: unhealthy"
        ids[service] = cid
    print("Running container limits, users, health, and restart history passed")
    peaks = dict.fromkeys(LIMITS, 0)
    kernel_peaks = dict.fromkeys(LIMITS, 0)
    deadline = time.monotonic() + args.sample_seconds
    while args.sample_seconds:
        sample = {}
        for service, cid in ids.items():
            values = command(["docker", "exec", cid, "sh", "-c", "cat /sys/fs/cgroup/memory.current; cat /sys/fs/cgroup/memory.peak; cat /sys/fs/cgroup/memory.events"]).splitlines()
            current = int(values[0])
            kernel_peaks[service] = max(kernel_peaks[service], int(values[1]))
            events = dict(line.split() for line in values[2:])
            assert int(events.get("oom_kill", 0)) == 0, f"{service}: OOM kill observed"
            peaks[service] = max(peaks[service], current)
            sample[service] = round(current / 1024**2, 1)
        print(json.dumps({"memory_mib": sample}), flush=True)
        if time.monotonic() >= deadline:
            break
        time.sleep(min(args.interval, max(0, deadline - time.monotonic())))
    if args.sample_seconds:
        print(json.dumps({"peak_sampled_mib": {k: round(v / 1024**2, 1) for k, v in peaks.items()}}))
        print(json.dumps({"kernel_peak_mib": {k: round(v / 1024**2, 1) for k, v in kernel_peaks.items()}}))
        for service, cid in ids.items():
            state = json.loads(command(["docker", "inspect", cid]))[0]
            assert state["RestartCount"] == 0 and state["State"]["Running"] and not state["State"]["OOMKilled"], f"{service}: restart/exit during soak"
        for service, peak in kernel_peaks.items():
            assert peak <= LIMITS[service] * 0.85, f"{service}: less than 15% memory headroom"


if __name__ == "__main__":
    main()
