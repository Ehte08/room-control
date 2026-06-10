import json
import os
import queue
import struct
import sys
import threading
import time
import numpy as np
import sounddevice as sd
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv
from rapidfuzz import process, fuzz
from vosk import Model, KaldiRecognizer
import govee

load_dotenv()

# --- Config ---
API_KEY    = os.environ["GOVEE_API_KEY"]
AC_DEVICE  = "E1:B6:D0:C9:07:A3:95:AE"
AC_MODEL   = "H5082"

BATHROOM_DEVICE      = "1D:8B:98:17:3C:92:1E:98"
STANDING_DEVICE      = "18:32:98:17:3C:93:9E:D8"
STANDING_RICE_DEVICE = "5E:DF:98:17:3C:9A:1C:3A"
FADO_DEVICE          = "54:98:98:17:3C:97:49:DE"
LIGHT_MODEL          = "H6008"

MODEL_PATH      = "vosk-model-en-us-0.22-lgraph"
SAMPLE_RATE     = 16000
CLAP_THRESHOLD  = 13000  # lower = more sensitive; raise if false triggers
CLAP_WINDOW     = 2.0    # max seconds between clap 1 and clap 2
CLAP_CHUNK      = 800    # 50ms at 16000Hz — spike must fit in one chunk to count
COMMAND_TIMEOUT = 5.0    # seconds to speak a command after "Jarvis"
FUZZY_THRESHOLD = 75     # 0–100 min score to auto-execute + learn alias
ALIASES_FILE    = "aliases.json"
GAIN            = 2.0    # mic amplification; raise to 3.0 for distance, lower if distorting


def goodnight(from_mode=None):
    global _current_mode
    bulbs = [STANDING_DEVICE, STANDING_RICE_DEVICE, FADO_DEVICE]
    tasks = []
    if from_mode != "red":
        for d in [*bulbs, BATHROOM_DEVICE]:
            tasks.append((govee.set_color, API_KEY, d, LIGHT_MODEL, 255, 0, 0))
    if from_mode not in ("red", "lounge"):
        for d in [*bulbs, BATHROOM_DEVICE]:
            tasks.append((govee.set_brightness, API_KEY, d, LIGHT_MODEL, 30))
    tasks.append((govee.toggle_outlet, API_KEY, AC_DEVICE, AC_MODEL, 2, True))
    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = [ex.submit(fn, *args) for fn, *args in tasks]
        for f in futures:
            f.result()
    _current_mode = "goodnight"


def lounge(optimized=False):
    global _current_mode
    all_bulbs = [STANDING_DEVICE, STANDING_RICE_DEVICE, FADO_DEVICE, BATHROOM_DEVICE]
    tasks = []
    # optimized=True skips main bulbs brightness (already 30% when coming from red)
    brightness_targets = [BATHROOM_DEVICE] if optimized else all_bulbs
    for d in brightness_targets:
        tasks.append((govee.set_brightness, API_KEY, d, LIGHT_MODEL, 30))
    for d in all_bulbs:
        tasks.append((govee.set_color_temp, API_KEY, d, LIGHT_MODEL, 2700))
    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = [ex.submit(fn, *args) for fn, *args in tasks]
        for f in futures:
            f.result()
    _current_mode = "lounge"


def red_mode():
    global _current_mode
    tasks = [(govee.set_color, API_KEY, d, LIGHT_MODEL, 255, 0, 0)
             for d in [STANDING_DEVICE, STANDING_RICE_DEVICE, FADO_DEVICE, BATHROOM_DEVICE]]
    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = [ex.submit(fn, *args) for fn, *args in tasks]
        for f in futures:
            f.result()
    _current_mode = "red"


def good_morning():
    all_bulbs = [STANDING_DEVICE, STANDING_RICE_DEVICE, FADO_DEVICE, BATHROOM_DEVICE]
    tasks = []
    for d in all_bulbs:
        tasks.append((govee.set_brightness, API_KEY, d, LIGHT_MODEL, 30))
        tasks.append((govee.set_color_temp, API_KEY, d, LIGHT_MODEL, 2700))
    tasks.append((govee.toggle_outlet, API_KEY, AC_DEVICE, AC_MODEL, 2, False))
    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = [ex.submit(fn, *args) for fn, *args in tasks]
        for f in futures:
            f.result()


COMMANDS = {
    "turn the ac on":  lambda: govee.toggle_outlet(API_KEY, AC_DEVICE, AC_MODEL, 2, True),
    "turn on the ac":  lambda: govee.toggle_outlet(API_KEY, AC_DEVICE, AC_MODEL, 2, True),
    "turn the ac off": lambda: govee.toggle_outlet(API_KEY, AC_DEVICE, AC_MODEL, 2, False),
    "turn off the ac": lambda: govee.toggle_outlet(API_KEY, AC_DEVICE, AC_MODEL, 2, False),
    "goodnight":       lambda: goodnight(from_mode=_current_mode),
    "sleep mode":      lambda: goodnight(from_mode=_current_mode),
    "lounge mode":     lambda: lounge(optimized=_current_mode == "red"),
    "good morning":    good_morning,
    "red mode":        red_mode,
}


