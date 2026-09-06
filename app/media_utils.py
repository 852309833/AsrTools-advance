import hashlib
import logging
import math
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Optional, List, Tuple

NATIVE_AUDIO_EXTS = {'.mp3', '.wav', '.flac', '.m4a'}
VIDEO_EXTS = {'.mp4', '.avi', '.mov', '.ts', '.mkv', '.wmv', '.flv', '.webm', '.rmvb'}
CONVERTIBLE_EXTS = NATIVE_AUDIO_EXTS | VIDEO_EXTS | {'.ogg', '.aac', '.wma'}

# 长音频按片切分，保证云端接口与本地识别都能吃下
DEFAULT_CHUNK_SECONDS = 10 * 60  # 10 分钟一片
CHUNK_SPLIT_THRESHOLD_SECONDS = 12 * 60  # 超过约 12 分钟才切分


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


def _run_ffmpeg(cmd: list[str], timeout: int = 7200) -> subprocess.CompletedProcess:
    kwargs = {
        'capture_output': True,
        'encoding': 'utf-8',
        'errors': 'replace',
        'timeout': timeout,
    }
    if sys.platform == 'win32':
        kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
    return subprocess.run(cmd, **kwargs)


def _needs_ascii_workaround(path: Path) -> bool:
    try:
        str(path).encode('ascii')
        return False
    except UnicodeEncodeError:
        return True


def _prepare_ffmpeg_input(input_path: Path) -> Tuple[str, Optional[Path]]:
    """返回 (ffmpeg 可用输入路径, 需要事后删除的临时副本)。"""
    if sys.platform == 'win32' and _needs_ascii_workaround(input_path):
        tmp_input = get_audio_cache_dir() / f"_in_{uuid.uuid4().hex[:10]}{input_path.suffix.lower()}"
        try:
            shutil.copy2(str(input_path), str(tmp_input))
            return str(tmp_input), tmp_input
        except OSError as exc:
            logging.warning(f"复制到临时路径失败，改用原路径: {exc}")
    return str(input_path), None


def video2audio(input_file: str, output: str = "") -> bool:
    """使用 ffmpeg 将视频/音频统一转为 16kHz 单声道 mp3。"""
    input_path = Path(input_file)
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    ffmpeg_input, tmp_input = _prepare_ffmpeg_input(input_path)
    cmd = [
        get_ffmpeg_path(),
        '-hide_banner',
        '-loglevel', 'error',
        '-i', ffmpeg_input,
        '-vn',
        '-ac', '1',
        '-ar', '16000',
        '-c:a', 'libmp3lame',
        '-b:a', '48k',
        '-y',
        str(output_path),
    ]
    try:
        result = _run_ffmpeg(cmd, timeout=7200)
    except FileNotFoundError:
        logging.error("未找到 ffmpeg，请确认软件目录中存在 ffmpeg.exe")
        return False
    except subprocess.TimeoutExpired:
        logging.error("ffmpeg 转换超时")
        return False
    finally:
        if tmp_input and tmp_input.is_file():
            try:
                tmp_input.unlink()
            except OSError:
                pass

    if result.returncode == 0 and output_path.is_file() and output_path.stat().st_size > 0:
        logging.info(f"已生成 MP3: {output_path} ({output_path.stat().st_size // 1024}KB)")
        return True

    stderr = (result.stderr or '').strip()
    logging.error(f"ffmpeg 转换失败: {stderr[-800:]}")
    return False


def probe_duration_ms(file_path: str) -> int:
    """探测媒体时长（毫秒）。"""
    ffprobe = Path(get_ffmpeg_path()).with_name('ffprobe.exe')
    try:
        if ffprobe.is_file():
            cmd = [
                str(ffprobe), '-v', 'error', '-show_entries', 'format=duration',
                '-of', 'default=noprint_wrappers=1:nokey=1', file_path,
            ]
            result = _run_ffmpeg(cmd, timeout=120)
            text = (result.stdout or '').strip()
            if text:
                return max(int(float(text) * 1000), 1000)
    except Exception:
        pass

    try:
        cmd = [get_ffmpeg_path(), '-i', file_path]
        result = _run_ffmpeg(cmd, timeout=120)
        m = re.search(r'Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)', result.stderr or '')
        if m:
            h, mi, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
            return int((h * 3600 + mi * 60 + s) * 1000)
    except Exception:
        pass
    return 0


