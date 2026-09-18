#!/usr/bin/env python3
"""Read-only Proxmox collector. It writes one atomic JSON snapshot and exposes no socket."""

import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

OUTPUT = Path(os.environ.get("PVE_LENS_OUTPUT", "/run/pve-lens/snapshot.json"))
INTERVAL = float(os.environ.get("PVE_LENS_INTERVAL", "5"))
TOPOLOGY_INTERVAL = float(os.environ.get("PVE_LENS_TOPOLOGY_INTERVAL", "60"))
HARDWARE_INTERVAL = float(os.environ.get("PVE_LENS_HARDWARE_INTERVAL", "15"))
ROTATIONAL_STATUS = Path(os.environ.get("PVE_LENS_ROTATIONAL_STATUS", "/run/pve-lens/rotational-status"))
ROTATIONAL_TEMP_KEY = os.environ.get("PVE_LENS_ROTATIONAL_TEMP_KEY", "temperature")
ROTATIONAL_USAGE_KEY = os.environ.get("PVE_LENS_ROTATIONAL_USAGE_KEY", "usage_percent")
VMID_RE = re.compile(r"(?:vm|base)-(\d+)-disk-")
QEMU_DISK_RE = re.compile(r"^(?:ide|sata|scsi|virtio)\d+$")
LXC_MOUNT_RE = re.compile(r"^mp\d+$")


def run_json(command):
    result = subprocess.run(command, text=True, capture_output=True, check=True, timeout=20)
    return json.loads(result.stdout)


def number(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0


def properties(value):
    """Parse Proxmox's comma-separated key/value configuration strings."""
    result = {"source": ""}
    for index, part in enumerate(str(value or "").split(",")):
        if "=" in part:
            key, item = part.split("=", 1)
            result[key] = item
        elif index == 0:
            result["source"] = part
    return result


def configured_size(value):
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([KMGTPE]?)\s*", str(value or ""), re.IGNORECASE)
    if not match:
        return 0
    units = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4, "P": 1024**5, "E": 1024**6}
    return float(match.group(1)) * units[match.group(2).upper()]


def runtime_addresses(node, guest_type, vmid, config):
    if guest_type == "lxc":
        endpoint = ["pvesh", "get", f"/nodes/{node}/lxc/{vmid}/interfaces", "--output-format", "json"]
    elif guest_type == "qemu" and str(config.get("agent", "0")).split(",", 1)[0] in {"1", "enabled=1"}:
        endpoint = ["pvesh", "get", f"/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces", "--output-format", "json"]
    else:
        return []
    try:
        interfaces = run_json(endpoint)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return []
    addresses = []
    for interface in interfaces if isinstance(interfaces, list) else []:
        for address in interface.get("ip-addresses", []):
            value = address.get("ip-address")
            if address.get("ip-address-type") == "inet" and value and not value.startswith("127."):
                addresses.append(value)
        direct = interface.get("inet")
        if direct and not direct.startswith("127."):
            addresses.append(direct.split("/", 1)[0])
    return list(dict.fromkeys(addresses))


def guest_configuration(resource):
    guest_type = resource.get("type", "")
    vmid = int(resource.get("vmid", 0))
    node = resource.get("node", socket.gethostname())
    try:
        config = run_json(["pvesh", "get", f"/nodes/{node}/{guest_type}/{vmid}/config", "--output-format", "json"])
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        config = {}

    disks = []
    for key, raw in config.items():
        is_lxc_disk = guest_type == "lxc" and (key == "rootfs" or LXC_MOUNT_RE.fullmatch(key))
        is_qemu_disk = guest_type == "qemu" and QEMU_DISK_RE.fullmatch(key)
        if not (is_lxc_disk or is_qemu_disk):
            continue
        parsed = properties(raw)
        if parsed.get("media") == "cdrom" or parsed.get("source") in {"none", "cloudinit"}:
            continue
        disks.append({
            "name": key,
            "source": parsed.get("source", ""),
            "size": configured_size(parsed.get("size")),
            "mountpoint": "/" if key == "rootfs" else parsed.get("mp"),
        })

    networks = []
    configured_ips = []
    for key, raw in config.items():
        if not re.fullmatch(r"net\d+", key):
            continue
        parsed = properties(raw)
        configured_ip = parsed.get("ip")
        if configured_ip and configured_ip != "dhcp":
            configured_ips.append(configured_ip.split("/", 1)[0])
        networks.append({
            "name": parsed.get("name") or key,
            "bridge": parsed.get("bridge"),
            "mac": parsed.get("hwaddr") or parsed.get("virtio") or parsed.get("e1000") or parsed.get("source"),
            "configuredIp": configured_ip,
        })

    runtime_ips = runtime_addresses(node, guest_type, vmid, config) if resource.get("status") == "running" else []
    sockets = max(1, int(number(config.get("sockets")) or 1))
    cores = int(number(config.get("cores")) or number(resource.get("maxcpu")))
    return {
        "cpuAllocated": cores * sockets if guest_type == "qemu" else cores,
        "memoryAllocated": number(config.get("memory")) * 1024**2,
        "swapAllocated": number(config.get("swap")) * 1024**2 if guest_type == "lxc" else 0,
        "onboot": bool(number(config.get("onboot"))),
        "ipAddresses": list(dict.fromkeys(runtime_ips + configured_ips)),
        "disks": disks,
        "networks": networks,
    }


