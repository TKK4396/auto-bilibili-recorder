import asyncio
import os.path
import sys
import threading
import time
import traceback
from queue import Queue
from string import Template
from datetime import datetime

import dateutil.parser
import yaml
from bilibili_api import sync

from comment_task import CommentTask
from recorder_config import RecorderConfig, UploaderAccount
from recorder_manager import RecorderManager
from session import Session, Video
from subtitle_task import SubtitleTask
from task_save import TaskSave

# 定义会话继续、等待和上传前等待的时间常量（分钟）
CONTINUE_SESSION_MINUTES = 5
WAIT_SESSION_MINUTES = 6
WAIT_BEFORE_SESSION_MINUTES = 1
MAX_RETRY_ATTEMPTS = 5
RETRY_INTERVAL_SECONDS = 600  # 10分钟


class PersistentUploadTask:
    """持久化上传任务类"""
    def __init__(self, upload_task, trial=0, last_retry_time=None):
        self.session_id = upload_task.session_id
        self.video_path = upload_task.video_path
        self.thumbnail_path = upload_task.thumbnail_path
        self.sc_path = upload_task.sc_path
        self.he_path = upload_task.he_path
        self.subtitle_path = upload_task.subtitle_path
        self.title = upload_task.title
        self.source = upload_task.source
        self.description = upload_task.description
        self.tag = upload_task.tag
        self.channel_id = upload_task.channel_id
        self.danmaku = upload_task.danmaku
        self.account = upload_task.account
        self.trial = trial
        self.last_retry_time = last_retry_time or datetime.now().isoformat()
        
    def to_dict(self):
        return {
            'session_id': self.session_id,
            'video_path': self.video_path,
            'thumbnail_path': self.thumbnail_path,
            'sc_path': self.sc_path,
            'he_path': self.he_path,
            'subtitle_path': self.subtitle_path,
            'title': self.title,
            'source': self.source,
            'description': self.description,
            'tag': self.tag,
            'channel_id': self.channel_id,
            'danmaku': self.danmaku,
            'account': self.account.__dict__ if self.account else None,
            'trial': self.trial,
            'last_retry_time': self.last_retry_time
        }
    
    @classmethod
    def from_dict(cls, data):
        task = cls.__new__(cls)
        task.session_id = data['session_id']
        task.video_path = data['video_path']
        task.thumbnail_path = data['thumbnail_path']
        task.sc_path = data['sc_path']
        task.he_path = data['he_path']
        task.subtitle_path = data['subtitle_path']
        task.title = data['title']
        task.source = data['source']
        task.description = data['description']
        task.tag = data['tag']
        task.channel_id = data['channel_id']
        task.danmaku = data['danmaku']
        # 注意：这里需要重构UploadTask对象，假设account是可重建的
        task.account = data['account']
        task.trial = data['trial']
        task.last_retry_time = data['last_retry_time']
        return task


