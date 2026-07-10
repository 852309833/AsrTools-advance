import requests

from .ASRData import ASRDataSeg
from .BaseASR import BaseASR


class KuaiShouASR(BaseASR):
    def __init__(self, audio_path: [str, bytes], use_cache: bool = False):
        super().__init__(audio_path, use_cache)

    def _run(self) -> dict:
        return self._submit()

    def _make_segments(self, resp_data: dict) -> list[ASRDataSeg]:
        return [ASRDataSeg(u['text'], u['start_time'], u['end_time']) for u in resp_data['data']['text']]

    def _submit(self) -> dict:
        payload = {
            "typeId": "1"
        }
        files = [('file', ('test.mp3', self.file_binary, 'audio/mpeg'))]
        result = requests.post(
            "https://ai.kuaishou.com/api/effects/subtitle_generate",
            data=payload,
            files=files,
            timeout=120,
        )
        result.raise_for_status()
        resp = result.json()
        if resp.get('code') != 0 or not resp.get('data'):
            msg = resp.get('msg') or resp.get('message') or '未知错误'
            raise RuntimeError(
                f"快手接口暂不可用 (code={resp.get('code')}): {msg}。"
                "建议改用「必剪」或「剪映」接口。"
            )
        return resp