def rotational_status():
    values = {}
    try:
        for line in ROTATIONAL_STATUS.read_text().splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                values[key.strip()] = value.strip()
    except OSError:
        pass
    raw_temperature = values.get(ROTATIONAL_TEMP_KEY, "")
    match = re.search(r"-?\d+(?:\.\d+)?", raw_temperature)
    temperature = float(match.group(0)) if match else None
    usage_match = re.search(r"\d+(?:\.\d+)?", values.get(ROTATIONAL_USAGE_KEY, ""))
    usage_percent = float(usage_match.group(0)) if usage_match else None
    sleeping = raw_temperature.upper() in {"SLEEP", "SLEEPING", "STANDBY"} or temperature is None
    return {"temperature": temperature, "usagePercent": usage_percent, "state": "sleeping" if sleeping else "active"}


def mountpoints_for(device):
    points = {point for point in (device.get("mountpoints") or []) if point}
    for child in device.get("children") or []:
        points.update(mountpoints_for(child))
    return points


def decode_mount(value):
    return value.replace("\\040", " ").replace("\\011", "\t").replace("\\134", "\\")


def df_rows(excluded_targets=None, cached_rows=None):
    excluded = {os.path.realpath(target) for target in (excluded_targets or set())}
    cached = {os.path.realpath(row["target"]): row for row in (cached_rows or [])}
    rows = []
    seen = set()
    for line in Path("/proc/self/mounts").read_text().splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        source, target = decode_mount(parts[0]), decode_mount(parts[1])
        real_target = os.path.realpath(target)
        if real_target in seen:
            continue
        seen.add(real_target)
        if real_target in excluded:
            if real_target in cached:
                rows.append(cached[real_target])
            continue
        try:
            stats = os.statvfs(target)
        except OSError:
            continue
        size = stats.f_blocks * stats.f_frsize
        available = stats.f_bavail * stats.f_frsize
        used = (stats.f_blocks - stats.f_bfree) * stats.f_frsize
        rows.append({"source": source, "target": target, "size": size, "used": used, "available": available, "percent": used / size * 100 if size else 0})
    return rows


def disk_counters(names):
    counters = {}
    for name in names:
        try:
            fields = Path(f"/sys/class/block/{name}/stat").read_text().split()
            counters[name] = {"read": int(fields[2]) * 512, "write": int(fields[6]) * 512, "busyMs": int(fields[9])}
        except (OSError, ValueError, IndexError):
            counters[name] = {"read": 0, "write": 0, "busyMs": 0}
    return counters


