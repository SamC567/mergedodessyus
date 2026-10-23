"""System health for the Jarvis status panel and the system_status tool.

Read-only probes with short timeouts: model endpoints (latency, models,
llama.cpp context size), ChromaDB, RAM, disk and - on Windows - GPU VRAM via
performance counters. No third-party dependencies beyond httpx.
"""

from __future__ import annotations

import ctypes
import os
import platform
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional

import httpx

from src.constants import DATA_DIR

PROBE_TIMEOUT = 2.5
_VRAM_CACHE: Dict[str, Any] = {"at": 0.0, "value": None}
_VRAM_TTL = 30.0


def _gb(n: Optional[float]) -> Optional[float]:
    return None if n is None else round(n / 1024 ** 3, 1)


def memory() -> Dict[str, Any]:
    total = avail = None
    try:
        if os.name == "nt":
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                total, avail = stat.ullTotalPhys, stat.ullAvailPhys
        elif os.path.exists("/proc/meminfo"):
            info = {}
            with open("/proc/meminfo", encoding="utf-8") as f:
                for line in f:
                    key, _, rest = line.partition(":")
                    info[key] = int(rest.strip().split()[0]) * 1024
            total, avail = info.get("MemTotal"), info.get("MemAvailable")
    except Exception:
        pass
    if not total:
        return {"ok": None, "note": "RAM not readable on this platform"}
    free_gb = _gb(avail)
    return {"ok": free_gb is None or free_gb >= 3, "total_gb": _gb(total), "free_gb": free_gb,
            "used_pct": round(100 * (1 - avail / total)) if avail else None}


def disk() -> Dict[str, Any]:
    try:
        usage = shutil.disk_usage(DATA_DIR)
    except Exception as e:
        return {"ok": None, "note": f"disk not readable: {e}"}
    free_gb = _gb(usage.free)
    return {"ok": free_gb >= 10, "path": DATA_DIR, "total_gb": _gb(usage.total), "free_gb": free_gb}


def vram() -> Dict[str, Any]:
    """Dedicated GPU memory in use (Windows counters or nvidia-smi), cached 30s."""
    now = time.monotonic()
    if _VRAM_CACHE["value"] is not None and now - _VRAM_CACHE["at"] < _VRAM_TTL:
        return _VRAM_CACHE["value"]
    value: Dict[str, Any] = {"ok": None, "note": "VRAM not readable"}
    try:
        if shutil.which("nvidia-smi"):
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=4).stdout.strip().splitlines()
            if out:
                used, total = (float(x) for x in out[0].split(","))
                value = {"ok": used / total < 0.97, "used_mib": int(used), "total_mib": int(total)}
        elif os.name == "nt":
            ps = ("(Get-Counter '\\GPU Adapter Memory(*)\\Dedicated Usage').CounterSamples | "
                  "Measure-Object CookedValue -Maximum | Select-Object -ExpandProperty Maximum")
            out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                                 capture_output=True, text=True, timeout=6,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip()
            if out:
                value = {"ok": None, "used_mib": int(float(out) / 1024 ** 2),
                         "note": "dedicated usage across all apps; total not exposed"}
    except Exception as e:
        value = {"ok": None, "note": f"VRAM probe failed: {type(e).__name__}"}
    _VRAM_CACHE.update(at=now, value=value)
    return value


def _probe_endpoint(name: str, base_url: str) -> Dict[str, Any]:
    base = base_url.rstrip("/")
    root = base[:-3] if base.endswith("/v1") else base
    out: Dict[str, Any] = {"name": name, "url": base}
    started = time.monotonic()
    try:
        with httpx.Client(timeout=PROBE_TIMEOUT) as client:
            r = client.get(base + "/models")
            out["latency_ms"] = int((time.monotonic() - started) * 1000)
            out["ok"] = r.status_code == 200
            if r.status_code == 200:
                ids = [m.get("id") for m in (r.json().get("data") or []) if isinstance(m, dict)]
                out["models"] = ids[:5]
            else:
                out["note"] = f"HTTP {r.status_code}"
            try:  # llama.cpp extras: context size and busy slots
                s = client.get(root + "/slots")
                if s.status_code == 200 and isinstance(s.json(), list) and s.json():
                    slots = s.json()
                    out["n_ctx"] = slots[0].get("n_ctx")
                    out["busy_slots"] = sum(1 for x in slots if x.get("is_processing"))
            except Exception:
                pass
    except Exception as e:
        out.update(ok=False, note=f"unreachable ({type(e).__name__})")
    return out


