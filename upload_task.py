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
    检查视频大小，如果超过 15GB，则按约 8GB 为一段进行拆分。
    返回拆分后的视频文件路径列表。
    """
    MAX_SIZE = 14 * 1024 * 1024 * 1024  # 15GB 限制阈值
    SPLIT_SIZE = 8 * 1024 * 1024 * 1024 # 8GB 分割单位
    file_size = os.path.getsize(video_path)

    # 如果文件大小合规，直接返回原路径
    if file_size <= MAX_SIZE:
        return [video_path]

    print(f"视频大小 {file_size} 字节超出 14GB 限制，正在按 8GB 分块拆分...")

    # 1. 获取视频总时长 (秒)
    # 尝试一：获取全局容器时长
    cmd_format = f'ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "{video_path}"'
    stdout, stderr = await async_wait_output(cmd_format)
    duration_str = stdout.decode('utf-8').strip()

    # 如果没获取到（为空或者等于 N/A），尝试二：获取视频流的时长
    if not duration_str or duration_str.lower() == 'n/a':
        print("未读取到全局时长，尝试读取视频流时长...")
        cmd_stream = f'ffprobe -v error -select_streams v:0 -show_entries stream=duration -of default=noprint_wrappers=1:nokey=1 "{video_path}"'
        stdout, stderr = await async_wait_output(cmd_stream)
        # 可能会返回多行（如果有多条视频流），我们取第一行
        duration_str = stdout.decode('utf-8').strip().split('\n')[0].strip()

    try:
        duration = float(duration_str)
        print(f"成功获取视频时长: {duration} 秒")
    except Exception as e:
        print(f"获取视频时长依然失败，取消拆分: {e}")
        print(f"ffprobe 诊断信息 -> stdout: '{stdout.decode('utf-8')}' | stderr: '{stderr.decode('utf-8')}'")
        return [video_path]

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
        print("视频拆分失败（未检测到切片文件），尝试上传原视频。")
        return [video_path]

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

    async def upload(self, session_dict: {str: str}):

        if self.danmaku:
            suffix = "弹幕高能版"
        else:
            suffix = "无弹幕版"

        if self.account.line == "auto":
            line = None
        elif self.account.line == "bda2":
            line = Lines.BDA2
        elif self.account.line == "qn":
            line = Lines.QN
        elif self.account.line == "ws":
            line = Lines.WS
        elif self.account.line == "bldsa":
            line = Lines.BLDSA
        else:
            print(f"Unknown line: {self.account.line}, use auto instead.")
            line = None

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

        # =============== 修改点核心：动态构建分 P 列表 ===============
        video_paths = await split_video_if_needed(self.video_path)
        pages = []
        for i, v_path in enumerate(video_paths):
            # 如果发生了切片，且文件数量大于 1，我们在每个 P 后面加上标注区分
            page_title = suffix if len(video_paths) == 1 else f"{suffix} (P{i+1})"
            pages.append(VideoUploaderPage(path=v_path, title=page_title))

        uploader = VideoUploader(
            pages=pages,
            meta=meta,
            credential=self.verify,
            line=line
        )
        # ============================================================

        uploader.add_event_listener("__ALL__", on_progress)
        if self.session_id not in session_dict:
            result = await uploader.start()
            print(f"{meta.title} uploaded: {result}")
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
                "new_web_edit": 1,
                "act_reserve_create": 0,
                "handle_staff": False,
                "topic_grey": 1,
                "no_reprint": 0,
                "subtitles": {
                    "lan": "",
                    "open": 0
                },
                "web_os": 2,
                'videos': videos
            }

            updater = VideoEditor(
                bvid=session_dict[self.session_id],
                meta=meta_dict,
                credential=self.verify
            )
            updater.add_event_listener("__ALL__", on_progress)
            await updater._fetch_configs()

            # =============== 修改点：保护老分 P 数据不被覆盖 ===============
            old_videos = []
            if "videos" in updater._VideoEditor__old_configs:
                old_videos = updater._VideoEditor__old_configs["videos"]
            elif "archive" in updater._VideoEditor__old_configs and "videos" in updater._VideoEditor__old_configs["archive"]:
                old_videos = updater._VideoEditor__old_configs["archive"]["videos"]

            # 将新上传的分 P 追加到老的列表之后
            updater.meta["videos"] = old_videos + videos
            # ============================================================

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
            print(f"{new_title} updated: {result}")
            return updater.bvid