def build_export_path(source_path: str, export_format: str, output_dir: str = "") -> str:
    """生成导出文件路径。

    - 若指定 output_dir 且有效，则输出到该目录，文件名与源文件同名
    - 否则输出到源文件同目录
    """
    source = Path(source_path).resolve()
    ext = export_format.lower().lstrip('.')
    filename = f"{source.stem}.{ext}"
    out = (output_dir or "").strip().strip('"').strip("'")
    if out:
        out_path = Path(out)
        if out_path.is_file():
            out_path = out_path.parent
        out_path.mkdir(parents=True, exist_ok=True)
        return str((out_path / filename).resolve())
    return str(source.with_suffix(f'.{ext}'))


def extracted_mp3_path_for(source: Path) -> Path:
    """视频旁保存的中间 MP3：xxx.mp4 -> xxx_audio.mp3"""
    return source.with_name(f"{source.stem}_audio.mp3")


def prepare_audio(file_path: str) -> tuple[str, bool]:
    """
    统一准备识别用 MP3。

    流程：
    1. 视频 / 非 mp3 → 先转成 MP3（保存到源文件同目录的 *_audio.mp3）
    2. 已是 mp3 → 直接使用（过大则重编码压缩）
    返回 (mp3路径, 是否为可清理的临时文件)
    """
    source = Path(file_path)
    if not source.is_file():
        raise FileNotFoundError(f"文件不存在: {file_path}")

    ext = source.suffix.lower()
    if ext not in CONVERTIBLE_EXTS:
        raise ValueError(
            f"不支持的文件格式: {ext}。"
            f"支持: {', '.join(sorted(CONVERTIBLE_EXTS))}"
        )

    # 已经是较小的 mp3：直接作为识别输入
    if ext == '.mp3' and source.stat().st_size <= 25 * 1024 * 1024:
        logging.info(f"使用已有 MP3: {source.name}")
        return str(source.resolve()), False

    # 视频：在同目录生成 xxx_audio.mp3，方便核对「先转音频再转文字」
    if ext in VIDEO_EXTS:
        output_path = extracted_mp3_path_for(source)
        if (
            output_path.is_file()
            and output_path.stat().st_size > 0
            and output_path.stat().st_mtime >= source.stat().st_mtime
        ):
            logging.info(f"复用已提取的 MP3: {output_path.name}")
            return str(output_path.resolve()), False

        logging.info(f"步骤1/2: 视频转 MP3 → {output_path.name}")
        if not video2audio(str(source), str(output_path)):
            # 同目录写失败（权限等）时退回缓存目录
            digest = hashlib.md5(str(source.resolve()).encode('utf-8')).hexdigest()[:10]
            output_path = get_audio_cache_dir() / f"media_{digest}_{uuid.uuid4().hex[:8]}.mp3"
            logging.warning(f"同目录写入失败，改存缓存: {output_path}")
            if not video2audio(str(source), str(output_path)):
                raise RuntimeError("视频转 MP3 失败。请确认 ffmpeg.exe 可用，且视频未损坏。")
            return str(output_path.resolve()), True
        return str(output_path.resolve()), False

    # 其他音频 / 过大 mp3：转成标准识别用 mp3（缓存）
    digest = hashlib.md5(str(source.resolve()).encode('utf-8')).hexdigest()[:10]
    output_path = get_audio_cache_dir() / f"media_{digest}_{uuid.uuid4().hex[:8]}.mp3"
    logging.info(f"步骤1/2: 音频规范化为 MP3 → {output_path.name}")
    if not video2audio(str(source), str(output_path)):
        raise RuntimeError("音频转 MP3 失败。请确认 ffmpeg.exe 可用，且文件未损坏。")
    return str(output_path.resolve()), True


