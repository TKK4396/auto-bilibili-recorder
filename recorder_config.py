from collections import OrderedDict
from typing import Optional

from bilibili_api import Credential, sync


class UploaderAccount:
    name: str
    sessdata: str
    bili_jct: str
    buvid3: str
    buvid4: str
    dedeuserid: str
    login_proxy: str
    access_token: str
    refresh_token: str
    app_key: str
    appsec: str
    cookie_file: str
    line: str
    verify: Credential

    def __init__(self, config_dict):
        for key, value in config_dict.items():
            self.__setattr__(key, value)
        self.login()

    def get_cookie_dict(self):
        return OrderedDict({
            "buvid3": getattr(self, "buvid3", None),
            "buvid4": getattr(self, "buvid4", None),
            "DedeUserID": getattr(self, "dedeuserid", None),
            "SESSDATA": getattr(self, "sessdata", None),
            "bili_jct": getattr(self, "bili_jct", None),
        })

    def get_cookie_file_dict(self):
        """生成兼容 BiliBili 原生 API 的 cookie 文件格式（嵌套结构）"""
        cookies = []
        for field, bili_name in [
            ("buvid3", "buvid3"), ("buvid4", "buvid4"),
            ("dedeuserid", "DedeUserID"), ("sessdata", "SESSDATA"),
            ("bili_jct", "bili_jct")
        ]:
            val = getattr(self, field, None)
            if val:
                cookies.append({"name": bili_name, "value": val})
        return {
            "cookie_info": {"cookies": cookies},
            "token_info": {
                "access_token": getattr(self, "access_token", None),
                "refresh_token": getattr(self, "refresh_token", None)
            }
        }

    def login(self):
        print(self.__dict__)
        required_cookies = ["sessdata", "bili_jct", "buvid3", "buvid4", "dedeuserid"]
        has_all_cookies = all(
            hasattr(self, cookie) and getattr(self, cookie) and not getattr(self, cookie).startswith("your_")
            for cookie in required_cookies
        )

        if not has_all_cookies:
            print(f"Warning: Missing or invalid cookies for {self.name}. Running in test mode without login.")
            self.verify = None
            if not hasattr(self, "line"):
                self.line = "auto"
            return

        self.verify = Credential.from_cookies(self.get_cookie_dict())
        if not sync(self.verify.check_valid()):
            print(f"Warning: Login failed for {self.name}. Running in test mode.")
            self.verify = None
        else:
            print(f"login successfully! {self.name} {self.sessdata} {self.bili_jct}")
        if not hasattr(self, "line"):
            self.line = "auto"


class HighlightConfig:
    """高光视频生成配置"""
    enabled: bool
    deepseek_api_key: str
    deepseek_model: str
    whisper_model: str
    use_gpu: bool
    clip_before: int
    clip_after: int
    max_highlights: int
    min_segment_gap: int

    def __init__(self, config_dict: dict = None):
        config_dict = config_dict or {}
        self.enabled = config_dict.get('enabled', False)
        self.deepseek_api_key = config_dict.get('deepseek_api_key', '')
        self.deepseek_model = config_dict.get('deepseek_model', 'deepseek-chat')
        self.whisper_model = config_dict.get('whisper_model', 'base')
        self.use_gpu = config_dict.get('use_gpu', True)
        self.clip_before = config_dict.get('clip_before', 60)
        self.clip_after = config_dict.get('clip_after', 60)
        self.max_highlights = config_dict.get('max_highlights', 5)
        self.min_segment_gap = config_dict.get('min_segment_gap', 30)

    def to_dict(self) -> dict:
        return {
            'enabled': self.enabled,
            'deepseek_api_key': self.deepseek_api_key,
            'deepseek_model': self.deepseek_model,
            'whisper_model': self.whisper_model,
            'use_gpu': self.use_gpu,
            'clip_before': self.clip_before,
            'clip_after': self.clip_after,
            'max_highlights': self.max_highlights,
            'min_segment_gap': self.min_segment_gap,
        }


class RecoderRoom:
    id: int
    uploader: Optional[str]
    uploader_obj: Optional[UploaderAccount]
    recorder: Optional[str]
    recorder_obj: Optional[UploaderAccount]
    tags: Optional[str]
    channel_id: Optional[int]
    title: Optional[str]
    description: Optional[str]
    source: Optional[str]
    he_user_dict: Optional[str]
    he_regex_rules: Optional[str]
    highlight: Optional[HighlightConfig]

    def __init__(self, config_dict):
        self.uploader = None
        self.recorder = None
        self.recorder_obj = None
        self.uploader_obj = None
        self.he_user_dict = None
        self.he_regex_rules = None
        self.highlight = None
        for key, value in config_dict.items():
            if key == 'highlight':
                self.highlight = HighlightConfig(value)
            else:
                self.__setattr__(key, value)
        if self.recorder is None and self.uploader is not None:
            self.recorder = self.uploader
        assert self.recorder_obj is None, "recorder_obj should not be set manually"
        assert self.uploader_obj is None, "uploader_obj should not be set manually"


class RecorderConfig:
    def __init__(self, config_dict):
        self.accounts = {name: UploaderAccount(account) for name, account in config_dict['accounts'].items()}
        self.rooms = [RecoderRoom(room) for room in config_dict['rooms']]
        # 全局高光配置（与 rooms 同级）
        self.highlight = None
        if 'highlight' in config_dict:
            self.highlight = HighlightConfig(config_dict['highlight'])
        
        for room in self.rooms:
            if room.uploader is not None:
                assert room.uploader in self.accounts, f"uploader {room.uploader} not found"
                room.uploader_obj = self.accounts[room.uploader]
            if room.recorder is not None:
                assert room.recorder in self.accounts, f"recorder {room.recorder} not found"
                room.recorder_obj = self.accounts[room.recorder]
