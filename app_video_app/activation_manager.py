import requests
import uuid
import os

API_URL = "https://script.google.com/macros/s/AKfycbw1jI4oqrBNd3FFDW0urshFliVKktaMrgST1fUNl3QsvyK6aMtftBwSq-ndNhhlnMpW/exec"
DEVICE_ID_FILE = os.path.expanduser("~/.auto_video_device_id")

def get_device_id():
    if os.path.exists(DEVICE_ID_FILE):
        with open(DEVICE_ID_FILE, "r") as f:
            return f.read().strip()
    device_id = str(uuid.uuid4())
    with open(DEVICE_ID_FILE, "w") as f:
        f.write(device_id)
    return device_id

def activate_key(key, device_name=""):
    device_id = get_device_id()
    try:
        resp = requests.post(API_URL, json={
            "action": "activate",
            "key": key,
            "device_id": device_id,
            "device_name": device_name
        }, timeout=10)
        data = resp.json()
        return data
    except Exception as e:
        return {"status": "fail", "message": f"Lỗi kết nối: {e}"}

def check_key_status(key):
    device_id = get_device_id()
    try:
        resp = requests.post(API_URL, json={
            "action": "check",
            "key": key,
            "device_id": device_id
        }, timeout=10)
        data = resp.json()
        return data
    except Exception as e:
        return {"status": "fail", "message": f"Lỗi kết nối: {e}"}

def revoke_device(key):
    device_id = get_device_id()
    try:
        resp = requests.post(API_URL, json={
            "action": "revoke",
            "key": key,
            "device_id": device_id
        }, timeout=10)
        data = resp.json()
        return data
    except Exception as e:
        return {"status": "fail", "message": f"Lỗi kết nối: {e}"}
