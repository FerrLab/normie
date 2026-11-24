"""WASAPI loopback capture: measures the level of what plays on an output device."""
from __future__ import annotations

import concurrent.futures
import ctypes
import queue
import threading

import numpy as np
import pyaudiowpatch as pyaudio

from normie.config import BLOCK_SECONDS, LOOPBACK_SUFFIX


def discord_output_name(device_id: str | None, devices: list[dict]) -> str:
    """Name of the output device Discord plays on ("" when unknown).

    When Discord is set to "Default", that entry is named like
    "Default (Headphones (Elgato Wave:3))". That device is Discord's own idea of the
    default and may differ from the Windows default PortAudio reports, so the real device
    is read from inside the outer parentheses (this works in any Discord language)."""
    name = next((d.get("name") or "" for d in devices if d.get("id") == device_id), "")
    if device_id == "default":
        name = name[name.index("(") + 1:-1] if "(" in name and name.endswith(")") else ""
    return name.strip()


def _norm_device(name: str) -> str:
    return " ".join(name.removesuffix(LOOPBACK_SUFFIX).split()).casefold()


def match_loopback(output_name: str, loopback_names: list[str]) -> str | None:
    """The loopback device that captures `output_name` (a Discord output device name),
    or None. WASAPI loopback devices are named "<output device> [Loopback]"."""
    want = _norm_device(output_name)
    if not want:
        return None
    for name in loopback_names:
        if _norm_device(name) == want:
            return name
    for name in loopback_names:  # some APIs shorten long device names
        have = _norm_device(name)
        if have and (want.startswith(have) or have.startswith(want)):
            return name
    return None


class LoopbackMeter:
    """Captures, via WASAPI loopback, whatever plays on an output device.

    WASAPI is COM-based and PortAudio only works on threads where COM is initialized:
    opening a stream from any other thread fails with "-9999 Unanticipated host error".
    So every PortAudio call (init, device list, open, close, terminate) runs on one
    dedicated audio thread, and the public methods just hand work to it."""

    def __init__(self, on_block, log):
        self._on_block = on_block
        self._log = log
        self._pa = None
        self._stream = None
        self._devices: list[str] = []
        self._callback_failed = False
        self._jobs: queue.Queue = queue.Queue()
        self._ready = threading.Event()
        threading.Thread(target=self._run, name="audio", daemon=True).start()
        self._ready.wait(10)

    def _run(self) -> None:
        ctypes.windll.ole32.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED, for WASAPI
        try:
            self._pa = pyaudio.PyAudio()
            self._devices = [d["name"] for d in self._pa.get_loopback_device_info_generator()]
        except Exception as exc:
            self._log(f"Couldn't initialize audio: {exc}")
        finally:
            self._ready.set()
        while True:
            job, fut = self._jobs.get()
            if job is None:
                break
            try:
                fut.set_result(job())
            except Exception as exc:
                fut.set_exception(exc)

    def _call(self, job, timeout: float | None = None):
        fut: concurrent.futures.Future = concurrent.futures.Future()
        self._jobs.put((job, fut))
        return fut.result(timeout)

    def loopback_devices(self) -> list[str]:
        return list(self._devices)

    def start(self, name: str = "") -> None:
        self._call(lambda: self._open(name))

    def stop(self) -> None:
        self._call(self._close)

    def terminate(self, timeout: float = 2.5) -> None:
        try:
            self._call(self._shutdown, timeout)
        except Exception:  # driver stuck: main() still exits the process
            pass
        self._jobs.put((None, None))

    # ── everything below runs on the audio thread ──
    def _pick(self, name: str) -> dict:
        devices = list(self._pa.get_loopback_device_info_generator())
        if not devices:
            raise RuntimeError("No WASAPI loopback device found.")
        if name:
            for d in devices:
                if d["name"] == name:
                    return d
            self._log(f"Device '{name}' not found; using the default output.")
        wasapi = self._pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        default_out = self._pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
        for d in devices:
            if default_out["name"] in d["name"]:
                return d
        return devices[0]

    def _open(self, name: str) -> None:
        if self._pa is None:
            raise RuntimeError("audio isn't initialized")
        self._close()
        dev = self._pick(name)
        rate = int(dev["defaultSampleRate"])
        channels = max(1, int(dev["maxInputChannels"]))

        def callback(in_data, frame_count, _time_info, _status):
            try:
                samples = np.frombuffer(in_data, dtype=np.float32)
                if samples.size:
                    self._on_block(float(np.mean(samples * samples)), frame_count / rate)
            except Exception as exc:
                if not self._callback_failed:  # only log the first one
                    self._callback_failed = True
                    self._log(f"Error processing audio: {exc!r}")
            return None, pyaudio.paContinue

        self._stream = self._pa.open(
            format=pyaudio.paFloat32, channels=channels, rate=rate, input=True,
            input_device_index=dev["index"], frames_per_buffer=int(rate * BLOCK_SECONDS),
            stream_callback=callback,
        )
        self._log(f"Listening: {dev['name']} ({rate} Hz, {channels} channels)")

    def _close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    def _shutdown(self) -> None:
        self._close()
        if self._pa is not None:
            self._pa.terminate()
            self._pa = None
