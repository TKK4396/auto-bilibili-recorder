import os
import math
import asyncio
from bilibili_api.video_uploader import (
    VideoUploader, VideoUploaderPage, VideoEditor, VideoMeta, Lines, _choose_line)
from recorder_config import UploaderAccount

SPECIAL_SPACE = "\u2007"

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
    MAX_SIZE = 14 * 1024 * 1024 * 1024  # 15GB 限制阈值
    SPLIT_SIZE = 8 * 1024 * 1024 * 1024 # 8GB 分割单位
    file_size = os.path.getsize(video_path)

    # 如果文件大小合规，直接返回原路径
    if file_size <= MAX_SIZE:
        return [video_path]

    print(f"视频大小 {file_size} 字节超出 14GB 限制，正在按 8GB 分块拆分...")

    # 1. 尝试获取全局容器时长
    cmd_format = f'ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "{video_path}"'
    stdout, stderr = await async_wait_output(cmd_format)
    duration_str = stdout.decode('utf-8').strip()
    stderr_str = stderr.decode('utf-8').strip()

    # 【新增】：检测视频文件是否本身已损坏（如 moov atom 丢失），直接抛出异常阻断
    if "moov atom not found" in stderr_str:
        raise RuntimeError("视频文件严重损坏 (moov atom 丢失)，上一步视频压制意外中断。")

    # 如果没获取到全局时长，尝试读取视频流的时长
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
        # 【新增】：获取时长依然失败，说明无法安全切片，直接抛出异常
        raise RuntimeError(f"获取视频时长失败，无法进行拆分: {e} | stderr: {stderr.decode('utf-8').strip()}")

    # 2. 计算需要拆分的段数以及每段的时长
    num_parts = math.ceil(file_size / SPLIT_SIZE)
    segment_time = math.ceil(duration / num_parts)

    base_name, ext = os.path.splitext(video_path)
    output_pattern = f"{base_name}_part%03d{ext}"

    # 3. 使用 ffmpeg 的 segment 模块进行无损快速切片
    split_cmd = f'ffmpeg -y -i "{video_path}" -c copy -map 0 -segment_time {segment_time} -f segment -reset_timestamps 1 "{output_pattern}"'
    await async_wait_output(split_cmd)

    # 4. 收集切片生成的文件
    parts = []
    for i in range(num_parts + 5): # 稍微多探测几个索引以防误差
        part_name = f"{base_name}_part{i:03d}{ext}"
        if os.path.exists(part_name):
            parts.append(part_name)

    if not parts:
        # 【新增】：切片未生成，抛出异常
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
        # 多视频分 P 上传支持
        self.multi_part_videos = None  # [(video_path, title), ...]
        self.multi_part_titles = None  # ["P1标题", "P2标题", ...]

    def set_multi_part(self, part_videos: list, part_titles: list = None):
        """
        设置多视频分 P 上传
        
        Args:
            part_videos: 视频路径列表
            part_titles: 各分 P 标题列表（可选）
        """
        self.multi_part_videos = part_videos
        self.multi_part_titles = part_titles or [f"P{i+1}" for i in range(len(part_videos))]
        print(f"Set multi-part upload: {len(part_videos)} videos with titles: {self.multi_part_titles}")

    @staticmethod
    def _classify_error(e):
        """
        分类上传异常类型，返回 'credential' | 'upload'
        credential: 登录态/鉴权错误，切换线路无效，应快速失败
        upload: 网络/上传节点错误，可尝试切换线路重试
        """
        error_str = str(e)

        # 检查 ResponseCodeException 的 code 属性 (bilibili_api 异常)
        code = getattr(e, 'code', None)
        if code is not None and code in [-101, -111]:
            return "credential"

        # 根据异常消息中的关键词分类
        credential_kw = ['登录', '未登录', 'credential', 'cookie', 'sessdata', '鉴权', 'csrf']
        for kw in credential_kw:
            if kw.lower() in error_str.lower():
                return "credential"

        # 上传阶段错误和网络错误 → 可尝试切换线路
        upload_kw = ['upload_id', '上传', 'preupload', 'timeout', 'timed out',
                     'connectionerror', 'connection', 'network', '网络错误', '网络']
        for kw in upload_kw:
            if kw.lower() in error_str.lower():
                return "upload"

        # 默认当作可重试的上传错误
        return "upload"

    _LINE_MAP = {"bda2": Lines.BDA2, "qn": Lines.QN, "ws": Lines.WS, "bldsa": Lines.BLDSA}

    @staticmethod
    def _name_to_line(name):
        """将线路名称转换为 bilibili_api 的 Lines 枚举值"""
        return _LINE_MAP.get(name)

    async def upload(self, session_dict: {str: str}):

        if self.danmaku:
            suffix = "弹幕高能版"
        else:
            suffix = "无弹幕版"

        # 确定主线路和备用线路列表 (qn 和 bda2 互为备用)
        configured_line_name = self.account.line if self.account.line != "auto" else "auto"
        primary_line = self._name_to_line(configured_line_name)

        fallback_names = []
        for name in ["bda2", "qn"]:
            if name != configured_line_name:
                fallback_names.append(name)

        # 构建尝试线路列表: [主线路, 备用1, 备用2]
        lines_to_try = [(configured_line_name, primary_line)]
        for name in fallback_names:
            lines_to_try.append((name, self._name_to_line(name)))

        meta = VideoMeta(
            tid=self.channel_id,
            title=self.title + SPECIAL_SPACE + suffix,
            desc=self.description,
            tags=self.tag,
            original=False,
            source=self.source,
            cover=self.thumbnail_path,
            no_reprint=False,
            subtitle={
                "lan": "",
                "open": 0
            }
        )

        async def on_progress(data):
            print(data)

        # 视频拆分（仅执行一次，不随线路切换重复）
        all_video_paths = []
        all_page_titles = []

        if self.multi_part_videos:
            # 多视频分 P 模式
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
            # 单视频模式
            try:
                video_paths = await split_video_if_needed(self.video_path)
            except Exception as e:
                self.trial = 999
                raise e

            for i, v_path in enumerate(video_paths):
                all_video_paths.append(v_path)
                page_title = suffix if len(video_paths) == 1 else f"{suffix} (P{i+1})"
                all_page_titles.append(page_title)
        # ============================================================

        # 线路切换重试循环
        pages = []
        for i, v_path in enumerate(all_video_paths):
            pages.append(VideoUploaderPage(path=v_path, title=all_page_titles[i]))

        for attempt_idx, (line_name, line_obj) in enumerate(lines_to_try):
            if attempt_idx > 0:
                print(f"[线路切换] '{meta.title}' 切换到备用线路 {line_name} 重试上传")

            try:
                uploader = VideoUploader(
                    pages=pages,
                    meta=meta,
                    credential=self.verify,
                    line=line_obj
                )

                uploader.add_event_listener("__ALL__", on_progress)
                if self.session_id not in session_dict:
                    result = await uploader.start()
                    print(f"[上传成功] 线路 {line_name}: {meta.title} → {result}")
                    return result['bvid']
                else:
                    videos = []
                    uploader.line = await _choose_line(uploader.line)
                    for page in uploader.pages:
                        data = await uploader._upload_page(page)
                        videos.append(
                            {
                                "title": page.title,
                                "filename": data['filename'],
                                "desc": "",
                                "cid": data['cid']
                            }
                        )
                        print(f"{page.title} uploaded: {data['filename']}")

                    meta_dict = {
                        "copyright": 2,
                        "desc_format_id": 0,
                        "dynamic": "",
                        "interactive": 0,
                        "new_web_edit": 1, "act_reserve_create": 0,
                        "handle_staff": False, "topic_grey": 1, "no_reprint": 0, "subtitles": {
                            "lan": "",
                            "open": 0
                        }, "web_os": 2, 'videos': videos
                    }

                    updater = VideoEditor(
                        bvid=session_dict[self.session_id],
                        meta=meta_dict,
                        credential=self.verify
                    )
                    updater.add_event_listener("__ALL__", on_progress)
                    await updater._fetch_configs()

                    old_videos = []
                    if "videos" in updater._VideoEditor__old_configs:
                        old_videos = updater._VideoEditor__old_configs["videos"]
                    elif "archive" in updater._VideoEditor__old_configs and "videos" in updater._VideoEditor__old_configs["archive"]:
                        old_videos = updater._VideoEditor__old_configs["archive"]["videos"]

                    updater.meta["videos"] = old_videos + videos

                    updater.meta["desc"] = updater._VideoEditor__old_configs["archive"]["desc"]
                    updater.meta["tag"] = updater._VideoEditor__old_configs["archive"]["tag"]
                    updater.meta["copyright"] = updater._VideoEditor__old_configs["archive"]["copyright"]
                    updater.meta["source"] = updater._VideoEditor__old_configs["archive"]["source"]
                    updater.meta["cover"] = updater._VideoEditor__old_configs["archive"]["cover"]
                    updater.meta["tid"] = updater._VideoEditor__old_configs["archive"]["tid"]
                    old_title = updater._VideoEditor__old_configs["archive"]["title"]

                    if SPECIAL_SPACE in old_title:
                        stripped_title = old_title.rpartition(SPECIAL_SPACE)[0]
                    else:
                        stripped_title = old_title
                    new_title = stripped_title + SPECIAL_SPACE + suffix
                    updater.meta["title"] = new_title
                    result = await updater._submit()
                    print(f"[上传成功] 线路 {line_name}: {new_title} updated: {result}")
                    return updater.bvid

            except Exception as e:
                error_type = self._classify_error(e)

                if error_type == "credential":
                    print(f"[上传失败-认证错误] 线路 {line_name}: {e}")
                    print(f"[建议] 请检查账号 {self.account.name} 的 Cookie/登录态是否过期")
                    raise

                # upload 类型错误 → 尝试下一条线路
                if attempt_idx < len(lines_to_try) - 1:
                    print(f"[上传失败-线路不可用] 线路 {line_name}: {e}")
                else:
                    print(f"[上传失败] 所有线路均已尝试 (已试: {[n for n, _ in lines_to_try]}), 最后错误: {e}")
                    raise
        # ==============================================