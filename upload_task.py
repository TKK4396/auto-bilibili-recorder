import json
import os
import math
import asyncio
import requests as requests_lib
from bili_web_api import BiliBili, Data
from recorder_config import UploaderAccount
import logging

logger = logging.getLogger(__name__)

SPECIAL_SPACE = " "


async def async_wait_output(command):
    """异步执行终端命令，用于调用 ffprobe 和 ffmpeg"""
    print(f"running: {command}")
    process = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    return_value = await process.communicate()
    return return_value


async def split_video_if_needed(video_path):
    """
    检查视频大小，如果超过 14GB，则按约 8GB 为一段进行拆分。
    如果遇到视频损坏或拆分失败，抛出 RuntimeError 异常。
    """
    MAX_SIZE = 14 * 1024 * 1024 * 1024
    SPLIT_SIZE = 8 * 1024 * 1024 * 1024
    file_size = os.path.getsize(video_path)

    if file_size <= MAX_SIZE:
        return [video_path]

    print(f"视频大小 {file_size} 字节超出 14GB 限制，正在按 8GB 分块拆分...")

    cmd_format = f'ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "{video_path}"'
    stdout, stderr = await async_wait_output(cmd_format)
    duration_str = stdout.decode('utf-8').strip()
    stderr_str = stderr.decode('utf-8').strip()

    if "moov atom not found" in stderr_str:
        raise RuntimeError("视频文件严重损坏 (moov atom 丢失)，上一步视频压制意外中断。")

    if not duration_str or duration_str.lower() == 'n/a':
        print("未读取到全局时长，尝试读取视频流时长...")
        cmd_stream = f'ffprobe -v error -select_streams v:0 -show_entries stream=duration -of default=noprint_wrappers=1:nokey=1 "{video_path}"'
        stdout, stderr = await async_wait_output(cmd_stream)
        duration_lines = stdout.decode('utf-8').strip().split('\n')
        if duration_lines and duration_lines[0]:
            duration_str = duration_lines[0].strip()

    try:
        duration = float(duration_str)
        print(f"成功获取视频时长: {duration} 秒")
    except Exception as e:
        raise RuntimeError(f"获取视频时长失败，无法进行拆分: {e} | stderr: {stderr.decode('utf-8').strip()}")

    num_parts = math.ceil(file_size / SPLIT_SIZE)
    segment_time = math.ceil(duration / num_parts)

    base_name, ext = os.path.splitext(video_path)
    output_pattern = f"{base_name}_part%03d{ext}"

    split_cmd = f'ffmpeg -y -i "{video_path}" -c copy -map 0 -segment_time {segment_time} -f segment -reset_timestamps 1 "{output_pattern}"'
    await async_wait_output(split_cmd)

    parts = []
    for i in range(num_parts + 5):
        part_name = f"{base_name}_part{i:03d}{ext}"
        if os.path.exists(part_name):
            parts.append(part_name)

    if not parts:
        raise RuntimeError("ffmpeg 视频拆分失败，未生成任何切片文件。")

    print(f"拆分完成，共生成 {len(parts)} 个视频文件。")
    return parts


