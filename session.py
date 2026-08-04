import asyncio
import datetime
import os
import subprocess
import sys
import time
import traceback
from asyncio import Task
from typing import Optional

import dateutil.parser

from commons import BINARY_PATH
from recorder_config import RecoderRoom
from highlight_generator import HighlightGenerator, generate_highlight_video, HighlightResult


def _fmt_ts():
    """毫秒级时间戳，用于调试日志"""
    now = datetime.datetime.now()
    return now.strftime("%Y-%m-%d %H:%M:%S.") + f"{now.microsecond // 1000:03d}"


def log_debug(msg):
    """统一调试日志入口：带毫秒时间戳并立即 flush，确保 docker logs 实时可见。

    约定：所有业务阶段日志都走这里，便于后续 grep 检索与时间线分析。
    """
    print(f"[{_fmt_ts()}] {msg}")
    sys.stdout.flush()


def check_nvidia_gpu():
    """检测 NVIDIA 显卡，使用 nvidia-smi 作为主要检测方式

    Returns:
        tuple: (has_gpu: bool, gpu_name: str or None)
    """
    # 方法1: 直接调用 nvidia-smi（最可靠）
    try:
        result = subprocess.run(
            ['nvidia-smi', '--query-gpu=name', '--format=csv,noheader'],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            gpu_name = result.stdout.strip().split('\n')[0]
            return True, gpu_name
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # 方法2: 检查 ffmpeg 是否支持 nvenc 硬件编码
    try:
        result = subprocess.run(
            ['ffmpeg', '-hide_banner', '-codecs'],
            capture_output=True,
            text=True,
            timeout=5
        )
        if 'h264_nvenc' in result.stdout or 'hevc_nvenc' in result.stdout:
            return True, "GPU (ffmpeg nvenc)"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    return False, None


async def async_wait_output(command):
    """执行 shell 命令并返回 (returncode, stdout, stderr)。

    返回退出码后，调用方可以判断子进程是否成功，失败时输出错误信息，
    避免"命令失败但流程静默继续/静默终止"的问题。
    """
    print(f"running: {command}")
    sys.stdout.flush()
    process = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()
    sys.stdout.flush()
    sys.stderr.flush()
    return process.returncode, stdout, stderr


def print_tail(file_path, n=25):
    """打印文件末尾 n 行，用于定位子进程失败原因（如 extras.log / video.log）"""
    try:
        if not os.path.exists(file_path):
            print(f"  ({file_path} 不存在)")
            return
        with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
        print(f"  --- {file_path} 末尾 {min(n, len(lines))} 行 ---")
        for line in lines[-n:]:
            print(f"  {line.rstrip()}")
    except Exception as e:
        print(f"  读取日志失败: {e}")


class Video:
    base_path: str
    session_id: str
    video_length: float
    room_id: int
    video_resolution: str
    video_resolution_x: int
    video_resolution_y: int
    video_length_flv: float
    video_fps: float  # 新增：视频帧率

    def __init__(self, file_closed_event_json):
        flv_name = file_closed_event_json['EventData']['RelativePath']
        self.base_path = os.path.abspath(flv_name.rpartition('.')[0])
        self.session_id = file_closed_event_json["EventData"]["SessionId"]
        self.room_id = file_closed_event_json["EventData"]["RoomId"]
        self.video_length = file_closed_event_json["EventData"]["Duration"]

    def flv_file_path(self):
        return self.base_path + ".flv"

    def xml_file_path(self):
        return self.base_path + ".xml"

    async def gen_thumbnail(self, he_time, png_file_path, video_log_path):
        ffmpeg_command_img = f"ffmpeg -y -ss {he_time} -i \"{self.flv_file_path()}\" -vframes 1 \"{png_file_path}\"" \
                             f" >> \"{video_log_path}\" 2>&1"
        await async_wait_output(ffmpeg_command_img)

    async def query_meta(self):
        log_debug(f"[query_meta] 开始读取视频元数据: {self.flv_file_path()}")
        _, video_length_str, _ = await async_wait_output(
            f'ffprobe -v error -show_entries format=duration '
            f'-of default=noprint_wrappers=1:nokey=1 "{self.flv_file_path()}"'
        )
        _, video_resolution_str, _ = await async_wait_output(
            f'ffprobe -v error -select_streams v:0 -show_entries stream=width,height '
            f'-of csv=s=x:p=0 "{self.flv_file_path()}"'
        )
        self.video_length_flv = float(video_length_str.decode('utf-8').strip())
        self.video_resolution = str(video_resolution_str.decode('utf-8').strip())
        video_resolutions = self.video_resolution.split("x")
        self.video_resolution_x, self.video_resolution_y = int(video_resolutions[0]), int(video_resolutions[1])

        # 获取帧率
        _, fps_str, _ = await async_wait_output(
            f'ffprobe -v error -select_streams v:0 -show_entries stream=r_frame_rate '
            f'-of default=noprint_wrappers=1:nokey=1 "{self.flv_file_path()}"'
        )
        fps_value = fps_str[0].decode('utf-8').strip()

        # 处理分数形式的帧率（如30000/1001）
        if '/' in fps_value:
            num, den = map(int, fps_value.split('/'))
            self.video_fps = num / den
        else:
            self.video_fps = float(fps_value)

        print(f"视频帧率: {self.video_fps}fps")
        log_debug(f"[query_meta] 完成: 时长={self.video_length_flv}s 分辨率={self.video_resolution} "
                  f"帧率={self.video_fps}fps -> {self.flv_file_path()}")

class Session:
    session_id: str
    start_time: time.time
    end_time: Optional[datetime.datetime]
    room_id: int
    videos: [Video]
    total_length: float
    notify_length: int
    length_alert: bool
    he_time: Optional[float]
    early_video_path: Optional[str]
    room_name: str
    room_title: str
    room_config: RecoderRoom

    def __init__(self, session_start_event_json, notify_length=60, room_config=None):
        if room_config is None:
            self.room_config = RecoderRoom({})
        else:
            self.room_config = room_config
        self.start_time = dateutil.parser.isoparse(session_start_event_json["EventTimestamp"])
        self.session_id = session_start_event_json["EventData"]["SessionId"]
        self.room_id = session_start_event_json["EventData"]["RoomId"]
        self.end_time = None
        self.notify_length = notify_length
        self.length_alert = False
        self.total_length = 0
        self.videos = []
        self.he_time = None
        self.early_video_path = None
        self.process_update(session_start_event_json)
        self.upload_task: Optional[Task] = None

    def process_update(self, update_json):
        self.room_name = update_json["EventData"]["Name"]
        self.room_title = update_json["EventData"]["Title"]
        if update_json["EventType"] == "SessionEnded":
            self.end_time = dateutil.parser.isoparse(update_json["EventTimestamp"])

    def _log(self, msg):
        """会话级日志：自动带上 [room_id@session_id] 前缀，方便按房间/场次检索"""
        log_debug(f"[{self.room_id}@{self.session_id}] {msg}")

    async def add_video(self, video):
        log_debug(f"[{self.room_id}@{self.session_id}] 添加视频片段: {video.flv_file_path()} "
                  f"(录制时长={video.video_length}s)")
        try:
            await video.query_meta()
        except ValueError:
            print(traceback.format_exc())
            print(f"video corrupted, skipping: {video.flv_file_path()}")
            self._log(f"视频片段损坏已跳过: {video.flv_file_path()}")
            return
        self.videos += [video]
        new_length = self.total_length + video.video_length
        if (new_length // self.notify_length) != (self.total_length // self.notify_length):
            self.length_alert = True
        self.total_length += new_length
        self._log(f"视频片段已加入，当前共 {len(self.videos)} 个，累计时长 {self.total_length:.1f}s")

    def output_base_path(self):
        return self.videos[0].base_path + ".all"

    def output_path(self):
        return {
            "xml": self.output_base_path() + ".xml",
            "clean_xml": self.output_base_path() + ".clean.xml",
            "ass": self.output_base_path() + ".ass",
            "early_video": self.output_base_path() + ".flv",
            "danmaku_video": self.output_base_path() + ".bar.mp4",
            "highlight_video": self.output_base_path() + ".bar.highlight.mp4",
            "concat_file": self.output_base_path() + ".concat.txt",
            "thumbnail": self.output_base_path() + ".thumb.png",
            "he_graph": self.output_base_path() + ".he.png",
            "he_file": self.output_base_path() + ".he.txt",
            "he_range": self.output_base_path() + ".he_range.txt",
            "sc_file": self.output_base_path() + ".sc.txt",
            "sc_srt": self.output_base_path() + ".sc.srt",
            "he_pos": self.output_base_path() + ".he_pos.txt",
            "extras_log": self.output_base_path() + ".extras.log",
            "video_log": self.output_base_path() + ".video.log",
            "highlight_log": self.output_base_path() + ".highlight.log",
        }

    async def merge_xml(self):
        self._log(f"▶ 阶段 merge_xml 开始：合并 {len(self.videos)} 个弹幕 xml -> {self.output_path()['xml']}")
        xmls = ' '.join(['"' + video.xml_file_path() + '"' for video in self.videos])
        danmaku_merge_command = \
            f"python3 -m danmaku_tools.merge_danmaku " \
            f"{xmls} " \
            f"--video_time \".flv\" " \
            f"--output \"{self.output_path()['xml']}\" " \
            f">> \"{self.output_path()['extras_log']}\" 2>&1"
        t0 = time.time()
        await async_wait_output(danmaku_merge_command)
        self._log(f"✔ 阶段 merge_xml 完成（耗时 {time.time() - t0:.1f}s）")
        if not os.path.exists(self.output_path()['xml']):
            self._log(f"[警告] merge_xml 后未找到输出文件: {self.output_path()['xml']}")

    async def clean_xml(self):
        self._log(f"▶ 阶段 clean_xml 开始：{self.output_path()['xml']} -> {self.output_path()['clean_xml']}")
        danmaku_clean_command = \
            f"python3 -m danmaku_tools.clean_danmaku " \
            f"{self.output_path()['xml']} " \
            f"--output \"{self.output_path()['clean_xml']}\" " \
            f">> \"{self.output_path()['extras_log']}\" 2>&1"
        t0 = time.time()
        await async_wait_output(danmaku_clean_command)
        self._log(f"✔ 阶段 clean_xml 完成（耗时 {time.time() - t0:.1f}s）")
        if not os.path.exists(self.output_path()['clean_xml']):
            self._log(f"[警告] clean_xml 后未找到输出文件: {self.output_path()['clean_xml']}")

    async def process_xml(self):
        self._log(f"▶ 阶段 process_xml (danmaku_energy_map) 开始：输入 {self.output_path()['clean_xml']}")
        danmaku_extras_command = \
            f"python3 -m danmaku_tools.danmaku_energy_map " \
            f"--graph \"{self.output_path()['he_graph']}\" " \
            f"--he_map \"{self.output_path()['he_file']}\" " \
            f"--sc_list \"{self.output_path()['sc_file']}\" " \
            f"--he_time \"{self.output_path()['he_pos']}\" " \
            f"--sc_srt \"{self.output_path()['sc_srt']}\" " \
            f"--he_range \"{self.output_path()['he_range']}\" " + \
            (
                f"--user_dict \"{self.room_config.he_user_dict}\" "
                if self.room_config.he_user_dict is not None else ""
            ) + \
            (
                f"--regex_rules \"{self.room_config.he_regex_rules}\" "
                if self.room_config.he_regex_rules is not None else ""
            ) + \
            f"\"{self.output_path()['clean_xml']}\" " \
            f">> \"{self.output_path()['extras_log']}\" 2>&1"
        t0 = time.time()
        returncode, _, _ = await async_wait_output(danmaku_extras_command)
        self._log(f"  danmaku_energy_map 退出码={returncode}（耗时 {time.time() - t0:.1f}s）")
        if returncode != 0:
            # danmaku_energy_map 失败时（如依赖缺失、数据异常）不再静默，
            # 打印退出码与 extras.log 尾部，便于定位原因
            print(f"[警告] danmaku_energy_map 退出码 {returncode}，请检查 extras.log 确认失败原因")
            print_tail(self.output_path()['extras_log'])
        # 读取高能时间点；文件缺失/内容非法时使用默认值 0，
        # 避免异常向上传播导致后续视频压制流程被整体阻断
        try:
            with open(self.output_path()['he_pos'], 'r') as file:
                he_time_str = file.readline().strip()
            if not he_time_str:
                raise ValueError("he_pos 文件为空")
            self.he_time = float(he_time_str)
            self._log(f"✔ 阶段 process_xml 完成：高能时间点 he_time={self.he_time}s")
        except Exception as e:
            print(f"[警告] 读取高能时间点失败: {e}，使用默认值 0")
            self.he_time = 0.0
            self._log(f"⚠ 阶段 process_xml 使用默认 he_time=0.0（原因: {e}）")

    def generate_concat(self):
        concat_text = "\n".join([f"file '{video.flv_file_path()}'" for video in self.videos])
        with open(self.output_path()['concat_file'], 'w') as concat_file:
            concat_file.write(concat_text)
        self._log(f"已生成 concat 列表: {self.output_path()['concat_file']} "
                  f"（{len(self.videos)} 个片段）")

    async def process_thumbnail(self):
        self._log(f"▶ 阶段 process_thumbnail 开始：he_time={self.he_time}")
        local_he_time = self.he_time
        thumbnail_generated = False
        target_index = -1
        for i, video in enumerate(self.videos):
            if local_he_time < video.video_length_flv:
                target_index = i
                await video.gen_thumbnail(local_he_time, self.output_path()['thumbnail'],
                                          self.output_path()['video_log'])
                thumbnail_generated = True
                break
            local_he_time -= video.video_length_flv
        if not thumbnail_generated:  # Rare case where he_pos is after the last video
            print(f"{self.output_path()['video']}: thumbnail at {local_he_time} cannot be found")
            self._log(f"[警告] 高能时间点超出所有视频范围，回退到最后一段视频中点截图")
            await self.videos[-1].gen_thumbnail(
                self.videos[-1].video_length_flv / 2,
                self.output_path()['thumbnail'],
                self.output_path()['video_log']
            )
        else:
            self._log(f"✔ 阶段 process_thumbnail 完成：在第 {target_index + 1} 段视频截图 -> "
                      f"{self.output_path()['thumbnail']}")

    def get_resolution(self):
        video_res_sorted = list(reversed([
            (video.video_resolution_x / video.video_resolution_y,
             video.video_resolution_x,
             video.video_resolution_y)
            for video in self.videos
        ]))  # prioritize wider, higher-res format
        video_res_x = video_res_sorted[0][1]
        video_res_y = video_res_sorted[0][2]
        # try to scale to at least 1920x1080 or 1080x1920
        if video_res_x > video_res_y:
            if video_res_x < 1920:
                video_res_y = video_res_y * 1920 // video_res_x
                video_res_x = 1920
        else:
            if video_res_y < 1920:
                video_res_x = video_res_x * 1920 // video_res_y
                video_res_y = 1920
        return video_res_x, video_res_y

    async def process_danmaku(self):
        video_res_x, video_res_y = self.get_resolution()
        font_size = max(video_res_x, video_res_y) * 55 // 1920
        print(f"font_size: {font_size}")
        self._log(f"▶ 阶段 process_danmaku 开始：分辨率={video_res_x}x{video_res_y} 字号={font_size} "
                  f"-> {self.output_path()['ass']}")
        danmaku_conversion_command = \
            f"{BINARY_PATH}DanmakuFactory/DanmakuFactory " \
            f"-x {video_res_x} " \
            f"-y {video_res_y} " \
            f"--ignore-warnings " \
            f"-o \"{self.output_path()['ass']}\" " \
            f"-i \"{self.output_path()['clean_xml']}\" " \
            f"--fontname \"Noto Sans CJK SC\" -S {font_size} -O 255 -L 1 -D 1 --showusernames true --showmsgbox false" \
            f">> \"{self.output_path()['extras_log']}\" 2>&1"
        t0 = time.time()
        returncode, _, _ = await async_wait_output(danmaku_conversion_command)
        self._log(f"  DanmakuFactory 退出码={returncode}（耗时 {time.time() - t0:.1f}s）")
        if returncode != 0:
            print(f"[警告] DanmakuFactory 退出码 {returncode}，弹幕字幕(.ass)可能未生成")
            print_tail(self.output_path()['extras_log'])
        else:
            self._log(f"✔ 阶段 process_danmaku 完成：弹幕字幕 {self.output_path()['ass']}")

    async def process_early_video(self):
        self._log(f"▶ 阶段 process_early_video 开始")
        if len(self.videos) == 1:
            # 修复：flv_file_path 是方法，需要加括号调用得到路径字符串
            self.early_video_path = self.videos[0].flv_file_path()
            self._log(f"单片段直接复用原视频: {self.early_video_path}")
        format_check = True
        ref_video_res = self.videos[0].video_resolution
        for video in self.videos:
            if video.video_resolution != ref_video_res:
                format_check = False
                break
        if not format_check:
            self._log(f"[警告] 片段分辨率不一致（基准 {ref_video_res}），跳过早期视频合并")
            return
        ffmpeg_command = f'''ffmpeg -y \
        -f concat \
        -safe 0 \
        -i "{self.output_path()['concat_file']}" \
        -c copy "{self.output_path()['early_video']}" >> "{self.output_path()["video_log"]}" 2>&1'''
        t0 = time.time()
        returncode, _, _ = await async_wait_output(ffmpeg_command)
        self._log(f"  早期视频合并退出码={returncode}（耗时 {time.time() - t0:.1f}s）")
        if returncode != 0:
            print(f"[警告] 早期视频合并失败 (退出码 {returncode})")
            print_tail(self.output_path()['video_log'])
            return
        self.early_video_path = self.output_path()['early_video']
        self._log(f"✔ 阶段 process_early_video 完成: {self.early_video_path}")

    async def process_video(self):
        total_time = sum([video.video_length_flv for video in self.videos])
        self._log(f"===== 阶段 process_video (弹幕版压制) 开始 =====")
        self._log(f"输入: {len(self.videos)} 个片段, 总时长={total_time:.1f}s, "
                  f"输出={self.output_path()['danmaku_video']}")

        # === 获取视频分辨率和帧率 ===
        # 假设第一个视频代表整个session的分辨率和帧率
        reference_video = self.videos[0]

        # 获取视频帧率（需要添加到Video类的query_meta方法中）
        # 这里假设video_fps已经在Video类中定义
        video_fps = getattr(reference_video, 'video_fps', 30.0)  # 默认30fps

        video_res_x, video_res_y = self.get_resolution()
        self._log(f"分辨率={video_res_x}x{video_res_y} 帧率={video_fps}fps")

        # === 根据分辨率和帧率计算推荐码率 ===
        # B站推荐码率参考：https://www.bilibili.com/read/cv17931353
        # 分辨率码率基准（基于30fps）
        resolution_bitrate_base = {
            # 分辨率 (宽x高): 推荐码率 (Kbps)
            (1920, 1080): 2500,  # 1080p
            (1280, 720): 1500,  # 720p
            (854, 480): 1200,  # 480p
            (640, 360): 800,  # 360p
        }

        # 帧率调整系数（60fps需要约1.5倍码率）
        fps_adjustment = 1.0 + (video_fps - 30) / 60 * 0.5
        fps_adjustment = max(0.8, min(1.5, fps_adjustment))  # 限制在0.8-1.5倍

        # 查找最接近的分辨率基准
        recommended_bitrate = None
        min_distance = float('inf')

        for (res_w, res_h), base_bitrate in resolution_bitrate_base.items():
            # 计算分辨率差异（考虑宽高比）
            distance = abs(video_res_x - res_w) + abs(video_res_y - res_h)
            if distance < min_distance:
                min_distance = distance
                recommended_bitrate = base_bitrate

        # 如果没有匹配，根据像素数量估算
        if recommended_bitrate is None:
            total_pixels = video_res_x * video_res_y
            # 基于1080p (1920x1080=2,073,600像素) 6000Kbps的基准
            base_1080p_pixels = 1920 * 1080
            base_1080p_bitrate = 2500
            recommended_bitrate = int(total_pixels / base_1080p_pixels * base_1080p_bitrate)

        # 应用帧率调整
        recommended_bitrate = int(recommended_bitrate * fps_adjustment)

        # === 码率范围限制 ===
        # B站上传限制：最大8000Kbps，最小根据分辨率调整
        MAX_VIDEO_BITRATE = 18000  # Kbps（B站重编码上限）

        # 根据分辨率设置最小码率
        if video_res_x >= 1920 or video_res_y >= 1080:
            MIN_VIDEO_BITRATE = 3500  # 1080p及以上
        elif video_res_x >= 1280 or video_res_y >= 720:
            MIN_VIDEO_BITRATE = 2000  # 720p
        else:
            MIN_VIDEO_BITRATE = 1200  # 低分辨率

        # 确保码率在合理范围内
        video_bitrate = int(max(MIN_VIDEO_BITRATE, min(MAX_VIDEO_BITRATE, recommended_bitrate)))
        self._log(f"码率={video_bitrate}Kbps (推荐基线={recommended_bitrate}, 帧率系数={fps_adjustment:.2f}, "
                  f"范围 {MIN_VIDEO_BITRATE}~{MAX_VIDEO_BITRATE})")

        # ======== 核心优化：GPU 硬件检测与硬件加速策略 ========
        # 检测系统中是否存在独立显卡 (GPU)，使用 nvidia-smi 作为主要检测方式
        has_gpu, gpu_name = check_nvidia_gpu()
        print(f"GPU检测: has_gpu={has_gpu}, 显卡={gpu_name}")

        if has_gpu:
            print("检测到独立显卡 (GPU)，将使用硬件加速。")
        else:
            print("未检测到独立显卡 (GPU)，将使用CPU进行处理。")

        # 1. 硬件解码 (Hardware Decoding)
        hwaccel_decode = "-hwaccel auto " if has_gpu else ""

        # 2. 硬件编码 (Hardware Encoding)
        encoder_params = " -c:v h264_nvenc -preset slow -threads 0 " if has_gpu else " -c:v libx264 -preset medium -threads 0 "
        # ========================================================

        # === 输入可用性检查与降级 ===
        # 背景图（he_graph）可能因 danmaku_energy_map 失败而缺失，缺失时用纯黑背景，
        # 保证压制流程不会被前置步骤的失败阻断
        he_graph_path = self.output_path()['he_graph']
        ass_path = self.output_path()['ass']
        if os.path.exists(he_graph_path):
            bg_input = f"-loop 1 -t {total_time} -i \"{he_graph_path}\""
            self._log(f"背景图: 存在 {he_graph_path}")
        else:
            print(f"[警告] 高能背景图不存在: {he_graph_path}，压制将使用纯黑背景")
            bg_input = f"-f lavfi -i color=c=black:s={video_res_x}x{video_res_y}:d={total_time}"
            self._log(f"背景图: 缺失，降级为纯黑背景")

        # 滤镜链主体（背景图动态入场效果，与原逻辑一致）
        filter_base = f'''[1:v]scale={video_res_x}:{video_res_y}:force_original_aspect_ratio=decrease,pad={video_res_x}:{video_res_y}:-1:-1:color=black[v_fixed];
[0:v][v_fixed]scale2ref=iw:iw*(main_h/main_w)[color][ref];
[color]split[color1][color2];
[color1]hue=s=0[gray];
[color2]negate=negate_alpha=1[color_neg];
[gray]negate=negate_alpha=1[gray_neg];
color=black:d={total_time}[black];
[black][ref]scale2ref[blackref][ref2];
[blackref]split[blackref1][blackref2];
[color_neg][blackref1]overlay=x=t/{total_time}*W-W[color_crop_neg];
[gray_neg][blackref2]overlay=x=t/{total_time}*W[gray_crop_neg];
[color_crop_neg]negate=negate_alpha=1[color_crop];
[gray_crop_neg]negate=negate_alpha=1[gray_crop];
[ref2][color_crop]overlay=y=main_h-overlay_h[out_color];
[out_color][gray_crop]overlay=y=main_h-overlay_h[out]'''

        # 弹幕字幕（.ass）可能因 DanmakuFactory 失败而缺失，缺失时跳过字幕滤镜
        if os.path.exists(ass_path):
            filter_complex = f"{filter_base};[out]ass='{ass_path}'[out_sub]"
            video_map = "[out_sub]"
            self._log(f"弹幕字幕: 存在 {ass_path}")
        else:
            print(f"[警告] 弹幕字幕文件不存在: {ass_path}，压制视频将不含弹幕字幕")
            filter_complex = filter_base
            video_map = "[out]"
            self._log(f"弹幕字幕: 缺失，降级为无字幕")

        ffmpeg_command = f'''ffmpeg -y {bg_input} \
        {hwaccel_decode}-f concat \
        -safe 0 \
        -i "{self.output_path()['concat_file']}" \
        -t {total_time} \
        -filter_complex "{filter_complex}" \
        -map "{video_map}" -map 1:a ''' + \
                         encoder_params + \
                         f'-b:v {video_bitrate}K' + f''' -b:a 320K -ar 44100  "{self.output_path()['danmaku_video']}" \
                    ''' + f'>> "{self.output_path()["video_log"]}" 2>&1'
        self._log(f"编码器={'h264_nvenc(GPU)' if has_gpu else 'libx264(CPU)'} "
                  f"硬件解码={'是' if has_gpu else '否'}，开始压制...")
        t0 = time.time()
        returncode, _, _ = await async_wait_output(ffmpeg_command)
        cost = time.time() - t0
        if returncode != 0:
            print(f"[压制失败] ffmpeg 退出码 {returncode}，详见 video.log")
            print_tail(self.output_path()['video_log'])
            self._log(f"✘ 弹幕版压制失败 (退出码 {returncode}, 耗时 {cost:.1f}s)")
        else:
            out_path = self.output_path()['danmaku_video']
            size_mb = os.path.getsize(out_path) / 1024 / 1024 if os.path.exists(out_path) else 0
            self._log(f"✔ 弹幕版压制完成: {out_path} (耗时 {cost:.1f}s, 大小 {size_mb:.1f}MB)")

    async def gen_early_video(self):
        if len(self.videos) == 0:
            print(f"No video in session for {self.room_id}@{self.start_time}, skip!")
            return
        total_dur = sum(v.video_length_flv for v in self.videos)
        self._log(f"===== gen_early_video 开始（{len(self.videos)} 个片段，总时长 {total_dur:.1f}s）=====")
        session_t0 = time.time()
        # 每步独立 try/except：单步失败只打印告警，不阻断后续步骤，
        # 确保 process_video（弹幕版压制）尽可能照常执行
        async def safe_step(name, coro):
            self._log(f"  ▶ 步骤 {name} 开始")
            t0 = time.time()
            try:
                await coro
                self._log(f"  ✔ 步骤 {name} 完成（耗时 {time.time() - t0:.1f}s）")
            except Exception as e:
                self._log(f"  ✘ 步骤 {name} 失败（耗时 {time.time() - t0:.1f}s）: {e}")
                traceback.print_exc()

        await safe_step("merge_xml", self.merge_xml())
        await safe_step("clean_xml", self.clean_xml())
        await safe_step("process_xml", self.process_xml())
        await safe_step("process_danmaku", self.process_danmaku())
        await safe_step("process_thumbnail", self.process_thumbnail())
        try:
            self._log("  ▶ 步骤 generate_concat 开始")
            t0 = time.time()
            self.generate_concat()
            self._log(f"  ✔ 步骤 generate_concat 完成（耗时 {time.time() - t0:.1f}s）")
        except Exception as e:
            print(f"[警告] generate_concat 失败: {e}")
            traceback.print_exc()
        await safe_step("process_early_video", self.process_early_video())
        self._log(f"===== gen_early_video 结束（总耗时 {time.time() - session_t0:.1f}s，"
                  f"早期视频={'有' if self.early_video_path else '无'}）=====")

    async def gen_danmaku_video(self):
        if len(self.videos) == 0:
            print(f"No video in session for {self.room_id}@{self.start_time}, skip!")
            return
        self._log(f"===== gen_danmaku_video (压制) 开始 =====")
        session_t0 = time.time()
        try:
            await self.process_video()
            self._log(f"===== gen_danmaku_video 完成（耗时 {time.time() - session_t0:.1f}s，"
                      f"输出 {self.output_path()['danmaku_video']}）=====")
        except Exception as e:
            print(f"[压制异常] {self.room_id}@{self.session_id}: {e}")
            traceback.print_exc()

    def load_danmaku_energy_data(self) -> list:
        """加载弹幕能量数据"""
        he_file = self.output_path()['he_file']
        if not os.path.exists(he_file):
            print(f"Dammaku energy file not found: {he_file}")
            return []
        
        try:
            danmaku_data = []
            with open(he_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    # 格式: time, energy
                    parts = line.split(',')
                    if len(parts) >= 2:
                        try:
                            danmaku_data.append({
                                'time': float(parts[0]),
                                'energy': float(parts[1])
                            })
                        except ValueError:
                            continue
            print(f"Loaded {len(danmaku_data)} danmaku energy data points")
            return danmaku_data
        except Exception as e:
            print(f"Error loading danmaku energy data: {e}")
            return []

    async def gen_highlight_video(self, highlight_config: dict = None) -> Optional['HighlightResult']:
        """
        生成高光视频
        
        Args:
            highlight_config: 高光生成配置，包含：
                - enabled: 是否启用
                - qwen_api_key: 阿里云千问 API Key
                - qwen_model: 千问模型
                - whisper_model: Whisper 模型
                - use_gpu: 是否使用 GPU
                - clip_before: 高光前多少秒
                - clip_after: 高光后多少秒
        
        Returns:
            HighlightResult 包含视频路径、总结和高光片段，失败返回 None
        """
        if len(self.videos) == 0:
            print(f"No video in session for {self.room_id}@{self.start_time}, skip highlight generation!")
            return None

        danmaku_video_path = self.output_path()['danmaku_video']
        if not os.path.exists(danmaku_video_path):
            print(f"Danmaku video not found: {danmaku_video_path}")
            return None

        self._log(f"===== gen_highlight_video 开始 =====")
        session_t0 = time.time()

        # 合并默认配置和用户配置
        default_config = {
            'enabled': True,
            'deepseek_api_key': '',
            'deepseek_model': 'deepseek-chat',
            'whisper_model': 'base',
            'use_gpu': True,
            'clip_before': 60,
            'clip_after': 60,
        }
        if highlight_config:
            default_config.update(highlight_config)

        if not default_config.get('enabled', True):
            print("Highlight generation is disabled")
            self._log("高光生成已禁用，跳过")
            return None

        print(f"Generating highlight video for session {self.session_id}")

        # 加载弹幕能量数据
        danmaku_data = self.load_danmaku_energy_data()
        self._log(f"已加载弹幕能量数据点 {len(danmaku_data)} 个")

        # 创建高光生成器并生成视频
        generator = HighlightGenerator(default_config)
        result = await generator.generate_highlight(
            video_path=danmaku_video_path,
            danmaku_data=danmaku_data,
            output_path=self.output_path()['highlight_video'],
            log_path=self.output_path()['highlight_log']
        )

        if result:
            print(f"Highlight video generated: {result.video_path}")
            print(f"Highlight summary: {result.summary}")
            # 保存总结到文件，用于评论
            summary_path = self.output_path()['highlight_video'].replace('.mp4', '.summary.txt')
            with open(summary_path, 'w', encoding='utf-8') as f:
                f.write(result.summary)
            print(f"Summary saved to: {summary_path}")
            self._log(f"✔ 高光视频生成完成: {result.video_path}（耗时 {time.time() - session_t0:.1f}s）")
        else:
            print("Failed to generate highlight video")
            self._log(f"✘ 高光视频生成失败（耗时 {time.time() - session_t0:.1f}s）")

        return result


if __name__ == '__main__':
    BINARY_PATH = "../exes/"
    session_json = {'EventType': 'SessionStarted', 'EventTimestamp': '2021-04-09T22:50:15.301987-07:00',
                    'EventId': '6379acb5-0dfd-465e-bb03-58d9867e7591',
                    'EventData': {'SessionId': 'e3807981-3104-402a-ad71-8d42023c787d', 'RoomId': 128308, 'ShortId': 0,
                                  'Name': '隐染啊', 'Title': '不要自闭挑战', 'AreaNameParent': '娱乐', 'AreaNameChild': '户外'}}
    filenames = ["128308-20210530-014105.flv", "128308-20210530-020536.flv", "128308-20210530-032330.flv"]
    video_json_list = [
        {'EventType': 'FileClosed', 'EventTimestamp': '2021-04-09T23:44:37.128312-07:00',
         'EventId': '114c0b8d-80a3-4d2e-81f5-9d1ba17f4acd',
         'EventData': {'RelativePath': f'128308/{filename}', 'FileSize': 128308,
                       'Duration': 63.646, 'FileOpenTime': '2021-04-09T23:43:32.456413-07:00',
                       'FileCloseTime': '2021-04-09T23:44:37.128288-07:00',
                       'SessionId': '22fa4a41-6e75-4ed6-8352-2a2449eeb252', 'RoomId': 128308, 'ShortId': 0,
                       'Name': '隐染啊', 'Title': '不要自闭挑战', 'AreaNameParent': '娱乐', 'AreaNameChild': '户外'}} for filename in filenames
    ]
    session = Session(session_json)
    video_tasks = []
    for video_json in video_json_list:
        video = Video(video_json)
        asyncio.run(session.add_video(video))

    asyncio.run(session.gen_early_video())
    asyncio.run(session.gen_danmaku_video())