def _load_aliases() -> dict:
    try:
        with open(ALIASES_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

_aliases = _load_aliases()


def handle(text: str):
    text = text.lower().strip()
    if not text:
        return
    resolved = _aliases.get(text, text)
    for phrase, action in COMMANDS.items():
        if phrase in resolved:
            print(f"[✓] Matched: '{phrase}'")
            action()
            return
    match, score, _ = process.extractOne(resolved, COMMANDS.keys(), scorer=fuzz.partial_ratio)
    if score >= FUZZY_THRESHOLD:
        print(f"[~] Fuzzy matched '{text}' → '{match}' (score {score})")
        COMMANDS[match]()
        if text not in _aliases:
            _aliases[text] = match
            with open(ALIASES_FILE, "w") as f:
                json.dump(_aliases, f, indent=2)
    else:
        print(f"[ ] Heard: '{text}' — no match (best: '{match}' @ {score})")


# --- Clap detection ---
# Runs in a background thread, reading from the same audio stream as Vosk.
# Looks for two loud spikes 0.2–2s apart and triggers goodnight().

_clap_queue     = queue.Queue()
_last_clap_time    = 0.0
_clap_cooldown     = 0.0  # monotonic time before which claps are ignored
_clap_lock         = threading.Lock()
_current_mode      = None  # "red" | "lounge" | None


def _clap_worker():
    global _last_clap_time, _clap_cooldown
    while True:
        data = _clap_queue.get()
        samples = struct.unpack(f"{len(data) // 2}h", data)
        # Split into 50ms chunks — a real clap spikes hard in one chunk,
        # sustained sounds (speech, music) spread energy across all chunks
        chunks = [samples[i:i + CLAP_CHUNK] for i in range(0, len(samples), CLAP_CHUNK)]
        if not any(max(abs(s) for s in c) >= CLAP_THRESHOLD for c in chunks):
            continue
        now = time.monotonic()
        with _clap_lock:
            if now < _clap_cooldown:
                continue
            gap = now - _last_clap_time
            if 0.001 < gap < CLAP_WINDOW:
                _last_clap_time = 0.0
                _clap_cooldown = now + 2.0
                going_lounge = _current_mode == "red"
                target_fn = lounge if going_lounge else red_mode
                lounge_opt = going_lounge and _current_mode in ("red", "lounge")
                print(f"[✓] Double clap → {'lounge' if going_lounge else 'red'} mode")
                def _run_toggle(fn=target_fn, o=lounge_opt):
                    try:
                        fn(optimized=o) if fn is lounge else fn()
                    except Exception as e:
                        print(f"[!] mode error: {e}")
                threading.Thread(target=_run_toggle, daemon=True).start()
            else:
                _last_clap_time = now


def _apply_gain(data: bytes) -> bytes:
    samples = np.frombuffer(data, dtype=np.int16).astype(np.float32)
    samples *= GAIN
    np.clip(samples, -32768, 32767, out=samples)
    return samples.astype(np.int16).tobytes()


def listen():
    model = Model(MODEL_PATH)
    audio_queue: queue.Queue = queue.Queue()

    GRAMMAR = json.dumps(
        ["jarvis"] +
        list(COMMANDS.keys()) +
        ["jarvis " + p for p in COMMANDS.keys()] +
        ["[unk]"]
    )

    def callback(indata, frames, time_info, status):
        if status:
            print(status, file=sys.stderr)
        data = _apply_gain(bytes(indata))
        audio_queue.put(data)
        _clap_queue.put(data)

    def _flush():
        while not audio_queue.empty():
            try:
                audio_queue.get_nowait()
            except queue.Empty:
                break

    threading.Thread(target=_clap_worker, daemon=True).start()
    print("Listening for 'Jarvis'... (Ctrl+C to stop)")

    with sd.RawInputStream(samplerate=SAMPLE_RATE, blocksize=8000,
                           dtype="int16", channels=1, callback=callback):
        wake_heard = False
        deadline   = 0.0
        rec        = KaldiRecognizer(model, SAMPLE_RATE, GRAMMAR)

        while True:
            data = audio_queue.get()

            if wake_heard and time.monotonic() > deadline:
                print("[.] Command window closed")
                wake_heard = False

            partial = json.loads(rec.PartialResult()).get("partial", "").lower()

            if "jarvis" in partial and not wake_heard:
                print("[!] Wake word — go ahead")
                wake_heard = True
                deadline = time.monotonic() + COMMAND_TIMEOUT

            if wake_heard:
                after = partial.split("jarvis", 1)[-1].strip() if "jarvis" in partial else partial
                for phrase in COMMANDS:
                    if phrase in after:
                        print(f"[✓] Matched (early): '{phrase}'")
                        threading.Thread(target=COMMANDS[phrase], daemon=True).start()
                        wake_heard = False
                        rec = KaldiRecognizer(model, SAMPLE_RATE, GRAMMAR)
                        _flush()
                        break

            if rec.AcceptWaveform(data):
                text = json.loads(rec.Result()).get("text", "").lower().strip()
                if "jarvis" in text:
                    if not wake_heard:
                        print("[!] Wake word — go ahead")
                        wake_heard = True
                        deadline = time.monotonic() + COMMAND_TIMEOUT
                    after = text.split("jarvis", 1)[-1].strip()
                    if after:
                        handle(after)
                        wake_heard = False
                elif wake_heard:
                    handle(text)
                    wake_heard = False


if __name__ == "__main__":
    if "--discover" in sys.argv:
        devices = govee.discover_devices(API_KEY)
        if not devices:
            print("No devices found.")
        else:
            print("Devices on your account:")
            for d in devices:
                print(f"  {d['model']:<12} {d['device']}  —  {d['deviceName']}")
            print("\nCopy the device address and model into main.py.")
        sys.exit(0)

    listen()