def model_endpoints(owner: Optional[str]) -> List[Dict[str, Any]]:
    from core.database import SessionLocal, ModelEndpoint
    db = SessionLocal()
    try:
        q = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True)  # noqa: E712
        rows = [(e.name or e.base_url, e.base_url, getattr(e, "endpoint_kind", None)) for e in q.all()]
    except Exception:
        rows = []
    finally:
        db.close()
    # Only probe local/self-hosted endpoints: no traffic to cloud providers.
    local = [r for r in rows if r[2] == "local" or any(h in (r[1] or "") for h in ("127.0.0.1", "localhost"))]
    return [_probe_endpoint(name, url) for name, url, _ in local[:4]]


def chroma() -> Dict[str, Any]:
    host = os.getenv("CHROMADB_HOST", "localhost")
    port = os.getenv("CHROMADB_PORT", "8100")
    try:
        with httpx.Client(timeout=PROBE_TIMEOUT) as client:
            r = client.get(f"http://{host}:{port}/api/v2/heartbeat")
        return {"ok": r.status_code == 200, "url": f"http://{host}:{port}"}
    except Exception as e:
        return {"ok": False, "url": f"http://{host}:{port}", "note": f"unreachable ({type(e).__name__})"}


def default_model() -> Dict[str, Any]:
    try:
        from src.settings import load_settings
        s = load_settings()
        return {"model": s.get("default_model"), "fallbacks": s.get("default_model_fallbacks") or [],
                "utility_model": s.get("utility_model") or None}
    except Exception:
        return {}


def snapshot(owner: Optional[str] = None) -> Dict[str, Any]:
    endpoints = model_endpoints(owner)
    parts = {
        "llm": endpoints,
        "chroma": chroma(),
        "ram": memory(),
        "disk": disk(),
        "vram": vram(),
    }
    problems = []
    if endpoints and not any(e.get("ok") for e in endpoints):
        problems.append("no local model endpoint is responding")
    if not endpoints:
        problems.append("no local model endpoint configured")
    if parts["chroma"].get("ok") is False:
        problems.append("ChromaDB is down (memory search and tool index degraded)")
    if parts["ram"].get("ok") is False:
        problems.append(f"low RAM: {parts['ram'].get('free_gb')} GB free")
    if parts["disk"].get("ok") is False:
        problems.append(f"low disk: {parts['disk'].get('free_gb')} GB free")
    return {
        **parts,
        "routing": default_model(),
        "platform": f"{platform.system()} {platform.release()}",
        "ok": not problems,
        "problems": problems,
    }


def as_text(snap: Dict[str, Any]) -> str:
    lines = ["All systems OK." if snap["ok"] else "Problems: " + "; ".join(snap["problems"])]
    for e in snap["llm"]:
        bits = [("up" if e.get("ok") else "DOWN")]
        if e.get("latency_ms") is not None:
            bits.append(f"{e['latency_ms']} ms")
        if e.get("models"):
            bits.append(", ".join(m for m in e["models"] if m))
        if e.get("n_ctx"):
            bits.append(f"ctx {e['n_ctx']}")
        if e.get("busy_slots"):
            bits.append(f"{e['busy_slots']} busy")
        if e.get("note"):
            bits.append(e["note"])
        lines.append(f"- LLM {e['name']}: " + ", ".join(bits))
    c = snap["chroma"]
    lines.append(f"- ChromaDB: {'up' if c.get('ok') else 'DOWN'}")
    r = snap["ram"]
    if r.get("total_gb"):
        lines.append(f"- RAM: {r['free_gb']} GB free of {r['total_gb']} GB ({r['used_pct']}% used)")
    d = snap["disk"]
    if d.get("total_gb"):
        lines.append(f"- Disk: {d['free_gb']} GB free of {d['total_gb']} GB")
    v = snap["vram"]
    if v.get("used_mib") is not None:
        total = f" of {v['total_mib']} MiB" if v.get("total_mib") else ""
        lines.append(f"- GPU VRAM in use: {v['used_mib']} MiB{total}")
    routing = snap.get("routing") or {}
    if routing.get("model"):
        lines.append(f"- Default model: {routing['model']}")
    return "\n".join(lines)
