"""
语音转文字模块
功能：从 .all.bar.mp4 视频中提取音频，分段后调用 SiliconFlow ASR API 转为文字
输出：合并所有分段文字为 .all.bar.tran.txt
"""

import asyncio
import os
import glob
import hashlib
import time
import yaml
from dataclasses import dataclass, field
from typing import List, Optional

import requests


# ==================== 配置 ====================

def _resolve_transcription_config_path():
    """解析转录配置文件路径"""
    env_path = os.environ.get('TRANSCRIPTION_CONFIG_PATH')
    if env_path:
        return env_path
    cwd_path = os.path.join(os.getcwd(), 'transcription_config.yaml')
    if os.path.isfile(cwd_path):
        return cwd_path
    return os.path.join(os.path.dirname(__file__), 'transcription_config.yaml')


TRANSCRIPTION_CONFIG_PATH = _resolve_transcription_config_path()


_config_cache = None
_config_cache_mtime = 0


def load_transcription_config() -> dict:
    """从独立的 transcription_config.yaml 加载配置（带 mtime 缓存）"""
    global _config_cache, _config_cache_mtime

    try:
        mtime = os.path.getmtime(TRANSCRIPTION_CONFIG_PATH)
        if _config_cache is not None and mtime == _config_cache_mtime:
            return _config_cache

        with open(TRANSCRIPTION_CONFIG_PATH, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
        if not config:
            _config_cache = {'enabled': False}
        else:
            _config_cache = {
                'enabled': config.get('enabled', False),
                'siliconflow_api_key': config.get('siliconflow_api_key', ''),
                'siliconflow_asr_model': config.get('siliconflow_asr_model', 'TeleAI/TeleSpeechASR'),
                'scan_directory': config.get('scan_directory', '/storage'),
                'segment_max_duration_minutes': config.get('segment_max_duration_minutes', 60),
                'segment_max_size_mb': config.get('segment_max_size_mb', 50),
            }
        _config_cache_mtime = mtime
        return _config_cache
    except OSError:
        return _config_cache if _config_cache is not None else {'enabled': False}
    except Exception as e:
        print(f"读取转录配置失败: {e}")
        return _config_cache if _config_cache is not None else {'enabled': False}


# ==================== 任务管理 ====================

@dataclass
class TranscriptionTask:
    """语音转文字任务"""
    video_path: str
    output_txt_path: str
    status: str = 'pending'  # pending, extracting, splitting, transcribing, done, failed
    progress: int = 0         # 0-100
    total_segments: int = 0
    completed_segments: int = 0
    result_text: str = ''
    error_msg: str = ''
    segment_texts: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            'video_path': self.video_path,
            'output_txt_path': self.output_txt_path,
            'status': self.status,
            'progress': self.progress,
            'total_segments': self.total_segments,
            'completed_segments': self.completed_segments,
            'error_msg': self.error_msg,
        }


_tasks: dict = {}


def _make_task_id(video_path: str) -> str:
    return hashlib.md5(video_path.encode()).hexdigest()[:12]


def get_task(task_id: str) -> Optional[TranscriptionTask]:
    return _tasks.get(task_id)


def get_task_by_path(video_path: str) -> Optional[TranscriptionTask]:
    return _tasks.get(_make_task_id(video_path))


def get_all_tasks() -> List[dict]:
    return [t.to_dict() for t in _tasks.values()]


def delete_task(task_id: str) -> bool:
    if task_id in _tasks:
        del _tasks[task_id]
        return True
    return False


# ==================== 文件扫描 ====================

def find_all_bar_mp4_files(base_dir: str) -> List[str]:
    """扫描目录下所有 .all.bar.mp4 文件"""
    pattern = os.path.join(base_dir, '**', '*.all.bar.mp4')
    files = glob.glob(pattern, recursive=True)
    files.sort(key=lambda f: os.path.getmtime(f), reverse=True)
    return files


def _get_output_txt_path(video_path: str) -> str:
    base = video_path[:-len('.mp4')] if video_path.endswith('.mp4') else video_path
    return base + '.tran.txt'


def _get_file_size_mb(file_path: str) -> float:
    if not os.path.exists(file_path):
        return 0.0
    return os.path.getsize(file_path) / (1024 * 1024)


