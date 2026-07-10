import requests
import sys
from PyQt5.QtWidgets import QMessageBox, QApplication, QWidget
from qfluentwidgets import MessageBox

CONFIG_URL = "https://asrtools-update.bkfeng.top"
CURRENT_VERSION = "1.1.0"  # 当前版本号


def check_update(parent):
    try:
        response = requests.get(CONFIG_URL, timeout=10)
        response.raise_for_status()
        payload = response.json()
        config = payload.get('data', payload)
        remote_version = config.get('version')
        if not remote_version or remote_version <= CURRENT_VERSION:
            return None

        return {
            'fource': config.get('force', config.get('fource', False)),
            'update_download_url': config.get('download_url', config.get('update_download_url', '')),
        }
    except Exception:
        return None

def check_internet_connection():
    try:
        requests.get("https://www.baidu.com", timeout=5)
        return True
    except requests.ConnectionError:
        return False