class RecordUploadManager:
    """
    录制上传管理器类，负责管理直播录制会话和视频上传流程
    
    该类处理直播房间的录制会话管理、视频上传队列、评论发布队列和字幕发布队列，
    并维护会话状态和上传进度的持久化存储。
    
    Attributes:
        config_path (str): 配置文件路径
        save_path (str): 保存文件路径
        config (RecorderConfig): 录制配置对象
        save (TaskSave): 任务保存对象，用于持久化存储
        recorder_manager (RecorderManager): 录制管理器
        sessions (dict): 存储活动会话的字典
        video_upload_queue (Queue): 视频上传任务队列
        comment_post_queue (Queue): 评论发布任务队列
        subtitle_post_queue (Queue): 字幕发布任务队列
        save_lock (threading.Lock): 保存操作的线程锁
        video_upload_thread (Thread): 视频上传工作线程
        comment_post_thread (Thread): 评论发布工作线程
        subtitle_post_thread (Thread): 字幕发布工作线程
        video_processing_loop (asyncio.EventLoop): 视频处理异步事件循环
        video_uploading_loop (asyncio.EventLoop): 视频上传异步事件循环
        comment_posting_loop (asyncio.EventLoop): 评论发布异步事件循环
        subtitle_posting_loop (asyncio.EventLoop): 字发布异步事件循环
        video_uploading_thread (Thread): 视频处理工作线程
        persistent_failed_tasks (list): 持久化失败任务列表
        retry_check_thread (Thread): 重试检查工作线程
    """
    def __init__(self, config_path, save_path):
        """
        初始化录制上传管理器
        
        Args:
            config_path (str): 配置文件路径
            save_path (str): 保存文件路径
        """
        self.config_path = config_path
        self.save_path = save_path
        with open(config_path, 'r') as file:
            self.config = RecorderConfig(yaml.load(file, Loader=yaml.FullLoader))
        if os.path.isfile(save_path):
            with open(save_path, 'r') as file:
                self.save = TaskSave.from_dict(yaml.load(file, Loader=yaml.FullLoader))
        else:
            print("Creating save file")
            self.save = TaskSave()
            self.save_progress()
        
        # 添加持久化失败任务列表
        if not hasattr(self.save, 'persistent_failed_tasks'):
            self.save.persistent_failed_tasks = []
        
        self.recorder_manager = RecorderManager([room for room in self.config.rooms])
        self.sessions: {str: Session} = dict()
        self.video_upload_queue: Queue[UploadTask] = Queue()
        self.comment_post_queue: Queue[CommentTask] = Queue()
        self.subtitle_post_queue: Queue[SubtitleTask] = Queue()
        self.save_lock = threading.Lock()
        self.video_upload_thread = threading.Thread(target=self.video_uploader)
        self.comment_post_thread = threading.Thread(target=self.comment_poster)
        self.subtitle_post_thread = threading.Thread(target=self.subtitle_poster)
        self.retry_check_thread = threading.Thread(target=self.check_and_retry_failed_tasks)
        self.video_processing_loop = asyncio.new_event_loop()
        self.video_uploading_loop = asyncio.new_event_loop()
        self.comment_posting_loop = asyncio.new_event_loop()
        self.subtitle_posting_loop = asyncio.new_event_loop()
        self.video_upload_thread.start()
        self.comment_post_thread.start()
        self.subtitle_post_thread.start()
        self.retry_check_thread.start()
        self.video_uploading_thread = threading.Thread(target=lambda: self.video_processing_loop.run_forever())
        self.video_uploading_thread.start()

    def save_progress(self):
        """
        将当前保存状态持久化到文件
        """
        with open(self.save_path, 'w') as file:
            yaml.dump(self.save.to_dict(), file, Dumper=yaml.Dumper)

    def add_persistent_failed_task(self, upload_task):
        """添加持久化失败任务"""
        persistent_task = PersistentUploadTask(upload_task, trial=upload_task.trial)
        with self.save_lock:
            self.save.persistent_failed_tasks.append(persistent_task)
            self.save_progress()
    
    def remove_persistent_failed_task(self, session_id):
        """移除持久化失败任务"""
        with self.save_lock:
            self.save.persistent_failed_tasks = [
                task for task in self.save.persistent_failed_tasks 
                if task.session_id != session_id
            ]
            self.save_progress()

    def check_and_retry_failed_tasks(self):
        """定时检查并重试失败任务"""
        while True:
            time.sleep(RETRY_INTERVAL_SECONDS)  # 每10分钟检查一次
            try:
                with self.save_lock:
                    tasks_to_retry = []
                    for task in self.save.persistent_failed_tasks[:]:  # 创建副本以避免修改列表时的问题
                        if task.trial < MAX_RETRY_ATTEMPTS:
                            # 重新创建UploadTask对象（这里需要根据实际UploadTask类结构调整）
                            # 假设有一个方法可以重新构建UploadTask
                            from upload_task import UploadTask  # 假设UploadTask在upload_task模块中
                            upload_task = UploadTask(
                                session_id=task.session_id,
                                video_path=task.video_path,
                                thumbnail_path=task.thumbnail_path,
                                sc_path=task.sc_path,
                                he_path=task.he_path,
                                subtitle_path=task.subtitle_path,
                                title=task.title,
                                source=task.source,
                                description=task.description,
                                tag=task.tag,
                                channel_id=task.channel_id,
                                danmaku=task.danmaku,
                                account=task.account
                            )
                            upload_task.trial = task.trial
                            tasks_to_retry.append((task, upload_task))
                    
                    # 从持久化列表中移除即将重试的任务
                    for persistent_task, _ in tasks_to_retry:
                        self.save.persistent_failed_tasks.remove(persistent_task)
                    
                    # 添加到上传队列
                    for _, upload_task in tasks_to_retry:
                        self.video_upload_queue.put(upload_task)
                        
                if tasks_to_retry:
                    print(f"Retrying {len(tasks_to_retry)} failed tasks from persistent storage")
                
            except Exception as e:
                print(f"Error checking and retrying failed tasks: {e}")
                print(traceback.format_exc())

    def video_uploader(self):
        """
        视频上传工作线程函数
        从队列中获取上传任务并执行视频上传操作
        """
        asyncio.set_event_loop(self.video_uploading_loop)
        while True:
            upload_task = self.video_upload_queue.get()
            try:
                first_video_comment = upload_task.session_id not in self.save.session_id_map
                bv_id = sync(upload_task.upload(self.save.session_id_map))
                sys.stdout.flush()
                with self.save_lock:
                    self.save.session_id_map[upload_task.session_id] = bv_id
                    self.save_progress()
                
                # 移除持久化失败任务（如果存在）
                self.remove_persistent_failed_task(upload_task.session_id)
                
                if first_video_comment:
                    print("adding comment task to queue")
                    self.comment_post_queue.put(
                        CommentTask.from_upload_task(upload_task)
                    )
                print("adding subtitle task to queue")
                self.subtitle_post_queue.put(
                    SubtitleTask.from_upload_task(upload_task, bv_id)
                )
            except Exception:
                if upload_task.trial < MAX_RETRY_ATTEMPTS:
                    upload_task.trial += 1
                    self.video_upload_queue.put(upload_task)
                    print(f"Upload failed: {upload_task.title}, retrying ({upload_task.trial}/{MAX_RETRY_ATTEMPTS})")
                else:
                    print(f"Upload failed too many times: {upload_task.title}, adding to persistent storage")
                    # 添加到持久化失败任务列表
                    self.add_persistent_failed_task(upload_task)
                    # 如果上传失败且重试超过5次，不再重试
                print(traceback.format_exc())

    def comment_poster(self):
        """
        评论发布工作线程函数
        处理评论发布任务队列中的任务
        """
        asyncio.set_event_loop(self.comment_posting_loop)
        while True:
            with self.save_lock:
                while not self.comment_post_queue.empty():
                    self.save.active_comment_tasks += [self.comment_post_queue.get()]
                    self.save_progress()
            try:
                if len(self.save.active_comment_tasks) != 0:
                    task_to_remove = []
                    for idx, task in enumerate(self.save.active_comment_tasks):
                        task: CommentTask
                        if sync(task.post_comment(self.save.session_id_map)):
                            task_to_remove += [idx]
                    if task_to_remove != 0:
                        with self.save_lock:
                            self.save.active_comment_tasks = [
                                comment_task
                                for idx, comment_task in enumerate(self.save.active_comment_tasks)
                                if idx not in task_to_remove
                            ]
                            self.save_progress()
            except Exception as err:
                print(f"Unknown posting exception: {err}")
                print(traceback.format_exc())
            finally:
                time.sleep(60)

    def subtitle_poster(self):
        """
        字幕发布工作线程函数
        处理字幕发布任务队列中的任务
        """
        asyncio.set_event_loop(self.subtitle_posting_loop)
        while True:
            with self.save_lock:
                while not self.subtitle_post_queue.empty():
                    self.save.active_subtitle_tasks += [self.subtitle_post_queue.get()]
                    self.save_progress()
            try:
                if len(self.save.active_subtitle_tasks) != 0:
                    task_to_remove = []
                    for idx, task in enumerate(self.save.active_subtitle_tasks):
                        task: SubtitleTask
                        print("try posting subtitle")
                        if sync(task.post_subtitle()):
                            task_to_remove += [idx]
                    if task_to_remove != 0:
                        with self.save_lock:
                            new_subtitle_tasks = []
                            for idx, subtitle_task in enumerate(self.save.active_subtitle_tasks):
                                subtitle_task: SubtitleTask
                                append = True
                                if idx in task_to_remove:
                                    append = False
                                else:
                                    for j in task_to_remove:
                                        removing_task = self.save.active_subtitle_tasks[j]
                                        if subtitle_task.is_earlier_task_of(removing_task):
                                            append = False
                                            break
                                if append:
                                    new_subtitle_tasks += [subtitle_task]
                            self.save.active_subtitle_tasks = new_subtitle_tasks
                            self.save_progress()
            except Exception as err:
                print(f"Unknown posting exception: {err}")
                print(traceback.format_exc())
            finally:
                time.sleep(60)

    async def upload_video(self, session: Session):
        """
        异步上传视频的协程函数
        
        Args:
            session (Session): 要上传的会话对象
        """
        await asyncio.sleep(WAIT_BEFORE_SESSION_MINUTES * 60)
        if len(session.videos) == 0:
            print(f"No video in session: {session.room_id}@{session.session_id}")
            return
        room_config = session.room_config
        if room_config.uploader_obj is None:
            print(f"No need to upload for {room_config.id}")
            await session.gen_early_video()
            await asyncio.sleep(WAIT_SESSION_MINUTES * 60)
            await session.gen_danmaku_video()
            return
        uploader: UploaderAccount = room_config.uploader_obj
        substitute_dict = {
            "name": session.room_name,
            "title": session.room_title,
            "uploader_name": uploader.name,
            "y": session.start_time.year,
            "m": session.start_time.month,
            "d": session.start_time.day,
            "HH": f"{session.start_time.hour:02d}",
            "MM": f"{session.start_time.minute:02d}",
            "SS": f"{session.start_time.second:02d}",
            "yy": f"{session.start_time.year:04d}",
            "mm": f"{session.start_time.month:02d}",
            "dd": f"{session.start_time.day:02d}",
            # remove "/storage/" prefix
            "flv_path": session.videos[0].flv_file_path()[9:],  # remove "/storage/" prefix
        }
        title = Template(room_config.title).substitute(substitute_dict)
        temp_title = title
        i = 1
        other_video_titles = [
            name for session_id, name in self.save.video_name_history.items()
            if session_id != session.session_id
        ]
        while temp_title in other_video_titles:
            i += 1
            temp_title = f"{temp_title}{i}"
        title = temp_title
        with self.save_lock:
            self.save.video_name_history[session.session_id] = title
        description = Template(room_config.description).substitute(substitute_dict)
        await session.gen_early_video()
        early_upload_task = None
        if session.early_video_path is not None:
            early_upload_task = UploadTask(
                session_id=session.session_id,
                video_path=session.early_video_path,
                thumbnail_path=session.output_path()['thumbnail'],
                sc_path=session.output_path()['sc_file'],
                he_path=session.output_path()['he_file'],
                subtitle_path=session.output_path()['sc_srt'],
                title=title,
                source=room_config.source,
                description=description,
                tag=room_config.tags,
                channel_id=room_config.channel_id,
                danmaku=False,
                account=uploader
            )
            self.video_upload_queue.put(early_upload_task)
        await asyncio.sleep(WAIT_SESSION_MINUTES * 60)
        await session.gen_danmaku_video()
        danmaku_upload_task = UploadTask(
            session_id=session.session_id,
            video_path=session.output_path()['danmaku_video'],
            thumbnail_path=session.output_path()['thumbnail'],
            sc_path=session.output_path()['sc_file'],
            he_path=session.output_path()['he_file'],
            subtitle_path=session.output_path()['sc_srt'],
            title=title,
            source=room_config.source,
            description=description,
            tag=room_config.tags,
            channel_id=room_config.channel_id,
            danmaku=True,
            account=uploader
        )
        self.video_upload_queue.put(
            danmaku_upload_task
        )
        if early_upload_task is None:
            self.comment_post_queue.put(
                CommentTask.from_upload_task(danmaku_upload_task)
            )

    async def handle_update(self, update_json: dict):
        """
        处理更新事件的异步函数
        
        根据不同的事件类型处理会话开始、文件打开、文件关闭和会话结束等事件
        
        Args:
            update_json (dict): 包含更新信息的JSON字典
        """
        if update_json["EventType"] not in [
            "SessionStarted",
            "FileOpening",
            "FileClosed",
            "SessionEnded",
        ]:
            print(f"无关事件: {update_json}")
            return

        room_id = update_json["EventData"]["RoomId"]
        session_id = update_json["EventData"]["SessionId"]
        event_timestamp = dateutil.parser.isoparse(update_json["EventTimestamp"])
        room_config = None
        for room in self.config.rooms:
            if room.id == room_id:
                room_config = room
        if room_config is None:
            print(f"Cannot find room config for {room_id}!")
            return
        if update_json["EventType"] == "SessionStarted":
            for session in self.sessions.values():
                if session.room_id == room_id and \
                        (event_timestamp - session.end_time).total_seconds() / 60 < CONTINUE_SESSION_MINUTES:
                    self.sessions[session_id] = session
                    if session.upload_task is not None:
                        session.upload_task.cancel()
                    return
            self.sessions[session_id] = Session(update_json, room_config=room_config)
        else:
            if session_id not in self.sessions:
                print(f"Cannot find {room_id}/{session_id} for: {update_json}")
                return
            current_session: Session = self.sessions[session_id]
            current_session.process_update(update_json)
            if update_json["EventType"] == "FileClosed":
                new_video = Video(update_json)
                await current_session.add_video(new_video)
            elif update_json["EventType"] == "SessionEnded":
                current_session.upload_task = \
                    asyncio.run_coroutine_threadsafe(self.upload_video(current_session), self.video_processing_loop)
