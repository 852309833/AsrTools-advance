# AsrTools Advance

基于 [AsrTools](https://github.com/WEIFENG2333/AsrTools) 的增强版语音转文字工具，提供图形界面、多引擎识别和批量导出能力。

## 功能

- 支持多种 ASR 接口：必剪 (B站)、剪映、快手、Whisper (本地)
- 支持音频/视频文件批量转写
- 支持拖拽文件或文件夹
- 多线程并发处理
- 导出格式：**TXT（默认）**、SRT、ASS
- 内置 ffmpeg，可将视频自动转为音频后再识别

## 默认导出格式

程序默认导出 **纯文本 `.txt`** 文件，每行一段识别结果，不含时间轴。

如需字幕文件，可在界面中将“导出格式”切换为 `SRT` 或 `ASS`。

## 目录结构

```
AsrTools-advance/
├── app/                    # Python 源码
│   ├── asr_gui.py          # 主界面
│   ├── media_utils.py      # 音视频预处理
│   ├── whisper_worker.py   # Whisper 子进程
│   └── bk_asr/             # ASR 引擎实现
├── whisper_config.json     # Whisper 配置模板
├── ffmpeg.exe              # 音视频转换（Windows 便携版）
├── runtime/                # 内置 Python 运行时（便携版）
└── cache/                  # 运行时缓存（不应提交到 Git）
```

## 运行方式

### 便携版（Windows）

直接运行：

```text
AsrTools.exe
```

### 源码运行

```bash
pip install -r requirements.txt
python app/asr_gui.py
```

Whisper 本地模式还需要：

```bash
pip install faster-whisper
```

## Whisper 配置

编辑 `whisper_config.json`：

```json
{
  "mode": "local",
  "model": "base",
  "python_path": "auto",
  "cache_dir": "",
  "api_base_url": "https://api.openai.com/v1",
  "api_key": "",
  "api_model": "whisper-1"
}
```

- `mode`: `local` 使用本地 faster-whisper，`api` 使用 OpenAI 兼容接口
- 建议通过环境变量 `OPENAI_API_KEY` 注入密钥，不要把真实密钥提交到 GitHub

## 支持的媒体格式

- 音频：mp3, wav, ogg, flac, aac, m4a, wma
- 视频：mp4, avi, mov, ts, mkv, wmv, flv, webm, rmvb

## 说明

- 本项目基于开源项目 AsrTools 二次开发
- 在线 ASR 接口依赖第三方服务，请自行评估可用性与合规性
- `cache/`、`runtime/` 为本地运行产物，GitHub 仓库默认不上传

## License

请参考上游项目 [WEIFENG2333/AsrTools](https://github.com/WEIFENG2333/AsrTools) 的许可证说明。