class UploadTask:

    def __init__(self, session_id, video_path, thumbnail_path, sc_path, he_path, subtitle_path,
                 title, source, description, tag, channel_id, danmaku, account: UploaderAccount, db_id=None):
        self.session_id = session_id
        self.video_path = video_path
        self.sc_path = sc_path
        self.he_path = he_path
        self.subtitle_path = subtitle_path
        self.thumbnail_path = thumbnail_path
        self.title = title
        self.source = source
        self.description = description
        self.tag = tag
        self.channel_id = channel_id
        self.danmaku = danmaku
        self.account = account
        self.verify = self.account.verify
        self.trial = 0
        self.db_id = db_id
        self.multi_part_videos = None
        self.multi_part_titles = None

    def set_multi_part(self, part_videos: list, part_titles: list = None):
        """设置多视频分 P 上传"""
        self.multi_part_videos = part_videos
        self.multi_part_titles = part_titles or [f"P{i+1}" for i in range(len(part_videos))]
        print(f"Set multi-part upload: {len(part_videos)} videos with titles: {self.multi_part_titles}")

    @staticmethod
    def _classify_error(e):
        """分类上传异常类型，返回 'credential' | 'upload'"""
        error_str = str(e)

        # requests 异常按类型判断
        if isinstance(e, requests_lib.exceptions.ConnectionError):
            return "upload"
        if isinstance(e, requests_lib.exceptions.Timeout):
            return "upload"
        if isinstance(e, requests_lib.exceptions.HTTPError):
            response = getattr(e, 'response', None)
            if response is not None and response.status_code in (401, 403):
                return "credential"
            return "upload"

        # B站 API 错误代码
        code = getattr(e, 'code', None)
        if code is not None and code in (-101, -111):
            return "credential"

        # 在错误消息中解析 JSON 错误码
        if isinstance(e, RuntimeError):
            try:
                err_json = json.loads(error_str)
                err_code = err_json.get('code', 0)
                if err_code in (-101, -111, -400, -352):
                    return "credential"
            except (ValueError, json.JSONDecodeError):
                pass

        # 关键词匹配（兜底）
        credential_kw = ['登录', '未登录', 'credential', 'cookie', 'sessdata', '鉴权', 'csrf']
        for kw in credential_kw:
            if kw.lower() in error_str.lower():
                return "credential"

        return "upload"

    _KNOWN_LINES = {"bda2", "qn", "ws", "bldsa"}

    @staticmethod
    def _name_to_line(name):
        if name not in UploadTask._KNOWN_LINES and name != "auto":
            logger.warning(f"未知线路 '{name}'，将由 probe 自动探测")
        return name

    async def upload(self, session_dict: {str: str}):

        if self.danmaku:
            suffix = "弹幕高能版"
        else:
            suffix = "无弹幕版"

        title_full = self.title + SPECIAL_SPACE + suffix

        # 确定主线路和备用线路列表
        configured_line_name = self.account.line if self.account.line != "auto" else "auto"
        fallback_names = []
        for name in ["bda2", "qn"]:
            if name != configured_line_name:
                fallback_names.append(name)

        lines_to_try = [configured_line_name] + fallback_names

        # 视频拆分（仅执行一次，不随线路切换重复）
        all_video_paths = []
        all_page_titles = []

        if self.multi_part_videos:
            for i, video_path in enumerate(self.multi_part_videos):
                try:
                    split_paths = await split_video_if_needed(video_path)
                    part_title = self.multi_part_titles[i] if self.multi_part_titles else f"P{i+1}"
                    for j, v_path in enumerate(split_paths):
                        all_video_paths.append(v_path)
                        if len(split_paths) == 1:
                            all_page_titles.append(part_title)
                        else:
                            all_page_titles.append(f"{part_title} ({j+1})")
                except Exception as e:
                    self.trial = 999
                    raise e
        else:
            try:
                video_paths = await split_video_if_needed(self.video_path)
            except Exception as e:
                self.trial = 999
                raise e

            for i, v_path in enumerate(video_paths):
                all_video_paths.append(v_path)
                page_title = suffix if len(video_paths) == 1 else f"{suffix} (P{i+1})"
                all_page_titles.append(page_title)

        # 线路切换重试循环
        for attempt_idx, line_name in enumerate(lines_to_try):
            if attempt_idx > 0:
                print(f"[线路切换] '{title_full}' 切换到备用线路 {line_name} 重试上传")

            try:
                video_data = Data(
                    tid=self.channel_id,
                    title=title_full,
                    desc=self.description,
                    tag=self.tag,
                    source=self.source,
                )
                bili = BiliBili(video_data)
                bili.set_cookies(self.account.get_cookie_file_dict())

                # 逐个上传视频文件
                for v_path, page_title in zip(all_video_paths, all_page_titles):
                    file_meta = await asyncio.to_thread(
                        bili.upload_file, v_path, lines=line_name, tasks=3
                    )
                    file_meta['title'] = page_title[:80]
                    file_meta['desc'] = ''
                    bili.video.append(file_meta)

                # 上传封面
                if self.thumbnail_path and os.path.exists(self.thumbnail_path):
                    try:
                        cover_url = await asyncio.to_thread(bili.cover_up, self.thumbnail_path)
                        bili.video.cover = cover_url
                    except Exception as e:
                        logger.warning(f"封面上传失败，继续提交: {e}")

                if self.session_id not in session_dict:
                    # 新视频投稿
                    result = await asyncio.to_thread(bili.submit)
                    bvid = result.get('data', {}).get('bvid', '')
                    print(f"[上传成功] 线路 {line_name}: {title_full} -> bvid={bvid}")
                    return bvid
                else:
                    # 追加分P到已有视频
                    existing_bvid = session_dict[self.session_id]
                    edit_data = bili.fetch_edit_data(existing_bvid)
                    archive = edit_data.get('data', {}).get('archive', {})

                    old_videos = archive.get('videos', [])
                    bili.video.videos = old_videos + bili.video.videos
                    bili.video.desc = archive.get('desc', '')
                    bili.video.tag = archive.get('tag', '')
                    bili.video.copyright = archive.get('copyright', 2)
                    bili.video.source = archive.get('source', '')
                    bili.video.cover = archive.get('cover', '')
                    bili.video.tid = archive.get('tid', self.channel_id)

                    old_title = archive.get('title', '')
                    if SPECIAL_SPACE in old_title:
                        stripped_title = old_title.rpartition(SPECIAL_SPACE)[0]
                    else:
                        stripped_title = old_title
                    bili.video.title = stripped_title + SPECIAL_SPACE + suffix

                    result = await asyncio.to_thread(bili.edit_submit, existing_bvid)
                    print(f"[上传成功] 线路 {line_name}: {bili.video.title} updated: {result}")
                    return existing_bvid

            except Exception as e:
                error_type = self._classify_error(e)

                if error_type == "credential":
                    print(f"[上传失败-认证错误] 线路 {line_name}: {e}")
                    print(f"[建议] 请检查账号 {self.account.name} 的 Cookie/登录态是否过期")
                    raise

                if attempt_idx < len(lines_to_try) - 1:
                    print(f"[上传失败-线路不可用] 线路 {line_name}: {e}")
                else:
                    print(f"[上传失败] 所有线路均已尝试 (已试: {lines_to_try}), 最后错误: {e}")
                    raise
