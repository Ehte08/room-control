import time
import requests

BASE_URL = "https://developer-api.govee.com/v1"


def _headers(api_key: str) -> dict:
    return {"Govee-API-Key": api_key}


def discover_devices(api_key: str) -> list[dict]:
    r = requests.get(f"{BASE_URL}/devices", headers=_headers(api_key))
    r.raise_for_status()
    return r.json()["data"]["devices"]


def _control(api_key: str, device: str, model: str, name: str, value):
    payload = {
        "device": device,
        "model": model,
        "cmd": {"name": name, "value": value},
    }
    for attempt in range(3):
        r = requests.put(f"{BASE_URL}/devices/control", json=payload, headers=_headers(api_key))
        if r.status_code == 429:
            time.sleep(10 * (2 ** attempt))
            continue
        r.raise_for_status()
        return
    r.raise_for_status()


def turn_on(api_key: str, device: str, model: str):
    _control(api_key, device, model, "turn", "on")


def turn_off(api_key: str, device: str, model: str):
    _control(api_key, device, model, "turn", "off")


def toggle_outlet(api_key: str, device: str, model: str, outlet: int, on: bool):
    """Control a single outlet on a dual-plug. outlet=1 or 2."""
    value = f"{2 - outlet}_{1 if on else 0}"
    _control(api_key, device, model, "toggle", value)


def set_color(api_key: str, device: str, model: str, r: int, g: int, b: int):
    _control(api_key, device, model, "color", {"r": r, "g": g, "b": b})


def set_brightness(api_key: str, device: str, model: str, brightness: int):
    _control(api_key, device, model, "brightness", brightness)


def set_color_temp(api_key: str, device: str, model: str, kelvin: int):
    _control(api_key, device, model, "colorTem", kelvin)
