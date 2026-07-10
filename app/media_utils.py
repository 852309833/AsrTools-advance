import hashlib
import logging
import subprocess
import sys
import uuid
from pathlib import Path

NATIVE_AUDIO_EXTS = {'.mp3', '.wav', '.flac', '.m4a'}
CONVERTIBLE_EXTS = {
    '.mp3', '.wav', '.ogg', '.flac', '.aac', '.m4a', '.wma',
    '.mp4', '.avi', '.mov', '.ts', '.mkv', '.wmv', '.flv', '.webm', '.rmvb',
}


def get_app_root() -> Path:
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def get_ffmpeg_path() -> str:
    bundled = get_app_root() / 'ffmpeg.exe'
    if bundled.is_file():
        return str(bundled)
    return 'ffmpeg'


def get_audio_cache_dir() -> Path:
    cache_dir = get_app_root() / 'cache' / 'audio'
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def _run_ffmpeg(cmd: list[str]) -> subprocess.CompletedProcess:
    kwargs = {
        'capture_output': True,
        'encoding': 'utf-8',
        'errors': 'replace',
        'timeout': 3600,
    }
    if sys.platform == 'win32':
        kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
    return subprocess.run(cmd, **kwargs)


def video2audio(input_file: str, output: str = "") -> bool:
    """使用 ffmpeg 将视频或非原生音频转为 mp3。"""
    input_path = Path(input_file)
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        get_ffmpeg_path(),
        '-hide_banner',
        '-loglevel', 'error',
        '-i', str(input_path),
        '-vn',
        '-ac', '1',
        '-ar', '16000',
        '-c:a', 'libmp3lame',
        '-q:a', '2',
        '-y',
        str(output_path),
    ]
    try:
        result = _run_ffmpeg(cmd)
    except FileNotFoundError:
        logging.error("未找到 ffmpeg，请确认软件目录中存在 ffmpeg.exe")
        return False
    except subprocess.TimeoutExpired:
        logging.error("ffmpeg 转换超时")
        return False

    if result.returncode == 0 and output_path.is_file() and output_path.stat().st_size > 0:
        return True

    stderr = (result.stderr or '').strip()
    logging.error(f"ffmpeg 转换失败: {stderr[-800:]}")
    return False


def prepare_audio(file_path: str) -> tuple[str, bool]:
    """准备可供 ASR 引擎读取的音频文件，返回 (音频路径, 是否为临时文件)。"""
    source = Path(file_path)
    if not source.is_file():
        raise FileNotFoundError(f"文件不存在: {file_path}")

    ext = source.suffix.lower()
    if ext in NATIVE_AUDIO_EXTS:
        return str(source), False

    if ext not in CONVERTIBLE_EXTS:
        raise ValueError(
            f"不支持的文件格式: {ext}。"
            f"支持: {', '.join(sorted(CONVERTIBLE_EXTS))}"
        )

    digest = hashlib.md5(str(source.resolve()).encode('utf-8')).hexdigest()[:10]
    output_path = get_audio_cache_dir() / f"{source.stem}_{digest}_{uuid.uuid4().hex[:8]}.mp3"
    logging.info(f"正在转换媒体文件: {source.name} -> {output_path.name}")
    if not video2audio(str(source), str(output_path)):
        raise RuntimeError(
            "音频转换失败。请确认软件目录存在 ffmpeg.exe，且文件未损坏。"
        )
    return str(output_path), True
