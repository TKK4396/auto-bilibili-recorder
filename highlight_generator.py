"""
高光视频生成模块
功能：从视频中提取音频，转为文字，使用 AI 分析高光片段，生成高光合集视频
"""

import asyncio
import json
import os
import sys
import traceback
from dataclasses import dataclass
from typing import List, Optional, Tuple

# 语音识别和 AI 分析相关导入
try:
    from faster_whisper import WhisperModel
    WHISPER_AVAILABLE = True
except ImportError:
    WHISPER_AVAILABLE = False
    print("Warning: faster-whisper not installed. Speech-to-text will be disabled.")

import requests
DEEPSEEK_AVAILABLE = True  # requests 总是可用


@dataclass
class HighlightSegment:
    """高光片段数据结构"""
    start_time: float  # 开始时间（秒）
    end_time: float    # 结束时间（秒）
    score: float       # 高光分数 (0-1)
    reason: str        # 高光原因描述
    text_snippet: str  # 文字片段


@dataclass
class HighlightResult:
    """高光视频生成结果"""
    video_path: str  # 高光视频路径
    summary: str     # 高光内容文字总结
    highlights: List[HighlightSegment]  # 高光片段列表


async def async_run_command(command: str, log_path: str = None):
    """异步执行命令"""
    print(f"Running: {command}")
    sys.stdout.flush()
    
    if log_path:
        command = f"{command} >> \"{log_path}\" 2>&1"
    
    process = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()
    sys.stdout.flush()
    sys.stderr.flush()
    return stdout, stderr, process.returncode