def split_audio_chunks(
    mp3_path: str,
    chunk_seconds: int = DEFAULT_CHUNK_SECONDS,
    split_threshold_seconds: int = CHUNK_SPLIT_THRESHOLD_SECONDS,
) -> Tuple[List[Tuple[str, int]], List[str]]:
    """
    将长 MP3 切成多段。

    返回:
      chunks: [(chunk_mp3_path, start_offset_ms), ...]
      temps: 需要清理的临时文件/目录相关路径
    """
    source = Path(mp3_path)
    if not source.is_file():
        raise FileNotFoundError(f"MP3 不存在: {mp3_path}")

    duration_ms = probe_duration_ms(mp3_path)
    duration_s = duration_ms / 1000.0 if duration_ms > 0 else 0
    size_mb = source.stat().st_size / (1024 * 1024)

    # 短音频不切分
    if duration_s and duration_s <= split_threshold_seconds and size_mb <= 12:
        return [(str(source), 0)], []
    if not duration_s and size_mb <= 12:
        return [(str(source), 0)], []

    if not duration_s:
        # 时长探测失败时按文件大小粗估（48kbps ≈ 0.36MB/分钟）
        duration_s = max(size_mb / 0.36 * 60, chunk_seconds + 1)

    total_chunks = max(1, int(math.ceil(duration_s / chunk_seconds)))
    logging.info(
        f"长音频分片: 时长约 {duration_s/60:.1f} 分钟, 大小 {size_mb:.1f}MB, "
        f"切为 {total_chunks} 片 (每片 {chunk_seconds//60} 分钟)"
    )

    chunk_dir = get_audio_cache_dir() / f"chunks_{source.stem[:40]}_{uuid.uuid4().hex[:8]}"
    chunk_dir.mkdir(parents=True, exist_ok=True)
    temps = [str(chunk_dir)]
    chunks: List[Tuple[str, int]] = []

    ffmpeg_input, tmp_input = _prepare_ffmpeg_input(source)
    if tmp_input:
        temps.append(str(tmp_input))

    for idx in range(total_chunks):
        start_s = idx * chunk_seconds
        if start_s >= duration_s:
            break
        out_path = chunk_dir / f"chunk_{idx:04d}.mp3"
        cmd = [
            get_ffmpeg_path(),
            '-hide_banner',
            '-loglevel', 'error',
            '-ss', str(start_s),
            '-t', str(chunk_seconds),
            '-i', ffmpeg_input,
            '-ac', '1',
            '-ar', '16000',
            '-c:a', 'libmp3lame',
            '-b:a', '48k',
            '-y',
            str(out_path),
        ]
        try:
            result = _run_ffmpeg(cmd, timeout=1800)
        except Exception as exc:
            raise RuntimeError(f"分片失败 chunk#{idx}: {exc}") from exc

        if result.returncode != 0 or not out_path.is_file() or out_path.stat().st_size == 0:
            stderr = (result.stderr or '').strip()
            raise RuntimeError(f"分片失败 chunk#{idx}: {stderr[-500:]}")

        chunks.append((str(out_path), int(start_s * 1000)))
        logging.info(f"已生成分片 {idx + 1}/{total_chunks}: {out_path.name}")

    if tmp_input and tmp_input.is_file():
        try:
            tmp_input.unlink()
            temps = [t for t in temps if t != str(tmp_input)]
        except OSError:
            pass

    if not chunks:
        return [(str(source), 0)], temps
    return chunks, temps


def cleanup_paths(paths: list[str]) -> None:
    """清理临时分片文件或目录。"""
    for item in paths:
        p = Path(item)
        try:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            elif p.is_file():
                p.unlink()
        except OSError:
            pass
