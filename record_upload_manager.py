import asyncio
import os.path
import sys
import threading
import time
import traceback
import json
from queue import Queue
from string import Template

import dateutil.parser
import yaml
from bilibili_api import sync

from comment_task import CommentTask
from recorder_config import RecorderConfig, UploaderAccount
from recorder_manager import RecorderManager
from session import Session, Video, log_debug
from subtitle_task import SubtitleTask
from task_save import TaskSave
from upload_task import UploadTask
from db_manager import DBManager
import traceback

CONTINUE_SESSION_MINUTES = 5
WAIT_SESSION_MINUTES = 6
WAIT_BEFORE_SESSION_MINUTES = 1


class RecordUploadManager:
    def __init__(self, config_path, save_path):
        self.config_path = config_path
        self.save_path = save_path
        with open(config_path, 'r', encoding='utf-8') as file:
            self.config = RecorderConfig(yaml.load(file, Loader=yaml.FullLoader))
        if os.path.isfile(save_path):
            with open(save_path, 'r') as file:
                self.save = TaskSave.from_dict(yaml.load(file, Loader=yaml.FullLoader))
        else:
            print("Creating save file")
            self.save = TaskSave()
            self.save_progress()
        self.recorder_manager = RecorderManager([room for room in self.config.rooms])
        self.sessions: {str: Session} = dict()
        self.video_upload_queue: Queue[UploadTask] = Queue()
        self.comment_post_queue: Queue[CommentTask] = Queue()
        self.subtitle_post_queue: Queue[SubtitleTask] = Queue()
        self.save_lock = threading.Lock()
        self.video_upload_thread = threading.Thread(target=self.video_uploader)
        self.comment_post_thread = threading.Thread(target=self.comment_poster)
        self.subtitle_post_thread = threading.Thread(target=self.subtitle_poster)
        self.video_processing_loop = asyncio.new_event_loop()
        self.video_uploading_loop = asyncio.new_event_loop()
        self.comment_posting_loop = asyncio.new_event_loop()
        self.subtitle_posting_loop = asyncio.new_event_loop()
        self.video_upload_thread.start()
        self.comment_post_thread.start()
        self.subtitle_post_thread.start()
        self.video_uploading_thread = threading.Thread(target=lambda: self.video_processing_loop.run_forever())
        self.video_uploading_thread.start()
        # --- 新增数据库及轮询线程 ---
        # 请在这里修改为你的实际 MySQL 连接信息
        self.db_manager = DBManager(host='127.0.0.1', user='root', password='password100', database='bilibili_recorder')
        self.db_polling_thread = threading.Thread(target=self.db_poller)
        self.db_polling_thread.start()
        # ----------------------------


    def save_progress(self):
        with open(self.save_path, 'w') as file:
            yaml.dump(self.save.to_dict(), file, Dumper=yaml.Dumper)

    # def video_uploader(self):
    #     asyncio.set_event_loop(self.video_uploading_loop)
    #     while True:
    #         upload_task = self.video_upload_queue.get()
    #         try:
    #             first_video_comment = upload_task.session_id not in self.save.session_id_map
    #             bv_id = sync(upload_task.upload(self.save.session_id_map))
    #             sys.stdout.flush()
    #             with self.save_lock:
    #                 self.save.session_id_map[upload_task.session_id] = bv_id
    #                 self.save_progress()
    #             if first_video_comment:
    #                 print("adding comment task to queue")
    #                 self.comment_post_queue.put(
    #                     CommentTask.from_upload_task(upload_task)
    #                 )
    #             print("adding subtitle task to queue")
    #             self.subtitle_post_queue.put(
    #                 SubtitleTask.from_upload_task(upload_task, bv_id)
    #             )
    #         except Exception:
    #             if upload_task.trial < 5:
    #                 upload_task.trial += 1
    #                 self.video_upload_queue.put(upload_task)
    #                 print(f"Upload failed: {upload_task.title}, retrying")
    #             else:
    #                 print(f"Upload failed too many times: {upload_task.title}")
    #             print(traceback.format_exc())
    def video_uploader(self):
        asyncio.set_event_loop(self.video_uploading_loop)
        while True:
            upload_task = self.video_upload_queue.get()
            if upload_task.db_id:
                # 状态 1 代表 UPLOADING (上传中)
                self.db_manager.update_status(upload_task.db_id, 1) # 开始上传
            try:
                first_video_comment = upload_task.session_id not in self.save.session_id_map
                bv_id = sync(upload_task.upload(self.save.session_id_map))
                sys.stdout.flush()
                with self.save_lock:
                    self.save.session_id_map[upload_task.session_id] = bv_id
                    self.save_progress()

                if upload_task.db_id:
                    # 状态 2 代表 SUCCESS (成功)
                    self.db_manager.update_status(upload_task.db_id, 2) # 上传成功

                if first_video_comment:
                    print("adding comment task to queue")
                    self.comment_post_queue.put(
                        CommentTask.from_upload_task(upload_task)
                    )
                print("adding subtitle task to queue")
                self.subtitle_post_queue.put(
                    SubtitleTask.from_upload_task(upload_task, bv_id)
                )
            except Exception as e:
                error_msg = str(e)[:500]  # 截取部分报错信息
                error_type = UploadTask._classify_error(e)

                # 按错误类型输出差异化日志
                if error_type == "credential":
                    print(f"[重试终止-认证错误] {upload_task.title}: {error_msg}")
                    print(f"[建议] 账号 {upload_task.account.name} 登录态异常，请更新 Cookie 后从数据库手动重试")
                elif error_type == "upload":
                    print(f"[上传失败] {upload_task.title}: {error_msg}")
                else:
                    print(f"[上传失败-未知错误] {upload_task.title}: {error_msg}")

                if upload_task.trial < 5:
                    upload_task.trial += 1
                    if upload_task.db_id:
                        # 失败后还有机会，状态退回 0 (QUEUED)
                        self.db_manager.update_status(upload_task.db_id, 0, f"第{upload_task.trial}次重试... {error_msg}")
                    if error_type == "credential":
                        # 认证错误：减少重试次数（再试 2 次后放弃，给 cookie 刷新留机会但不无限等待）
                        if upload_task.trial < 3:
                            print(f"[退避] 等待 10s 后重试 (第{upload_task.trial}次)")
                            time.sleep(10)
                            self.video_upload_queue.put(upload_task)
                        else:
                            print(f"[放弃] 认证错误重试 {upload_task.trial} 次仍失败: {upload_task.title}")
                            if upload_task.db_id:
                                self.db_manager.update_status(upload_task.db_id, 3, f"认证失败: {error_msg}")
                    else:
                        print(f"[退避] 等待 10s 后重试 (第{upload_task.trial}次)")
                        time.sleep(10)
                        self.video_upload_queue.put(upload_task)
                else:
                    if upload_task.db_id:
                        # 彻底失败，状态设为 3 (FAILED)
                        self.db_manager.update_status(upload_task.db_id, 3, error_msg)
                    print(f"[放弃] 重试 {upload_task.trial} 次后仍失败: {upload_task.title}")
                print(traceback.format_exc())

    def comment_poster(self):
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
        log_debug(f"[upload_video][{session.room_id}@{session.session_id}] 进入处理流程 "
                  f"（{len(session.videos)} 个片段，标题={session.room_title}）")
        await asyncio.sleep(WAIT_BEFORE_SESSION_MINUTES * 60)
        if len(session.videos) == 0:
            print(f"No video in session: {session.room_id}@{session.session_id}")
            return
        room_config = session.room_config
        if room_config.uploader_obj is None:
            print(f"No need to upload for {room_config.id}")
            log_debug(f"[upload_video][{session.room_id}@{session.session_id}] 分支=仅本地（无 uploader），"
                      f"先生成早期视频，等待 {WAIT_SESSION_MINUTES} 分钟后压制")
            # 关键修复：gen_early_video 任一步失败（如 danmaku_energy_map 依赖缺失）
            # 都不能阻断后续的 gen_danmaku_video（弹幕版视频压制）
            try:
                await session.gen_early_video()
            except Exception as e:
                print(f"[警告] 生成早期视频失败，将继续压制流程: {e}")
                traceback.print_exc()
            log_debug(f"[upload_video][{session.room_id}@{session.session_id}] 进入等待期 "
                      f"({WAIT_SESSION_MINUTES} 分钟) 后开始压制")
            await asyncio.sleep(WAIT_SESSION_MINUTES * 60)
            try:
                await session.gen_danmaku_video()
            except Exception as e:
                print(f"[压制失败] {session.room_id}@{session.session_id}: {e}")
                traceback.print_exc()
            log_debug(f"[upload_video][{session.room_id}@{session.session_id}] 仅本地分支处理结束")
            return
        uploader: UploaderAccount = room_config.uploader_obj
        log_debug(f"[upload_video][{session.room_id}@{session.session_id}] 分支=上传 "
                  f"(账号={uploader.name}, 频道={room_config.channel_id})")
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
        # 早期视频（封面/高能时间/弹幕字幕/concat）生成失败时继续走压制与上传流程，
        # 异常不再静默阻断后续 gen_danmaku_video
        log_debug(f"[upload_video][{session.room_id}@{session.session_id}] 开始生成早期视频 "
                  f"(标题={title})")
        try:
            await session.gen_early_video()
        except Exception as e:
            print(f"[警告] 生成早期视频失败，将继续上传/压制流程: {e}")
            traceback.print_exc()

        # 构建插入数据库的数据模版
        base_db_task = {
            'session_id': session.session_id,
            'room_id': room_config.id,
            'thumbnail_path': session.output_path()['thumbnail'],
            'sc_path': session.output_path()['sc_file'],
            'he_path': session.output_path()['he_file'],
            'subtitle_path': session.output_path()['sc_srt'],
            'title': title,
            'source': room_config.source,
            'description': description,
            'tag': room_config.tags,
            'channel_id': room_config.channel_id,
            'account_name': uploader.name,
            'extra_info': None
        }

        early_upload_task = None
        if session.early_video_path is not None:
            db_task_early = base_db_task.copy()
            db_task_early.update({'video_path': session.early_video_path, 'danmaku': False})
            early_db_id = self.db_manager.insert_task(db_task_early) # 先入库，获取ID
            log_debug(f"[upload_video][{session.room_id}@{session.session_id}] 早期视频已入队上传 "
                      f"(db_id={early_db_id}, 路径={session.early_video_path})")

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
                account=uploader,
                db_id=early_db_id # 传入ID
            )
            self.video_upload_queue.put(early_upload_task)
        else:
            log_debug(f"[upload_video][{session.room_id}@{session.session_id}] 早期视频缺失，"
                      f"未入队上传（可能生成阶段失败）")

        log_debug(f"[upload_video][{session.room_id}@{session.session_id}] 早期视频上传任务已提交，"
                  f"进入等待期 ({WAIT_SESSION_MINUTES} 分钟) 后开始弹幕版压制")
        await asyncio.sleep(WAIT_SESSION_MINUTES * 60)
        try:
            await session.gen_danmaku_video()
        except Exception as e:
            print(f"[压制失败] {session.room_id}@{session.session_id}: {e}")
            traceback.print_exc()

        # 生成高光视频（使用全局配置）
        highlight_video_path = None
        highlight_summary = None
        highlight_config = None
        # 优先使用全局高光配置
        if self.config.highlight and self.config.highlight.enabled:
            print(f"Highlight generation enabled (global config)")
            highlight_config = self.config.highlight.to_dict()
            log_debug(f"[upload_video][{session.room_id}@{session.session_id}] 开始生成高光视频")
            try:
                highlight_result = await session.gen_highlight_video(highlight_config)
                if highlight_result:
                    highlight_video_path = highlight_result.video_path
                    highlight_summary = highlight_result.summary
                    print(f"Highlight video generated: {highlight_video_path}")
                    print(f"Highlight summary: {highlight_summary}")
                    log_debug(f"[upload_video][{session.room_id}@{session.session_id}] 高光视频已生成: "
                              f"{highlight_video_path}")
                else:
                    print("Failed to generate highlight video")
                    log_debug(f"[upload_video][{session.room_id}@{session.session_id}] 高光视频生成为空")
            except Exception as e:
                print(f"Error generating highlight video: {e}")
                traceback.print_exc()
                log_debug(f"[upload_video][{session.room_id}@{session.session_id}] 高光视频生成异常: {e}")

        db_task_danmaku = base_db_task.copy()
        db_task_danmaku.update({'video_path': session.output_path()['danmaku_video'], 'danmaku': True})

        part_videos = None
        part_titles = None
        # 如果有高光视频，准备分 P 数据并序列化到 extra_info
        if highlight_video_path and os.path.exists(highlight_video_path):
            part_videos = [session.output_path()['danmaku_video'], highlight_video_path]
            part_titles = ["完整录播", "高光时刻"]
            db_task_danmaku['extra_info'] = json.dumps({
                "part_videos": part_videos,
                "part_titles": part_titles
            }, ensure_ascii=False)

        danmaku_db_id = self.db_manager.insert_task(db_task_danmaku) # 先入库，获取ID
        log_debug(f"[upload_video][{session.room_id}@{session.session_id}] 弹幕版视频已入队上传 "
                  f"(db_id={danmaku_db_id}, 路径={session.output_path()['danmaku_video']}"
                  f"{', 含高光分P' if part_videos else ''})")

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
            account=uploader,
            db_id=danmaku_db_id # 传入ID
        )

        if part_videos:
            danmaku_upload_task.set_multi_part(
                part_videos=part_videos,
                part_titles=part_titles
            )
            print(f"Set multi-part upload with highlight video")

        self.video_upload_queue.put(
            danmaku_upload_task
        )
        if early_upload_task is None:
            # 创建评论任务，包含高光总结
            comment_task = CommentTask(
                sc_path=session.output_path()['sc_file'],
                he_path=session.output_path()['he_file'],
                session_id=session.session_id,
                verify=uploader.verify,
                highlight_summary=highlight_summary
            )
            self.comment_post_queue.put(comment_task)
            log_debug(f"[upload_video][{session.room_id}@{session.session_id}] 已提交评论任务"
                      f"（含高光总结: {'是' if highlight_summary else '否'}）")

        log_debug(f"[upload_video][{session.room_id}@{session.session_id}] 上传分支处理结束")

    async def handle_update(self, update_json: dict):
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
                log_debug(f"[handle_update] 收到 SessionEnded，已提交 upload_video 任务 "
                          f"({current_session.room_id}@{current_session.session_id})")

    def db_poller(self):
        while True:
            try:
                # 查找被用户手动修改为 PENDING_RETRY 的任务
                tasks = self.db_manager.get_tasks_by_status(4)
                for task in tasks:
                    account_name = task['account_name']
                    # 从配置中找到对应的上传账号
                    uploader = next((acc for name, acc in self.config.accounts.items() if acc.name == account_name), None)
                    if uploader:
                        upload_task = UploadTask(
                            session_id=task['session_id'],
                            video_path=task['video_path'],
                            thumbnail_path=task['thumbnail_path'],
                            sc_path=task['sc_path'],
                            he_path=task['he_path'],
                            subtitle_path=task['subtitle_path'],
                            title=task['title'],
                            source=task['source'],
                            description=task['description'],
                            tag=task['tag'],
                            channel_id=task['channel_id'],
                            danmaku=bool(task['danmaku']),
                            account=uploader,
                            db_id=task['id']
                        )

                        # 从 extra_info 恢复分 P 状态
                        if task.get('extra_info'):
                            try:
                                extra_info = json.loads(task['extra_info'])
                                if 'part_videos' in extra_info:
                                    upload_task.set_multi_part(
                                        part_videos=extra_info['part_videos'],
                                        part_titles=extra_info.get('part_titles')
                                    )
                            except Exception as e:
                                print(f"解析 extra_info 失败: {e}")

                        # 重置状态并放入上传队列
                        self.db_manager.update_status(task['id'], 0,'')
                        self.video_upload_queue.put(upload_task)
                        print(f"从数据库恢复并重试任务: {task['title']}")
                    else:
                        print(f"未找到账号 {account_name}，无法重试任务: {task['title']}")
                        self.db_manager.update_status(task['id'], 3, f"未找到账号 {account_name}")
            except Exception as e:
                print(f"数据库轮询异常: {e}")
            time.sleep(30)  # 每 30 秒轮询一次