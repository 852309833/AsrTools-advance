"""独立 Whisper 识别进程，由主程序通过 subprocess 调用。"""
import argparse
import json
import os
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description='AsrTools Whisper worker')
    parser.add_argument('audio', help='音频文件路径')
    parser.add_argument('--model', default='base', help='Whisper 模型名称')
    parser.add_argument('--cache-dir', default='', help='模型缓存目录')
    args = parser.parse_args()

    if args.cache_dir:
        os.environ['HF_HOME'] = args.cache_dir
        os.environ['HUGGINGFACE_HUB_CACHE'] = args.cache_dir
    os.environ.setdefault('HF_HUB_DISABLE_SYMLINKS_WARNING', '1')

    if not os.path.isfile(args.audio):
        print(json.dumps({'error': f'音频文件不存在: {args.audio}'}), file=sys.stderr)
        return 1

    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        print(json.dumps({
            'error': (
                '未找到 faster-whisper。请安装: pip install faster-whisper '
                f'({exc})'
            )
        }), file=sys.stderr)
        return 1

    try:
        model = WhisperModel(args.model, device='cpu', compute_type='int8')
        segments, _info = model.transcribe(
            args.audio,
            language='zh',
            vad_filter=True,
        )
        payload = {
            'segments': [
                {
                    'text': segment.text.strip(),
                    'start': segment.start,
                    'end': segment.end,
                }
                for segment in segments
                if segment.text.strip()
            ]
        }
        print(json.dumps(payload, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({'error': str(exc)}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
