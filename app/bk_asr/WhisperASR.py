import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

import requests

from .ASRData import ASRDataSeg
from .BaseASR import BaseASR

DEFAULT_MODEL = 'base'
DEFAULT_API_MODEL = 'whisper-1'


def _get_app_root() -> Path:
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent.parent


def _get_whisper_config() -> dict:
    config_path = _get_app_root() / 'whisper_config.json'
    defaults = {
        'mode': 'local',
        'model': DEFAULT_MODEL,
        'python_path': 'auto',
        'cache_dir': str(_get_app_root() / 'cache' / 'whisper_models'),
        'api_base_url': os.getenv('OPENAI_BASE_URL', 'https://api.openai.com/v1'),
        'api_key': os.getenv('OPENAI_API_KEY', ''),
        'api_model': DEFAULT_API_MODEL,
    }
    if not config_path.is_file():
        return defaults

    try:
        with open(config_path, 'r', encoding='utf-8') as handle:
            user_config = json.load(handle)
        if isinstance(user_config, dict):
            defaults.update(user_config)
    except (OSError, json.JSONDecodeError) as exc:
        logging.warning(f"读取 whisper_config.json 失败: {exc}")
    return defaults


def _python_has_faster_whisper(python_exe: str, extra_args: Optional[list[str]] = None) -> bool:
    cmd = [python_exe]
    if extra_args:
        cmd.extend(extra_args)
    cmd.extend(['-c', 'from faster_whisper import WhisperModel'])
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _find_whisper_python(config: dict) -> Optional[tuple[str, list[str]]]:
    candidates: list[tuple[str, list[str]]] = []
    configured = (config.get('python_path') or '').strip()
    if configured and configured.lower() != 'auto':
        candidates.append((configured, []))

    env_python = os.getenv('ASRTOOLS_WHISPER_PYTHON', '').strip()
    if env_python:
        candidates.append((env_python, []))

    candidates.extend([
        (r'E:\Python310\python.exe', []),
        (r'E:\Tools\Python312\python.exe', []),
        ('py', ['-3.10']),
        ('py', ['-3.12']),
        ('py', []),
    ])

    for python_exe, extra_args in candidates:
        if python_exe == 'py' or Path(python_exe).is_file():
            if _python_has_faster_whisper(python_exe, extra_args):
                return python_exe, extra_args
    return None


class WhisperASR(BaseASR):
    def __init__(self, audio_path: [str, bytes], model: str = DEFAULT_MODEL, use_cache: bool = False):
        super().__init__(audio_path, use_cache)
        self.config = _get_whisper_config()
        self.model = model or self.config.get('model', DEFAULT_MODEL)

    def _get_key(self) -> str:
        return f"{self.__class__.__name__}-{self.model}-{self.crc32_hex}"

    def _run(self) -> dict:
        mode = (self.config.get('mode') or 'local').lower()
        if mode == 'api':
            return self._submit_api()
        return self._submit_local()

    def _submit_local(self) -> dict:
        python_info = _find_whisper_python(self.config)
        if not python_info:
            raise RuntimeError(
                "未找到可用于 Whisper 的 Python 环境。请安装 faster-whisper：\n"
                "  pip install faster-whisper\n"
                "或在 whisper_config.json 中设置 python_path 指向已安装的 Python。"
            )

        python_exe, extra_args = python_info
        worker = Path(__file__).resolve().parent.parent / 'whisper_worker.py'
        cache_dir = self.config.get('cache_dir') or str(_get_app_root() / 'cache' / 'whisper_models')
        Path(cache_dir).mkdir(parents=True, exist_ok=True)

        if isinstance(self.audio_path, bytes):
            raise RuntimeError("Whisper 本地模式需要音频文件路径")

        cmd = [python_exe, *extra_args, str(worker), self.audio_path, '--model', self.model, '--cache-dir', cache_dir]
        logging.info(f"启动本地 Whisper: model={self.model}")
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                encoding='utf-8',
                errors='replace',
                timeout=7200,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError("Whisper 本地识别超时") from exc

        if result.returncode != 0:
            message = (result.stderr or result.stdout or '').strip()
            if message.startswith('{'):
                try:
                    message = json.loads(message).get('error', message)
                except json.JSONDecodeError:
                    pass
            raise RuntimeError(f"Whisper 本地识别失败: {message[-800:]}")

        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Whisper 返回结果解析失败: {result.stdout[:300]}") from exc

    def _submit_api(self) -> dict:
        api_key = (self.config.get('api_key') or '').strip()
        base_url = (self.config.get('api_base_url') or 'https://api.openai.com/v1').rstrip('/')
        api_model = self.config.get('api_model', DEFAULT_API_MODEL)
        if not api_key:
            raise ValueError(
                "Whisper API 模式需要配置 api_key。"
                "请在 whisper_config.json 或环境变量 OPENAI_API_KEY 中设置。"
            )

        url = f"{base_url}/audio/transcriptions"
        headers = {'Authorization': f'Bearer {api_key}'}
        data = {
            'model': api_model,
            'response_format': 'verbose_json',
            'language': 'zh',
            'temperature': '0',
        }
        files = {'file': ('audio.mp3', self.file_binary, 'audio/mpeg')}
        response = requests.post(url, headers=headers, data=data, files=files, timeout=600)
        response.raise_for_status()
        return response.json()

    def _make_segments(self, resp_data: dict) -> list[ASRDataSeg]:
        segments = []
        for item in resp_data.get('segments', []):
            text = (item.get('text') or '').strip()
            if not text:
                continue
            start = item.get('start', 0)
            end = item.get('end', start)
            if start < 1000 and end < 1000:
                start *= 1000
                end *= 1000
            segments.append(ASRDataSeg(text, int(start), int(end)))
        return segments