def host_sample():
    cpu_lines = Path("/proc/stat").read_text().splitlines()
    cpu_fields = [int(value) for value in cpu_lines[0].split()[1:]]
    cpu_cores = {}
    for line in cpu_lines[1:]:
        fields = line.split()
        if not fields or not re.fullmatch(r"cpu\d+", fields[0]):
            continue
        values = [int(value) for value in fields[1:]]
        cpu_cores[fields[0]] = {"total": sum(values), "idle": values[3] + values[4]}
    memory = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        memory[key] = int(value.split()[0]) * 1024
    interfaces = {}
    for interface_path in Path("/sys/class/net").iterdir():
        if not (interface_path / "device").exists():
            continue
        try:
            speed = number((interface_path / "speed").read_text().strip())
        except OSError:
            speed = 0
        interfaces[interface_path.name] = {
            "status": (interface_path / "operstate").read_text().strip(),
            "speedMbps": speed,
            "received": number((interface_path / "statistics/rx_bytes").read_text().strip()),
            "transmitted": number((interface_path / "statistics/tx_bytes").read_text().strip()),
        }
    load_1, load_5, load_15 = os.getloadavg()
    frequencies = []
    for frequency_path in Path("/sys/devices/system/cpu").glob("cpu[0-9]*/cpufreq/scaling_cur_freq"):
        try:
            frequencies.append(number(frequency_path.read_text().strip()) / 1_000_000)
        except OSError:
            continue
    return {
        "cpuTotal": sum(cpu_fields), "cpuIdle": cpu_fields[3] + cpu_fields[4],
        "cpuCores": cpu_cores,
        "memoryTotal": memory.get("MemTotal", 0),
        "memoryUsed": memory.get("MemTotal", 0) - memory.get("MemAvailable", 0),
        "uptime": float(Path("/proc/uptime").read_text().split()[0]),
        "cpuCount": os.cpu_count() or 1,
        "load1": load_1, "load5": load_5, "load15": load_15,
        "cpuFrequencyGhz": sum(frequencies) / len(frequencies) if frequencies else None,
        "interfaces": interfaces,
    }


def hardware_sample(physical, rotational):
    cpu_temperature = None
    memory_temperatures = []
    for hardware_path in Path("/sys/class/hwmon").glob("hwmon*"):
        try:
            hardware_name = (hardware_path / "name").read_text().strip()
            if hardware_name == "k10temp":
                cpu_temperature = number((hardware_path / "temp1_input").read_text().strip()) / 1000
            elif hardware_name == "spd5118":
                memory_temperatures.extend(
                    number(sensor.read_text().strip()) / 1000
                    for sensor in hardware_path.glob("temp*_input")
                )
        except OSError:
            continue
    memory_temperature = max(memory_temperatures) if memory_temperatures else None

    ssds = []
    for disk in physical:
        if disk.get("tran") != "nvme":
            continue
        path = disk.get("path") or f'/dev/{disk.get("name")}'
        try:
            result = subprocess.run(["smartctl", "-a", "-j", path], text=True, capture_output=True, timeout=8)
            report = json.loads(result.stdout)
            health = report.get("nvme_smart_health_information_log", {})
            ssds.append({
                "name": disk.get("name"), "model": (disk.get("model") or disk.get("name")).strip(),
                "passed": bool(report.get("smart_status", {}).get("passed", False)),
                "temperature": number(health.get("temperature") or report.get("temperature", {}).get("current")),
                "lifeUsedPercent": number(health.get("percentage_used")),
                "availableSparePercent": number(health.get("available_spare")),
                "mediaErrors": number(health.get("media_errors")),
            })
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            ssds.append({"name": disk.get("name"), "model": (disk.get("model") or disk.get("name")).strip(), "passed": False, "temperature": 0, "lifeUsedPercent": 0, "availableSparePercent": 0, "mediaErrors": 0})

    gpu = None
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,temperature.gpu,utilization.gpu,memory.used,memory.total,power.draw,fan.speed", "--format=csv,noheader,nounits"],
            text=True, capture_output=True, check=True, timeout=8,
        )
        values = [value.strip() for value in result.stdout.splitlines()[0].split(",")]
        gpu = {"name": values[0], "temperature": number(values[1]), "utilization": number(values[2]), "memoryUsedMb": number(values[3]), "memoryTotalMb": number(values[4]), "powerWatts": number(values[5]), "fanPercent": number(values[6])}
    except (OSError, subprocess.SubprocessError, IndexError):
        pass
    return {"cpuTemperature": cpu_temperature, "memoryTemperature": memory_temperature, "hddTemperature": rotational["temperature"], "hddState": rotational["state"], "ssds": ssds, "gpu": gpu}


def rate(current, previous, elapsed):
    if not previous or elapsed <= 0:
        return 0
    return max(0, (current - previous) / elapsed)