def get_tran_content(video_path: str, allowed_dir: str) -> Optional[str]:
    """
    安全获取转录结果文本内容
    返回 None 表示文件不存在或路径越权
    """
    tran_txt = _get_output_txt_path(video_path)

    real_tran = os.path.realpath(tran_txt)
    real_allowed = os.path.realpath(allowed_dir)
    if not real_tran.startswith(real_allowed + os.sep):
        return None

    if not os.path.exists(tran_txt):
        return None

    with open(tran_txt, 'r', encoding='utf-8') as f:
        return f.read()


# ==================== ffmpeg 工具 ====================

async def _run_ffmpeg(args: list) -> tuple:
    """异步执行 ffmpeg 命令（参数列表模式，无命令注入风险）"""
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()
    return stdout, stderr, process.returncode


async def _get_audio_duration(audio_path: str) -> float:
    """异步获取音频时长（秒）"""
    process = await asyncio.create_subprocess_exec(
        'ffprobe', '-v', 'error', '-show_entries', 'format=duration',
        '-of', 'default=noprint_wrappers=1:nokey=1', audio_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, _ = await process.communicate()
    try:
        return float(stdout.decode().strip())
    except (ValueError, UnicodeDecodeError):
        return 0.0


async def extract_audio(video_path: str, output_audio_path: str) -> bool:
    """从视频中提取音频为 mp3（64kbps 单声道）"""
    args = [
        'ffmpeg', '-y', '-i', video_path,
        '-vn', '-acodec', 'libmp3lame',
        '-b:a', '64k', '-ac', '1', '-ar', '16000',
        output_audio_path
    ]
    _, stderr, returncode = await _run_ffmpeg(args)
    if returncode != 0:
        print(f"提取音频失败: {stderr.decode('utf-8', errors='ignore')}")
        return False
    return os.path.exists(output_audio_path)


async def split_audio_segments(audio_path: str, output_dir: str,
                                max_minutes: int = 60,
                                max_mb: int = 50) -> List[str]:
    """将音频文件按约束条件分段"""
    duration_sec = await _get_audio_duration(audio_path)
    file_size_mb = _get_file_size_mb(audio_path)

    if duration_sec <= 0:
        print(f"无法获取音频时长: {audio_path}")
        return [audio_path]

    max_duration_sec = max_minutes * 60

    if file_size_mb > 0:
        kb_per_sec = (file_size_mb * 1024) / duration_sec
        if kb_per_sec > 0:
            max_size_sec = (max_mb * 1024) / kb_per_sec
            max_duration_sec = min(max_duration_sec, max_size_sec)

    max_duration_sec = max(max_duration_sec, 60)

    segment_count = max(1, int((duration_sec + max_duration_sec - 1) / max_duration_sec))
    actual_segment_duration = int(duration_sec / segment_count) + 1

    print(f"音频时长 {duration_sec:.1f}s, 分为 {segment_count} 段, 每段 {actual_segment_duration}s")

    base_name = os.path.splitext(os.path.basename(audio_path))[0]
    os.makedirs(output_dir, exist_ok=True)

    output_pattern = os.path.join(output_dir, f"{base_name}_seg_%03d.mp3")

    args = [
        'ffmpeg', '-y', '-i', audio_path,
        '-f', 'segment', '-segment_time', str(actual_segment_duration),
        '-c', 'copy', output_pattern
    ]
    _, stderr, returncode = await _run_ffmpeg(args)
    if returncode != 0:
        print(f"音频分段失败: {stderr.decode('utf-8', errors='ignore')}")
        return [audio_path]

    segment_files = sorted(glob.glob(os.path.join(output_dir, f"{base_name}_seg_*.mp3")))
    print(f"生成 {len(segment_files)} 个分段文件")
    return segment_files


# ==================== API 调用 ====================

def transcribe_segment(api_key: str, model: str, segment_path: str,
                       max_retries: int = 2) -> str:
    """调用 SiliconFlow ASR API 将音频转为文字（含指数退避重试）"""
    url = 'https://api.siliconflow.cn/v1/audio/transcriptions'

    last_error = None
    for attempt in range(max_retries + 1):
        try:
            with open(segment_path, 'rb') as f:
                files = {'file': (os.path.basename(segment_path), f, 'audio/mpeg')}
                data = {'model': model}
                headers = {'Authorization': f'Bearer {api_key}'}

                response = requests.post(url, headers=headers, files=files, data=data, timeout=300)
                response.raise_for_status()
                result = response.json()
                return result.get('text', '')
        except requests.exceptions.RequestException as e:
            last_error = e
            if attempt < max_retries:
                wait = 2 ** attempt  # 1s, 2s 指数退避
                print(f"API 调用失败 [{segment_path}] (第 {attempt + 1} 次重试), {wait}s 后重试: {e}")
                time.sleep(wait)
            else:
                print(f"API 调用失败 [{segment_path}] (已重试 {max_retries} 次): {e}")
                if hasattr(e, 'response') and e.response is not None:
                    print(f"Response: {e.response.text[:500]}")
    raise last_error


async def _transcribe_segment_async(api_key: str, model: str, segment_path: str) -> str:
    """异步转录单个音频分段（在独立线程中运行同步 requests）"""
    return await asyncio.to_thread(transcribe_segment, api_key, model, segment_path)


def merge_transcriptions(segment_texts: List[str], output_txt_path: str):
    """将多段文字合并写入 txt 文件"""
    os.makedirs(os.path.dirname(output_txt_path), exist_ok=True)
    with open(output_txt_path, 'w', encoding='utf-8') as f:
        for i, text in enumerate(segment_texts):
            if i > 0:
                f.write('\n')
            f.write(text)
    print(f"转录结果已保存到: {output_txt_path}")


# ==================== 主流程 ====================

async def run_transcription(video_path: str, config: dict) -> str:
    """
    执行完整的语音转文字流程

    Args:
        video_path: 视频文件路径
        config: 转录配置字典（来自 load_transcription_config）

    Returns:
        任务 ID
    """
    api_key = config.get('siliconflow_api_key', '')
    model = config.get('siliconflow_asr_model', 'TeleAI/TeleSpeechASR')
    max_minutes = config.get('segment_max_duration_minutes', 60)
    max_mb = config.get('segment_max_size_mb', 50)

    output_txt_path = _get_output_txt_path(video_path)
    task_id = _make_task_id(video_path)

    if task_id in _tasks:
        return task_id

    task = TranscriptionTask(video_path=video_path, output_txt_path=output_txt_path)
    _tasks[task_id] = task

    # 限制 _tasks 最大条目数，防止内存泄漏
    if len(_tasks) > 200:
        stale_keys = list(_tasks.keys())[:len(_tasks) - 200]
        for k in stale_keys:
            del _tasks[k]

    if os.path.exists(output_txt_path):
        with open(output_txt_path, 'r', encoding='utf-8') as f:
            task.result_text = f.read()
        task.status = 'done'
        task.progress = 100
        return task_id

    seg_dir = video_path + '.segments'
    audio_path = video_path[:-len('.mp4')] + '.audio.mp3' if video_path.endswith('.mp4') else video_path + '.audio.mp3'
    segment_files = []
    need_split = False

    try:
        task.status = 'extracting'
        task.progress = 5

        if not os.path.exists(audio_path) or _get_file_size_mb(audio_path) <= 0:
            success = await extract_audio(video_path, audio_path)
            if not success:
                task.status = 'failed'
                task.error_msg = '音频提取失败'
                return task_id

        duration_sec = await _get_audio_duration(audio_path)
        file_size_mb = _get_file_size_mb(audio_path)

        need_split = (duration_sec > max_minutes * 60) or (file_size_mb > max_mb)

        if not need_split:
            segment_files = [audio_path]
            task.total_segments = 1
        else:
            task.status = 'splitting'
            task.progress = 10
            segment_files = await split_audio_segments(audio_path, seg_dir, max_minutes, max_mb)
            task.total_segments = len(segment_files)

        task.status = 'transcribing'
        task.segment_texts = []

        coros = [_transcribe_segment_async(api_key, model, seg_path) for seg_path in segment_files]
        results = await asyncio.gather(*coros, return_exceptions=True)
        for idx, result in enumerate(results):
            if isinstance(result, Exception):
                task.segment_texts.append('')
                print(f"转录分段 {idx} 失败: {result}")
            else:
                task.segment_texts.append(result)
            task.completed_segments = idx + 1
            task.progress = 10 + int(85 * (idx + 1) / len(segment_files))

        merge_transcriptions(task.segment_texts, output_txt_path)
        task.result_text = '\n'.join(task.segment_texts)
        task.segment_texts = []  # 已合并到文件，释放分段文本内存
        task.status = 'done'
        task.progress = 100

        if need_split:
            for seg_path in segment_files:
                try:
                    os.remove(seg_path)
                except OSError:
                    pass
            try:
                os.rmdir(seg_dir)
            except OSError:
                pass

    except Exception as e:
        task.status = 'failed'
        task.error_msg = str(e)
        print(f"转录任务失败 [{video_path}]: {e}")

    return task_id
