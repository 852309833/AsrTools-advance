import json
import logging
import time
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .ASRData import ASRDataSeg
from .BaseASR import BaseASR


__version__ = "0.0.4"

API_BASE_URL = "https://member.bilibili.com/x/bcut/rubick-interface"
API_REQ_UPLOAD = API_BASE_URL + "/resource/create"
API_COMMIT_UPLOAD = API_BASE_URL + "/resource/create/complete"
API_CREATE_TASK = API_BASE_URL + "/task"
API_QUERY_RESULT = API_BASE_URL + "/task/result"

# 实测当前可用；创建与查询必须一致
MODEL_ID = "8"

# 必剪对过大文件不稳定，超过该大小直接失败并交给降级引擎（约 20MB）
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

# 必剪 task/result 在任务未就绪时会大量返回瞬时 412（HTML），不能当最终失败
TRANSIENT_HTTP = {408, 409, 412, 425, 429, 500, 502, 503, 504}


class BcutASR(BaseASR):
    """必剪 语音识别接口"""

    headers = {
        "User-Agent": "Bilibili/1.0.0 (https://www.bilibili.com)",
        "Content-Type": "application/json",
    }

    def __init__(self, audio_path: [str, bytes], use_cache: bool = False):
        super().__init__(audio_path, use_cache=use_cache)
        self.session = requests.Session()
        retry = Retry(
            total=3,
            connect=3,
            read=3,
            backoff_factor=0.8,
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=["GET", "POST", "PUT"],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

        self.task_id: Optional[str] = None
        self.__etags: list[str] = []
        self.__in_boss_key: Optional[str] = None
        self.__resource_id: Optional[str] = None
        self.__upload_id: Optional[str] = None
        self.__upload_urls: list[str] = []
        self.__per_size: Optional[int] = None
        self.__clips: Optional[int] = None
        self.__download_url: Optional[str] = None

    def _reset_upload_state(self) -> None:
        self.task_id = None
        self.__etags = []
        self.__in_boss_key = None
        self.__resource_id = None
        self.__upload_id = None
        self.__upload_urls = []
        self.__per_size = None
        self.__clips = None
        self.__download_url = None

    def _request(self, method: str, url: str, *, allow_transient: bool = False, **kwargs):
        """带退避的请求。allow_transient=True 时，412 等瞬时错误不抛出，交给调用方处理。"""
        timeout = kwargs.pop("timeout", 60)
        last_error = None
        for attempt in range(8):
            try:
                resp = self.session.request(method, url, timeout=timeout, **kwargs)
                if resp.status_code in TRANSIENT_HTTP:
                    if allow_transient:
                        return resp
                    last_error = requests.HTTPError(
                        f"{resp.status_code} Client Error for url: {resp.url}",
                        response=resp,
                    )
                    time.sleep(min(2 ** attempt, 12))
                    continue
                resp.raise_for_status()
                return resp
            except requests.RequestException as exc:
                last_error = exc
                time.sleep(min(2 ** attempt, 12))
        if last_error:
            raise last_error
        raise RuntimeError(f"请求失败: {method} {url}")

    def upload(self) -> None:
        if not self.file_binary:
            raise ValueError("none set data")
        if len(self.file_binary) > MAX_UPLOAD_BYTES:
            raise RuntimeError(
                f"音频过大（{len(self.file_binary) // 1024 // 1024}MB），必剪接口不稳定，"
                "将自动切换其他引擎"
            )

        payload = json.dumps({
            "type": 2,
            "name": "audio.mp3",
            "size": len(self.file_binary),
            "ResourceFileType": "mp3",
            "model_id": MODEL_ID,
        })
        resp = self._request("POST", API_REQ_UPLOAD, data=payload, headers=self.headers, timeout=60)
        body = resp.json()
        if body.get("code"):
            raise RuntimeError(f"必剪申请上传失败: {body.get('message') or body}")

        data = body["data"]
        self.__in_boss_key = data["in_boss_key"]
        self.__resource_id = data["resource_id"]
        self.__upload_id = data["upload_id"]
        self.__upload_urls = data["upload_urls"]
        self.__per_size = data["per_size"]
        self.__clips = len(data["upload_urls"])
        logging.info(
            f"申请上传成功, 总计大小{data['size'] // 1024}KB, {self.__clips}分片, "
            f"分片大小{data['per_size'] // 1024}KB"
        )
        self.__upload_part()
        self.__commit_upload()

    def __upload_part(self) -> None:
        put_headers = {"User-Agent": self.headers["User-Agent"]}
        for clip in range(self.__clips):
            start_range = clip * self.__per_size
            end_range = (clip + 1) * self.__per_size
            logging.info(f"开始上传分片{clip}: {start_range}-{end_range}")
            resp = self._request(
                "PUT",
                self.__upload_urls[clip],
                data=self.file_binary[start_range:end_range],
                headers=put_headers,
                timeout=180,
            )
            etag = resp.headers.get("Etag") or resp.headers.get("ETag") or ""
            if not etag:
                raise RuntimeError(f"必剪分片{clip}上传未返回 ETag")
            self.__etags.append(etag)
            logging.info(f"分片{clip}上传成功: {etag}")

    def __commit_upload(self) -> None:
        data = json.dumps({
            "InBossKey": self.__in_boss_key,
            "ResourceId": self.__resource_id,
            "Etags": ",".join(self.__etags),
            "UploadId": self.__upload_id,
            "model_id": MODEL_ID,
        })
        resp = self._request("POST", API_COMMIT_UPLOAD, data=data, headers=self.headers, timeout=60)
        body = resp.json()
        if body.get("code"):
            raise RuntimeError(f"必剪提交上传失败: {body.get('message') or body}")
        self.__download_url = body["data"]["download_url"]
        logging.info("提交成功")

    def create_task(self) -> str:
        resp = self._request(
            "POST",
            API_CREATE_TASK,
            json={"resource": self.__download_url, "model_id": MODEL_ID},
            headers=self.headers,
            timeout=60,
        )
        body = resp.json()
        if body.get("code"):
            raise RuntimeError(f"必剪创建任务失败: {body.get('message') or body}")
        self.task_id = body["data"]["task_id"]
        logging.info(f"任务已创建: {self.task_id}")
        return self.task_id

    def result(self, task_id: Optional[str] = None) -> dict:
        """查询结果。412/5xx 视为任务尚未可读，返回 pending 状态继续轮询。"""
        resp = self._request(
            "GET",
            API_QUERY_RESULT,
            params={"model_id": MODEL_ID, "task_id": task_id or self.task_id},
            headers=self.headers,
            timeout=30,
            allow_transient=True,
        )
        if resp.status_code in TRANSIENT_HTTP:
            return {"state": 1, "result": "", "remark": f"http_{resp.status_code}"}

        try:
            body = resp.json()
        except ValueError:
            return {"state": 1, "result": "", "remark": "invalid_json"}

        if body.get("code"):
            # 业务未就绪也当作 pending，避免偶发 code 打断长视频
            logging.warning(f"必剪查询业务码: {body.get('code')} {body.get('message')}")
            return {"state": 1, "result": "", "remark": str(body.get("message") or body.get("code"))}

        return body.get("data") or {"state": 1, "result": "", "remark": "empty_data"}

    def _run_once(self) -> dict:
        self._reset_upload_state()
        self.upload()
        self.create_task()

        # 长视频可能需要更久；412 期间不算失败
        max_polls = 600
        task_resp = None
        for attempt in range(max_polls):
            task_resp = self.result()
            state = task_resp.get("state")
            if state == 4:
                break
            if state in (5, 6, -1):
                raise RuntimeError(f"必剪识别失败，任务状态: {state} {task_resp.get('remark') or ''}")
            # 前期 412 很频繁，稍加快；后期保持 1s
            time.sleep(0.6 if attempt < 30 else 1.0)
        else:
            raise TimeoutError("必剪识别超时（约10分钟），云端接口可能繁忙")

        raw = task_resp.get("result") if task_resp else None
        if not raw:
            raise RuntimeError("必剪识别完成但未返回结果，请稍后重试")

        logging.info("转换成功")
        return json.loads(raw)

    def _run(self):
        last_error = None
        for round_idx in range(3):
            try:
                if round_idx:
                    logging.warning(f"必剪整流程重试第 {round_idx} 次...")
                    time.sleep(2 * round_idx)
                return self._run_once()
            except Exception as exc:
                last_error = exc
                logging.error(f"必剪第 {round_idx + 1} 次失败: {exc}")
        raise RuntimeError(f"必剪识别多次失败: {last_error}") from last_error

    def _make_segments(self, resp_data: dict) -> list[ASRDataSeg]:
        utterances = resp_data.get("utterances") or []
        return [
            ASRDataSeg(u.get("transcript") or u.get("text") or "", u["start_time"], u["end_time"])
            for u in utterances
            if (u.get("transcript") or u.get("text") or "").strip()
        ]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    audio_file = r"test.mp3"
    asr = BcutASR(audio_file)
    print(asr.run())