class HighlightGenerator:
    """高光视频生成器"""
    
    def __init__(self, config: dict = None):
        """
        初始化高光生成器
        
        Args:
            config: 配置字典，包含以下键：
                - deepseek_api_key: DeepSeek API Key
                - deepseek_model: DeepSeek 模型名称（默认 deepseek-chat）
                - whisper_model: Whisper 模型大小（默认 base）
                - use_gpu: 是否使用 GPU（默认 True）
                - clip_before: 高光前多少秒（默认 60）
                - clip_after: 高光后多少秒（默认 60）
                - max_highlights: 最大高光片段数（默认 5）
                - min_segment_gap: 最小片段间隔秒数（默认 30）
        """
        self.config = config or {}
        
        # 默认配置
        self.deepseek_api_key = self.config.get('deepseek_api_key', '')
        self.deepseek_model = self.config.get('deepseek_model', 'deepseek-chat')
        self.whisper_model_size = self.config.get('whisper_model', 'base')
        self.use_gpu = self.config.get('use_gpu', True)
        self.clip_before = self.config.get('clip_before', 60)
        self.clip_after = self.config.get('clip_after', 60)
        self.max_highlights = self.config.get('max_highlights', 5)
        self.min_segment_gap = self.config.get('min_segment_gap', 30)
        self.enabled = self.config.get('enabled', True)
        
        # 初始化 Whisper 模型（延迟加载）
        self._whisper_model = None
    
    @property
    def whisper_model(self):
        """延迟加载 Whisper 模型"""
        if self._whisper_model is None and WHISPER_AVAILABLE:
            device = "cuda" if self.use_gpu else "cpu"
            compute_type = "float16" if self.use_gpu else "int8"
            print(f"Loading Whisper model: {self.whisper_model_size} on {device}")
            self._whisper_model = WhisperModel(
                self.whisper_model_size,
                device=device,
                compute_type=compute_type
            )
        return self._whisper_model
    
    async def extract_audio(self, video_path: str, audio_path: str, log_path: str = None) -> bool:
        """
        从视频中提取音频
        
        Args:
            video_path: 视频文件路径
            audio_path: 输出音频文件路径
            log_path: 日志文件路径
            
        Returns:
            是否成功
        """
        print(f"Extracting audio from {video_path} to {audio_path}")
        
        command = f'ffmpeg -y -i "{video_path}" -vn -acodec libmp3lame -q:a 2 "{audio_path}"'
        stdout, stderr, returncode = await async_run_command(command, log_path)
        
        if returncode != 0:
            print(f"Failed to extract audio: {stderr.decode('utf-8', errors='ignore')}")
            return False
        
        return os.path.exists(audio_path)
    
    def speech_to_text(self, audio_path: str) -> List[dict]:
        """
        语音转文字
        
        Args:
            audio_path: 音频文件路径
            
        Returns:
            转录结果列表，每个元素包含 start, end, text
        """
        if not WHISPER_AVAILABLE:
            print("Whisper not available, skipping speech-to-text")
            return []
        
        if not os.path.exists(audio_path):
            print(f"Audio file not found: {audio_path}")
            return []
        
        print(f"Transcribing audio: {audio_path}")
        
        # GPU/CUDA 诊断信息
        print("[GPU诊断] 开始检测环境...")
        try:
            import torch
            print(f"[GPU诊断] torch.cuda.is_available(): {torch.cuda.is_available()}")
            if torch.cuda.is_available():
                print(f"[GPU诊断] GPU name: {torch.cuda.get_device_name(0)}")
                print(f"[GPU诊断] GPU memory: {torch.cuda.get_device_properties(0).total_memory / 1024**3:.1f} GB")
        except ImportError:
            print("[GPU诊断] torch not installed")
        try:
            import faster_whisper
            print(f"[GPU诊断] faster-whisper version: {faster_whisper.__version__}")
        except Exception:
            print("[GPU诊断] faster-whisper version: unknown")
        print(f"[GPU诊断] use_gpu setting: {self.use_gpu}")
        print(f"[GPU诊断] whisper_model_size: {self.whisper_model_size}")
        
        try:
            segments, info = self.whisper_model.transcribe(
                audio_path,
                language="zh",
                task="transcribe",
                vad_filter=True,
                vad_parameters=dict(min_silence_duration_ms=500)
            )
            
            results = []
            for segment in segments:
                results.append({
                    "start": segment.start,
                    "end": segment.end,
                    "text": segment.text.strip()
                })
            
            print(f"Transcription complete: {len(results)} segments, {info.duration:.2f}s")
            
            # 保存转写结果到JSON文件
            result_path = audio_path.replace(".mp3", ".transcription.json")
            with open(result_path, 'w', encoding='utf-8') as f:
                json.dump({
                    "audio_path": audio_path,
                    "video_duration": info.duration,
                    "segments": results
                }, f, ensure_ascii=False, indent=2)
            print(f"Transcription saved to: {result_path}")
            
            return results
            
        except Exception as e:
            print(f"Transcription error: {e}")
            traceback.print_exc()
            return []
    
    def analyze_with_deepseek(self, transcription: List[dict], danmaku_data: List[dict] = None) -> List[HighlightSegment]:
        """
        使用 DeepSeek 分析文字内容，识别高光片段
        
        Args:
            transcription: 转录结果列表
            danmaku_data: 弹幕能量数据（可选）
            
        Returns:
            高光片段列表
        """
        if not self.deepseek_api_key:
            print("DeepSeek API key not set, skipping AI analysis")
            return []
        
        if not transcription:
            print("No transcription to analyze")
            return []
        
        # 构建文本内容
        text_content = "\n".join([
            f"[{seg['start']:.1f}s-{seg['end']:.1f}s] {seg['text']}"
            for seg in transcription
        ])
        
        # 构建弹幕信息
        danmaku_info = ""
        if danmaku_data:
            danmaku_info = "\n弹幕能量数据（时间点，能量值）：\n" + "\n".join([
                f"{d.get('time', 0):.1f}s: {d.get('energy', 0):.2f}"
                for d in danmaku_data[:50]  # 只取前 50 条
            ])
        
        # 构建提示词
        prompt = f"""你是一个专业的直播内容分析师。请分析以下直播的文字记录，识别其中的高光片段。

文字记录（带时间戳）：
{text_content}

{danmaku_info}

请找出 3-5 个最精彩、最有趣、最值得剪辑的高光片段。每个片段需要：
1. 明确的开始和结束时间（秒）
2. 高光原因（如搞笑、精彩操作、感人瞬间等）
3. 高光分数（0-1）

请以 JSON 格式返回结果，格式如下：
{{
    "highlights": [
        {{
            "start_time": 120.5,
            "end_time": 185.3,
            "score": 0.9,
            "reason": "主播做了一个非常搞笑的操作",
            "text_snippet": "哈哈哈哈这个操作太骚了"
        }}
    ]
}}

注意：
- 每个片段时长建议在 30 秒到 3 分钟之间
- 片段之间至少间隔 {self.min_segment_gap} 秒
- 优先选择有高弹幕互动的时间段
- 只返回 JSON，不要有其他文字"""
        
        print("Analyzing content with DeepSeek...")
        
        try:
            # 调用 DeepSeek API
            response = requests.post(
                "https://api.deepseek.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.deepseek_api_key}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": self.deepseek_model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 2000,
                    "temperature": 0.7
                },
                timeout=60
            )
            
            if response.status_code != 200:
                print(f"DeepSeek API error: {response.status_code} - {response.text}")
                return []
            
            result = response.json()
            result_text = result.get('choices', [{}])[0].get('message', {}).get('content', '')
            print(f"DeepSeek response: {result_text[:500]}...")
            
            # 解析 JSON
            # 尝试提取 JSON 部分
            json_start = result_text.find('{')
            json_end = result_text.rfind('}') + 1
            if json_start >= 0 and json_end > json_start:
                json_str = result_text[json_start:json_end]
                parsed = json.loads(json_str)
                
                highlights = []
                for h in parsed.get('highlights', []):
                    highlights.append(HighlightSegment(
                        start_time=float(h.get('start_time', 0)),
                        end_time=float(h.get('end_time', 0)),
                        score=float(h.get('score', 0)),
                        reason=h.get('reason', ''),
                        text_snippet=h.get('text_snippet', '')
                    ))
                
                print(f"Found {len(highlights)} highlight segments")
                return highlights
            else:
                print("No valid JSON found in response")
                return []
                
        except json.JSONDecodeError as e:
            print(f"JSON decode error: {e}")
            return []
        except requests.exceptions.Timeout:
            print("DeepSeek API timeout")
            return []
        except Exception as e:
            print(f"AI analysis error: {e}")
            traceback.print_exc()
            return []
    
    def merge_with_danmaku(
        self, 
        ai_highlights: List[HighlightSegment], 
        danmaku_data: List[dict],
        video_duration: float,
        log_prefix: str = "[Highlight]"
    ) -> List[HighlightSegment]:
        """
        合并 AI 分析结果与弹幕数据
        
        Args:
            ai_highlights: AI 分析得到的高光片段
            danmaku_data: 弹幕能量数据
            video_duration: 视频总时长
            log_prefix: 日志前缀
            
        Returns:
            合并后的高光片段列表（至少返回一段）
        """
        highlights = []
        
        if ai_highlights:
            # 合并 AI 结果与弹幕数据
            # 对 AI 高光片段，根据弹幕能量调整分数
            for h in ai_highlights:
                if danmaku_data:
                    # 找到该时间范围内的弹幕能量
                    segment_danmaku = [
                        d for d in danmaku_data
                        if h.start_time <= d.get('time', 0) <= h.end_time
                    ]
                    if segment_danmaku:
                        avg_energy = sum(d.get('energy', 0) for d in segment_danmaku) / len(segment_danmaku)
                        # AI 分数权重 0.7，弹幕分数权重 0.3
                        danmaku_score = min(1.0, avg_energy / 100)
                        h.score = h.score * 0.7 + danmaku_score * 0.3
            
            # 按分数排序
            ai_highlights.sort(key=lambda x: x.score, reverse=True)
            highlights = ai_highlights[:self.max_highlights]
        
        # 如果 AI 没有返回结果，或者需要补充高光片段
        if not highlights and danmaku_data:
            print(f"{log_prefix} [INFO] No AI highlights, using danmaku data only")
            # 根据弹幕能量排序，找出能量最高的片段
            sorted_danmaku = sorted(danmaku_data, key=lambda x: x.get('energy', 0), reverse=True)
            
            for d in sorted_danmaku[:self.max_highlights]:
                time_point = d.get('time', 0)
                highlights.append(HighlightSegment(
                    start_time=max(0, time_point - self.clip_before),
                    end_time=min(video_duration, time_point + self.clip_after),
                    score=min(1.0, d.get('energy', 0) / 100),
                    reason="高弹幕互动片段",
                    text_snippet=""
                ))
        
        # ========== 确保至少有一段高光 ==========
        # 如果以上都没有找到高光，则选择弹幕最多的10分钟作为默认高光
        if not highlights:
            print(f"{log_prefix} [WARN] No highlights found from AI or danmaku, finding highest danmaku density segment")
            default_highlight = self._find_highest_danmaku_density(danmaku_data, video_duration, log_prefix)
            if default_highlight:
                highlights.append(default_highlight)
            else:
                # 如果没有弹幕数据，则选择视频中段
                print(f"{log_prefix} [WARN] No danmaku data available, using middle of video as fallback")
                mid_point = video_duration / 2
                default_start = max(0, mid_point - 300)  # 5分钟
                default_end = min(video_duration, mid_point + 300)
                highlights.append(HighlightSegment(
                    start_time=default_start,
                    end_time=default_end,
                    score=0.5,
                    reason="默认高光片段（视频中段）",
                    text_snippet=""
                ))
        # ======================================
        
        # 合并接近的片段（考虑流畅性，把中间内容也包含进去）
        merged_highlights = self._merge_nearby_segments(highlights, log_prefix)
        
        # ========== 限制总时长不超过30分钟 ==========
        MAX_TOTAL_DURATION = 30 * 60  # 30分钟 = 1800秒
        total_duration = sum(h.end_time - h.start_time for h in merged_highlights)
        
        if total_duration > MAX_TOTAL_DURATION:
            print(f"{log_prefix} [INFO] Total highlight duration ({total_duration:.1f}s) exceeds 30 minutes, trimming...")
            # 按分数排序，优先保留高分片段
            sorted_highlights = sorted(merged_highlights, key=lambda x: x.score, reverse=True)
            
            trimmed_highlights = []
            current_duration = 0
            
            for seg in sorted_highlights:
                seg_duration = seg.end_time - seg.start_time
                
                if current_duration + seg_duration <= MAX_TOTAL_DURATION:
                    # 可以完整保留这个片段
                    trimmed_highlights.append(seg)
                    current_duration += seg_duration
                else:
                    # 需要截断这个片段以填满剩余时间
                    remaining_time = MAX_TOTAL_DURATION - current_duration
                    if remaining_time > 60:  # 至少保留1分钟
                        # 优先保留片段的前面部分（或可以改为中间部分）
                        trimmed_highlights.append(HighlightSegment(
                            start_time=seg.start_time,
                            end_time=seg.start_time + remaining_time,
                            score=seg.score,
                            reason=seg.reason,
                            text_snippet=seg.text_snippet
                        ))
                        current_duration = MAX_TOTAL_DURATION
                    break
            
            # 按时间排序返回
            merged_highlights = sorted(trimmed_highlights, key=lambda x: x.start_time)
            print(f"{log_prefix} [INFO] Trimmed to {len(merged_highlights)} segments, total duration: {current_duration:.1f}s")
        # ==========================================
        
        return merged_highlights
    
    def _find_highest_danmaku_density(
        self, 
        danmaku_data: List[dict], 
        video_duration: float,
        log_prefix: str = "[Highlight]",
        window_size: int = 600  # 10分钟 = 600秒
    ) -> Optional[HighlightSegment]:
        """
        找出弹幕密度最高的10分钟片段
        
        Args:
            danmaku_data: 弹幕能量数据
            video_duration: 视频总时长
            log_prefix: 日志前缀
            window_size: 滑动窗口大小（秒），默认10分钟
            
        Returns:
            高光片段，如果没有弹幕数据则返回 None
        """
        if not danmaku_data:
            return None
        
        # 按时间排序弹幕数据
        sorted_danmaku = sorted(danmaku_data, key=lambda x: x.get('time', 0))
        
        if not sorted_danmaku:
            return None
        
        # 使用滑动窗口计算弹幕密度
        best_start = 0
        best_score = 0
        
        # 每隔30秒采样一次
        step = 30
        for start_time in range(0, int(video_duration) - window_size, step):
            end_time = start_time + window_size
            
            # 计算窗口内的弹幕总能量
            window_energy = sum(
                d.get('energy', 0) for d in sorted_danmaku
                if start_time <= d.get('time', 0) <= end_time
            )
            
            if window_energy > best_score:
                best_score = window_energy
                best_start = start_time
        
        # 确保不超过视频时长
        best_end = min(best_start + window_size, video_duration)
        
        print(f"{log_prefix} [INFO] Found highest danmaku density segment: {best_start:.1f}s - {best_end:.1f}s (score: {best_score:.2f})")
        
        return HighlightSegment(
            start_time=best_start,
            end_time=best_end,
            score=min(1.0, best_score / 100),
            reason="高弹幕密度片段（10分钟）",
            text_snippet=""
        )
    
    def _merge_nearby_segments(
        self, 
        segments: List[HighlightSegment],
        log_prefix: str = "[Highlight]",
        max_gap: int = 180  # 最大间隔3分钟 = 180秒
    ) -> List[HighlightSegment]:
        """
        合并接近的高光片段，考虑视频流畅性
        
        如果两个高光片段之间的间隔小于 max_gap，则将中间段也包含进去，
        这样可以保证视频的流畅性，避免跳跃感
        
        Args:
            segments: 高光片段列表
            log_prefix: 日志前缀
            max_gap: 最大间隔（秒），小于此间隔的片段会被合并
            
        Returns:
            合并后的高光片段列表
        """
        if not segments:
            return []
        
        if len(segments) == 1:
            return segments
        
        # 按开始时间排序
        segments.sort(key=lambda x: x.start_time)
        
        merged = [segments[0]]
        
        for current in segments[1:]:
            last = merged[-1]
            gap = current.start_time - last.end_time
            
            # 如果当前片段与前一个片段重叠或间隔小于阈值，合并
            if gap <= max_gap:
                # 计算合并后的分数（取平均分或最高分）
                combined_score = max(last.score, current.score)
                
                # 创建合并后的片段，包含中间的内容
                merged[-1] = HighlightSegment(
                    start_time=last.start_time,
                    end_time=max(last.end_time, current.end_time),
                    score=combined_score,
                    reason=f"{last.reason}; {current.reason}",
                    text_snippet=f"{last.text_snippet} {current.text_snippet}".strip()
                )
                print(f"{log_prefix} [INFO] Merged nearby segments with gap {gap:.1f}s: {last.start_time:.1f}s - {current.end_time:.1f}s")
            else:
                merged.append(current)
        
        return merged
    
    def _merge_overlapping_segments(self, segments: List[HighlightSegment]) -> List[HighlightSegment]:
        """合并重叠或相邻的高光片段"""
        if not segments:
            return []
        
        # 按开始时间排序
        segments.sort(key=lambda x: x.start_time)
        
        merged = [segments[0]]
        for current in segments[1:]:
            last = merged[-1]
            # 如果当前片段与前一个片段重叠或间隔太小，合并
            if current.start_time <= last.end_time + self.min_segment_gap:
                # 扩展前一个片段
                merged[-1] = HighlightSegment(
                    start_time=last.start_time,
                    end_time=max(last.end_time, current.end_time),
                    score=max(last.score, current.score),
                    reason=f"{last.reason}; {current.reason}",
                    text_snippet=f"{last.text_snippet} {current.text_snippet}".strip()
                )
            else:
                merged.append(current)
        
        return merged
    
    async def cut_highlight_segments(
        self,
        video_path: str,
        segments: List[HighlightSegment],
        output_dir: str,
        log_path: str = None
    ) -> List[str]:
        """
        截取高光片段视频
        
        Args:
            video_path: 源视频路径
            segments: 高光片段列表
            output_dir: 输出目录
            log_path: 日志文件路径
            
        Returns:
            截取的视频片段路径列表
        """
        if not segments:
            print("No segments to cut")
            return []
        
        os.makedirs(output_dir, exist_ok=True)
        segment_paths = []
        
        for i, seg in enumerate(segments):
            output_path = os.path.join(output_dir, f"highlight_{i:02d}.mp4")
            
            # 使用 ffmpeg 截取片段
            command = (
                f'ffmpeg -y -ss {seg.start_time} -i "{video_path}" '
                f'-t {seg.end_time - seg.start_time} '
                f'-c:v libx264 -preset fast -c:a aac '
                f'"{output_path}"'
            )
            
            stdout, stderr, returncode = await async_run_command(command, log_path)
            
            if returncode == 0 and os.path.exists(output_path):
                segment_paths.append(output_path)
                print(f"Cut segment {i}: {seg.start_time:.1f}s - {seg.end_time:.1f}s")
            else:
                print(f"Failed to cut segment {i}: {stderr.decode('utf-8', errors='ignore')}")
        
        return segment_paths
    
    async def create_blinds_transition(
        self,
        segment_paths: List[str],
        output_path: str,
        transition_duration: float = 0.5,
        log_path: str = None
    ) -> bool:
        """
        使用百叶窗转场效果合并视频片段
        
        Args:
            segment_paths: 视频片段路径列表
            output_path: 输出视频路径
            transition_duration: 转场时长（秒）
            log_path: 日志文件路径
            
        Returns:
            是否成功
        """
        if len(segment_paths) == 0:
            print("No segments to merge")
            return False
        
        if len(segment_paths) == 1:
            # 只有一个片段，直接复制
            import shutil
            shutil.copy(segment_paths[0], output_path)
            return True
        
        print(f"Creating highlight video with blinds transition: {len(segment_paths)} segments")
        
        # 创建 concat 文件列表
        # 使用 ffmpeg xfade 滤镜实现百叶窗转场
        # 先获取每个视频的时长
        
        # 方法：使用复杂的 ffmpeg 滤镜链
        # 百叶窗转场效果：wipeleft, wiperight, wipeup, wipedown, slidedown, slideup 等
        # 这里使用自定义的百叶窗效果
        
        # 由于百叶窗转场比较复杂，我们使用一个更简单的方案：
        # 先将所有片段拼接，再添加交叉淡化转场
        
        # 创建临时 concat 文件
        concat_file = output_path + ".concat.txt"
        with open(concat_file, 'w') as f:
            for path in segment_paths:
                f.write(f"file '{path}'\n")
        
        # 使用 crossfade 转场（更可靠）
        # 对于多个片段，需要复杂的滤镜链
        # 简化方案：使用 concat demuxer 直接拼接，然后在片段之间添加简单的交叉淡化
        
        if len(segment_paths) == 2:
            # 两个片段：简单的交叉淡化
            command = (
                f'ffmpeg -y -i "{segment_paths[0]}" -i "{segment_paths[1]}" '
                f'-filter_complex '
                f'"[0:v][1:v]xfade=transition=slideleft:duration={transition_duration}:offset=5[outv]; '
                f'[0:a][1:a]acrossfade=d={transition_duration}[outa]" '
                f'-map "[outv]" -map "[outa]" '
                f'-c:v libx264 -preset medium -c:a aac "{output_path}"'
            )
        else:
            # 多个片段：使用 concat 并添加交叉淡化滤镜
            # 构建复杂的滤镜链
            inputs = " ".join([f'-i "{p}"' for p in segment_paths])
            
            # 构建 filter_complex
            # 先用 concat 拼接所有视频
            filter_parts = []
            n = len(segment_paths)
            
            # 简化：直接使用 concat 拼接（不添加复杂转场）
            # 如果需要更复杂的转场，可以后续优化
            stream_map = "".join([f"[{i}:v][{i}:a]" for i in range(n)])
            filter_complex = f'"{stream_map}concat=n={n}:v=1:a=1[outv][outa]"'
            
            command = f'ffmpeg -y {inputs} -filter_complex {filter_complex} -map "[outv]" -map "[outa]" -c:v libx264 -preset medium -c:a aac "{output_path}"'
        
        stdout, stderr, returncode = await async_run_command(command, log_path)

        if returncode != 0:
            print(f"Failed to create highlight video: {stderr.decode('utf-8', errors='ignore')}")
            # 尝试简单的 concat 方式
            print("Trying simple concat method...")
            command = f'ffmpeg -y -f concat -safe 0 -i "{concat_file}" -c copy "{output_path}"'
            stdout, stderr, returncode = await async_run_command(command, log_path)

            if returncode != 0:
                print(f"Simple concat also failed: {stderr.decode('utf-8', errors='ignore')}")
                # 清理临时文件
                if os.path.exists(concat_file):
                    os.remove(concat_file)
                return False

        # 清理临时文件
        if os.path.exists(concat_file):
            os.remove(concat_file)

        success = os.path.exists(output_path)
        if success:
            print(f"Highlight video created: {output_path}")
        return success
    
    def generate_highlight_summary(
        self,
        transcription: List[dict],
        highlights: List[HighlightSegment]
    ) -> str:
        """
        生成高光视频的文字总结
        
        Args:
            transcription: 转录结果列表
            highlights: 高光片段列表
            
        Returns:
            高光内容文字总结
        """
        if not highlights:
            return "高光时刻"
        
        # 收集高光片段的文字内容
        highlight_texts = []
        for seg in highlights:
            # 找到高光时间段内的文字
            segment_texts = []
            for t in transcription:
                if seg.start_time <= t['start'] <= seg.end_time:
                    segment_texts.append(t['text'])
            
            if segment_texts:
                highlight_texts.append({
                    'time': f"{int(seg.start_time // 60)}:{int(seg.start_time % 60):02d}-{int(seg.end_time // 60)}:{int(seg.end_time % 60):02d}",
                    'reason': seg.reason,
                    'text': ' '.join(segment_texts[:3])  # 只取前3句
                })
        
        if not highlight_texts:
            return "高光时刻"
        
        # 如果有 DeepSeek API，使用 AI 生成总结
        if self.deepseek_api_key:
            try:
                summary_parts = []
                for h in highlight_texts:
                    summary_parts.append(f"- {h['time']}：{h['reason']}")
                
                prompt = f"""请根据以下高光片段信息，生成一段简洁有趣的高光视频内容简介（不超过200字）：

{chr(10).join(summary_parts)}

要求：
1. 语言生动有趣
2. 突出每个高光点的精彩之处
3. 保持简洁，适合作为视频评论"""
                
                response = requests.post(
                    "https://api.deepseek.com/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.deepseek_api_key}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "model": self.deepseek_model,
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 300,
                        "temperature": 0.8
                    },
                    timeout=30
                )
                
                if response.status_code == 200:
                    result = response.json()
                    summary = result.get('choices', [{}])[0].get('message', {}).get('content', '').strip()
                    print(f"Generated highlight summary: {summary}")
                    return summary
            except Exception as e:
                print(f"Failed to generate AI summary: {e}")
        
        # 没有 AI 时，生成简单的总结
        summary_lines = ["🎬 高光时刻"]
        for h in highlight_texts:
            summary_lines.append(f"⏱️ {h['time']}：{h['reason']}")
        
        return '\n'.join(summary_lines)
    
    async def generate_highlight(
        self,
        video_path: str,
        danmaku_data: List[dict] = None,
        output_path: str = None,
        log_path: str = None
    ) -> Optional[HighlightResult]:
        """
        生成高光视频的完整流程
        
        Args:
            video_path: 源视频路径 (.bar.mp4)
            danmaku_data: 弹幕能量数据
            output_path: 输出视频路径（默认为 video_path 替换为 .bar.highlight.mp4）
            log_path: 日志文件路径
            
        Returns:
            HighlightResult 包含视频路径、总结和高光片段，失败返回 None
        """
        PREFIX = "[Highlight]"
        
        if not self.enabled:
            print(f"{PREFIX} [WARN] Highlight generation is disabled")
            return None
        
        if not os.path.exists(video_path):
            print(f"{PREFIX} [WARN] Video not found: {video_path}")
            return None
        
        # 设置默认输出路径
        if output_path is None:
            output_path = video_path.replace(".bar.mp4", ".bar.highlight.mp4")
        
        # 设置默认日志路径
        if log_path is None:
            log_path = video_path.replace(".bar.mp4", ".highlight.log")
        
        print(f"{PREFIX} Starting highlight generation for: {video_path}")
        
        # 检查弹幕数据是否存在
        has_danmaku = danmaku_data and len(danmaku_data) > 0
        if not has_danmaku:
            print(f"{PREFIX} [WARN] No danmaku data available, cannot generate highlights")
            return None
        
        try:
            # 1. 提取音频（失败不影响后续流程，使用弹幕数据兜底）
            audio_path = video_path.replace(".bar.mp4", ".audio.mp3")
            audio_extraction_success = False
            if await self.extract_audio(video_path, audio_path, log_path):
                audio_extraction_success = True
            else:
                print(f"{PREFIX} [WARN] Failed to extract audio, will rely on danmaku data only")
            
            # 2. 语音转文字（失败不影响后续流程，使用弹幕数据兜底）
            transcription = []
            if audio_extraction_success:
                try:
                    transcription = await asyncio.to_thread(self.speech_to_text, audio_path)
                    if not transcription:
                        print(f"{PREFIX} [WARN] Speech-to-text returned empty, relying on danmaku data")
                except Exception as e:
                    print(f"{PREFIX} [WARN] Speech-to-text failed: {e}, relying on danmaku data")
                    transcription = []
            
            # 3. AI 分析（失败不影响后续流程，使用弹幕数据兜底）
            ai_highlights = []
            if transcription:
                try:
                    ai_highlights = self.analyze_with_deepseek(transcription, danmaku_data)
                    if not ai_highlights:
                        print(f"{PREFIX} [WARN] No AI highlights detected, relying on danmaku data")
                except Exception as e:
                    print(f"{PREFIX} [WARN] AI analysis failed: {e}, relying on danmaku data")
            else:
                print(f"{PREFIX} [WARN] No transcription available, relying on danmaku data")
            
            # 4. 获取视频时长
            stdout, stderr, returncode = await async_run_command(
                f'ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "{video_path}"'
            )
            video_duration = float(stdout.decode('utf-8').strip()) if returncode == 0 else 3600
            
            # 5. 合并 AI 结果与弹幕数据
            highlights = self.merge_with_danmaku(ai_highlights, danmaku_data, video_duration, PREFIX)
            
            if not highlights:
                print(f"{PREFIX} [WARN] No highlights found after merging data")
                return None
            
            print(f"{PREFIX} Found {len(highlights)} highlights:")
            for h in highlights:
                print(f"{PREFIX}   - {h.start_time:.1f}s - {h.end_time:.1f}s: {h.reason}")
            
            # 6. 截取高光片段
            output_dir = os.path.dirname(video_path)
            segment_paths = await self.cut_highlight_segments(video_path, highlights, output_dir, log_path)
            
            if not segment_paths:
                print(f"{PREFIX} [WARN] No segments cut successfully")
                return None
            
            # 7. 合并视频片段（带转场效果）
            success = await self.create_blinds_transition(segment_paths, output_path, log_path=log_path)
            
            # 8. 生成高光文字总结
            summary = self.generate_highlight_summary(transcription, highlights)
            
            # 9. 清理临时文件（只清理高光片段，保留音频和转写结果）
            for path in segment_paths:
                if os.path.exists(path):
                    os.remove(path)
            # 音频和转写结果保留在同目录供后续分析使用
            
            if success:
                print(f"{PREFIX} Highlight video generated: {output_path}")
                print(f"{PREFIX} Highlight summary: {summary}")
                return HighlightResult(
                    video_path=output_path,
                    summary=summary,
                    highlights=highlights
                )
            else:
                print(f"{PREFIX} [WARN] Failed to generate highlight video")
                return None
                
        except Exception as e:
            print(f"{PREFIX} [ERROR] Highlight generation error: {e}")
            traceback.print_exc()
            return None


# 便捷函数
async def generate_highlight_video(
    video_path: str,
    danmaku_data: List[dict] = None,
    config: dict = None,
    output_path: str = None,
    log_path: str = None
) -> Optional[str]:
    """
    生成高光视频的便捷函数
    
    Args:
        video_path: 源视频路径
        danmaku_data: 弹幕能量数据
        config: 配置字典
        output_path: 输出路径
        log_path: 日志路径
        
    Returns:
        高光视频路径或 None
    """
    generator = HighlightGenerator(config)
    return await generator.generate_highlight(video_path, danmaku_data, output_path, log_path)