def mount_for(source, rows):
    candidates = {source, os.path.realpath(source)}
    for row in rows:
        if row["source"] in candidates or os.path.realpath(row["source"]) in candidates:
            return row
    return None


def collect(previous=None):
    now = time.monotonic()
    elapsed = now - previous["time"] if previous else INTERVAL
    external_rotational = rotational_status()
    hdd_awake = external_rotational["state"] == "active"
    resources = run_json(["pvesh", "get", "/cluster/resources", "--type", "vm", "--output-format", "json"])
    resource_keys = {f'{item.get("type")}/{item.get("vmid")}' for item in resources}
    known_guest_keys = set(previous.get("topology", {}).get("guestConfigs", {})) if previous else set()
    refresh_topology = not previous or resource_keys != known_guest_keys or now - previous.get("topologyTime", 0) >= TOPOLOGY_INTERVAL or (hdd_awake and not previous.get("hddAwake", False))
    if refresh_topology:
        lsblk = run_json(["lsblk", "-J", "-b", "-o", "NAME,PATH,TYPE,SIZE,MODEL,SERIAL,TRAN,ROTA,FSTYPE,MOUNTPOINTS,PKNAME"])
        physical = [item for item in lsblk.get("blockdevices", []) if item.get("type") == "disk"]
        sleeping_hdd_mounts = set()
        if not hdd_awake:
            for item in physical:
                if str(item.get("rota", "")).lower() in ("1", "true", "yes"):
                    sleeping_hdd_mounts.update(mountpoints_for(item))
        cached_filesystems = previous.get("topology", {}).get("filesystems", []) if previous else []
        filesystems = df_rows(sleeping_hdd_mounts, cached_filesystems)
        try:
            lvs = run_json(["lvs", "--reportformat", "json", "--units", "b", "--nosuffix", "-o", "lv_name,lv_path,lv_size,data_percent,pool_lv,vg_name,devices"])["report"][0]["lv"]
            pvs = run_json(["pvs", "--reportformat", "json", "-o", "pv_name,vg_name"])["report"][0]["pv"]
        except (subprocess.SubprocessError, KeyError, json.JSONDecodeError):
            lvs = []
            pvs = []
        with ThreadPoolExecutor(max_workers=min(6, max(1, len(resources)))) as executor:
            configurations = executor.map(guest_configuration, resources)
            guest_configs = {
                f'{guest.get("type")}/{guest.get("vmid")}': configuration
                for guest, configuration in zip(resources, configurations)
            }
        topology = {"physical": physical, "filesystems": filesystems, "lvs": lvs, "pvs": pvs, "guestConfigs": guest_configs}
        topology_time = now
    else:
        topology = previous["topology"]
        topology_time = previous["topologyTime"]
        physical = topology["physical"]
        filesystems = topology["filesystems"]
        lvs = topology["lvs"]
        pvs = topology["pvs"]
        guest_configs = topology.get("guestConfigs", {})
    refresh_hardware = not previous or now - previous.get("hardwareTime", 0) >= HARDWARE_INTERVAL
    if refresh_hardware:
        hardware = hardware_sample(physical, external_rotational)
        hardware_time = now
    else:
        hardware = previous["hardware"]
        hardware_time = previous["hardwareTime"]
    counters = disk_counters([item["name"] for item in physical])
    thin_pools = {lv.get("pool_lv") for lv in lvs if lv.get("pool_lv")}
    host = host_sample()

    old_disks = previous.get("disks", {}) if previous else {}
    old_guests = previous.get("guests", {}) if previous else {}
    guests = []
    for guest in sorted(resources, key=lambda item: int(item.get("vmid", 0))):
        key = f'{guest.get("type")}/{guest.get("vmid")}'
        prior = old_guests.get(key, {})
        configuration = guest_configs.get(key, {})
        guests.append({
            "vmid": int(guest.get("vmid", 0)), "name": guest.get("name") or key,
            "type": guest.get("type", "guest"), "status": guest.get("status", "unknown"),
            "cpu": number(guest.get("cpu")), "mem": number(guest.get("mem")), "maxmem": number(guest.get("maxmem")),
            "disk": number(guest.get("disk")), "maxdisk": number(guest.get("maxdisk")),
            "readRate": rate(number(guest.get("diskread")), prior.get("read"), elapsed),
            "writeRate": rate(number(guest.get("diskwrite")), prior.get("write"), elapsed),
            "receiveRate": rate(number(guest.get("netin")), prior.get("received"), elapsed),
            "transmitRate": rate(number(guest.get("netout")), prior.get("transmitted"), elapsed),
            **configuration,
        })

    disks = []
    warnings = []
    for item in physical:
        name = item["name"]
        size = number(item.get("size"))
        rotational = str(item.get("rota", "")).lower() in ("1", "true", "yes")
        disk_fs = None
        volumes = []
        children = item.get("children") or []
        device_names = {name} | {child.get("name", "") for child in children}
        disk_vgs = {pv.get("vg_name") for pv in pvs if any(device and device in (pv.get("pv_name") or "") for device in device_names)}

        for child in children:
            if child.get("fstype") and child.get("fstype") != "LVM2_member":
                row = mount_for(child.get("path", ""), filesystems)
                child_mountpoints = [point for point in (child.get("mountpoints") or []) if point]
                if row is None and rotational and external_rotational["usagePercent"] is not None and child_mountpoints:
                    cached_percent = external_rotational["usagePercent"]
                    row = {"target": child_mountpoints[0], "used": number(child.get("size")) * cached_percent / 100, "percent": cached_percent}
                volumes.append({"name": child.get("name"), "path": child.get("path"), "size": number(child.get("size")), "used": row["used"] if row else 0, "usagePercent": row["percent"] if row else 0, "kind": child.get("fstype"), "mountpoint": row["target"] if row else None})
                if row and row["target"].startswith("/mnt/"):
                    disk_fs = row

        row = mount_for(item.get("path", ""), filesystems)
        if row:
            disk_fs = row
            volumes.append({"name": Path(row["target"]).name or "root", "path": item.get("path"), "size": row["size"], "used": row["used"], "usagePercent": row["percent"], "kind": item.get("fstype") or "filesystem", "mountpoint": row["target"]})

        for lv in lvs:
            devices = lv.get("devices", "") or ""
            if lv.get("vg_name") not in disk_vgs and not any(device and device in devices for device in device_names):
                continue
            lv_path = lv.get("lv_path") or f'/dev/{lv.get("vg_name")}/{lv.get("lv_name")}'
            lv_size = number(str(lv.get("lv_size", "0")).strip())
            data_percent = number(str(lv.get("data_percent", "0")).strip()) if lv.get("data_percent") not in (None, "") else None
            lv_mount = mount_for(lv_path, filesystems)
            match = VMID_RE.search(lv.get("lv_name", ""))
            kind = "thin volume" if lv.get("pool_lv") else ("thin pool" if lv.get("lv_name") in thin_pools else "LVM")
            volumes.append({"name": lv.get("lv_name"), "path": lv_path, "size": lv_size, "used": lv_mount["used"] if lv_mount else None, "dataPercent": data_percent, "usagePercent": lv_mount["percent"] if lv_mount else data_percent, "kind": kind, "vmid": int(match.group(1)) if match else None, "mountpoint": lv_mount["target"] if lv_mount else None})

        if disk_fs is None and disk_vgs:
            top_level_used = sum(
                (volume.get("used") if volume.get("used") is not None else volume["size"] * (volume.get("dataPercent") or 0) / 100)
                for volume in volumes
                if volume["kind"] in ("LVM", "thin pool")
            )
            disk_fs = {"target": "LVM", "used": top_level_used, "percent": top_level_used / size * 100 if size else 0}

        current = counters.get(name, {})
        prior = old_disks.get(name, {})
        busy_percent = min(100, rate(current.get("busyMs", 0), prior.get("busyMs"), elapsed) / 10)
        disks.append({"name": name, "path": item.get("path") or f"/dev/{name}", "model": (item.get("model") or name).strip(), "serial": item.get("serial"), "transport": item.get("tran"), "rotational": rotational, "size": size, "mountpoint": disk_fs["target"] if disk_fs else None, "used": disk_fs["used"] if disk_fs else None, "usagePercent": disk_fs["percent"] if disk_fs else None, "readRate": rate(current.get("read", 0), prior.get("read"), elapsed), "writeRate": rate(current.get("write", 0), prior.get("write"), elapsed), "busyPercent": busy_percent, "volumes": volumes})
        if disk_fs and disk_fs["percent"] >= 85:
            warnings.append(f'{disk_fs["target"]} is {disk_fs["percent"]:.0f}% full')

    old_host = previous.get("host", {}) if previous else {}
    cpu_delta = host["cpuTotal"] - old_host.get("cpuTotal", host["cpuTotal"])
    idle_delta = host["cpuIdle"] - old_host.get("cpuIdle", host["cpuIdle"])
    host_cpu = max(0, min(1, 1 - idle_delta / cpu_delta)) if cpu_delta > 0 else 0
    old_cores = old_host.get("cpuCores", {})
    core_usage = []
    for name, current in host["cpuCores"].items():
        prior = old_cores.get(name, current)
        total_delta = current["total"] - prior.get("total", current["total"])
        idle_delta = current["idle"] - prior.get("idle", current["idle"])
        if total_delta > 0:
            core_usage.append(max(0, min(1, 1 - idle_delta / total_delta)))
    busiest_core = max(core_usage, default=0)
    interfaces = []
    for name, interface in host["interfaces"].items():
        prior = old_host.get("interfaces", {}).get(name, {})
        receive_rate = rate(interface["received"], prior.get("received"), elapsed)
        transmit_rate = rate(interface["transmitted"], prior.get("transmitted"), elapsed)
        capacity = interface["speedMbps"] * 1_000_000 / 8
        load_percent = min(100, max(receive_rate, transmit_rate) / capacity * 100) if capacity > 0 else 0
        interfaces.append({
            "name": name, "status": interface["status"], "speedMbps": interface["speedMbps"],
            "received": interface["received"], "transmitted": interface["transmitted"],
            "receiveRate": receive_rate, "transmitRate": transmit_rate, "loadPercent": load_percent,
        })
    receive_rate = sum(interface["receiveRate"] for interface in interfaces if interface["status"] == "up")
    transmit_rate = sum(interface["transmitRate"] for interface in interfaces if interface["status"] == "up")
    network_load = max((interface["loadPercent"] for interface in interfaces if interface["status"] == "up"), default=0)
    snapshot = {"generatedAt": datetime.now(timezone.utc).isoformat(), "node": {"name": socket.gethostname(), "cpu": host_cpu, "busiestCore": busiest_core, "cpuFrequencyGhz": host["cpuFrequencyGhz"], "memoryUsed": host["memoryUsed"], "memoryTotal": host["memoryTotal"], "uptime": host["uptime"], "cpuCount": host["cpuCount"], "load1": host["load1"], "load5": host["load5"], "load15": host["load15"], "interfaces": interfaces}, "totals": {"readRate": sum(d["readRate"] for d in disks), "writeRate": sum(d["writeRate"] for d in disks), "diskBusyPercent": max((d["busyPercent"] for d in disks), default=0), "receiveRate": receive_rate, "transmitRate": transmit_rate, "networkLoadPercent": network_load}, "hardware": hardware, "disks": disks, "guests": guests, "warnings": warnings}
    state = {"time": now, "topology": topology, "topologyTime": topology_time, "hardware": hardware, "hardwareTime": hardware_time, "hddAwake": hdd_awake, "host": host, "disks": {name: counters[name] for name in counters}, "guests": {f'{guest.get("type")}/{guest.get("vmid")}': {"read": number(guest.get("diskread")), "write": number(guest.get("diskwrite")), "received": number(guest.get("netin")), "transmitted": number(guest.get("netout"))} for guest in resources}}
    return snapshot, state


def write_atomic(snapshot):
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="snapshot-", suffix=".json", dir=OUTPUT.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(snapshot, handle, separators=(",", ":"))
        os.chmod(temporary, 0o644)
        os.replace(temporary, OUTPUT)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    once = "--once" in sys.argv
    state = None
    while True:
        started = time.monotonic()
        try:
            snapshot, state = collect(state)
            write_atomic(snapshot)
        except Exception as error:
            print(f"collector error: {error}", file=sys.stderr, flush=True)
        if once:
            return
        time.sleep(max(0.2, INTERVAL - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
