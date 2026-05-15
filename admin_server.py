from quart import Quart, jsonify, request, render_template, Response
import json
import yaml
import os
import asyncio
from db_manager import DBManager
from speech_to_text import (
    find_all_bar_mp4_files, run_transcription, get_task,
    get_task_by_path, get_all_tasks, delete_task,
    load_transcription_config, get_tran_content, _make_task_id,
    _get_output_txt_path
)

app = Quart(__name__)

def _resolve_config_path():
    env_path = os.environ.get('RECORDER_CONFIG_PATH')
    if env_path:
        return env_path

    cwd_path = os.path.join(os.getcwd(), 'recorder_config.yaml')
    if os.path.isfile(cwd_path):
        return cwd_path

    return os.path.join(os.path.dirname(__file__), 'recorder_config.yaml')

CONFIG_PATH = _resolve_config_path()

db_manager = DBManager(
    host='127.0.0.1',
    user='root',
    password='password100',
    database='bilibili_recorder'
)

STATUS_MAP = {
    0: '队列中',
    1: '上传中',
    2: '成功',
    3: '失败',
    4: '等待重试'
}


def _normalize_video_path(video_path: str) -> str:
    """Quart path 转换器会去掉开头 /，这里还原"""
    if not video_path or video_path.startswith('/'):
        return video_path
    return '/' + video_path

@app.route('/')
async def index():
    return await render_template('index.html', status_map=STATUS_MAP)

@app.route('/api/tasks')
async def get_tasks():
    tasks = db_manager.get_non_success_tasks(limit=100)
    for task in tasks:
        task['status_name'] = STATUS_MAP.get(task['status'], '未知')
        if task.get('extra_info'):
            try:
                task['extra_info'] = json.loads(task['extra_info'])
            except:
                pass
    return jsonify(tasks)

@app.route('/api/tasks/<int:task_id>', methods=['GET'])
async def get_task(task_id):
    task = db_manager.get_task_by_id(task_id)
    if task:
        task['status_name'] = STATUS_MAP.get(task['status'], '未知')
    return jsonify(task or {})

@app.route('/api/tasks/<int:task_id>/status', methods=['PUT'])
async def update_task_status(task_id):
    data = await request.json
    new_status = data.get('status')
    error_msg = data.get('error_msg', '')

    if new_status not in STATUS_MAP:
        return jsonify({'error': '无效的状态值'}), 400

    db_manager.update_status(task_id, new_status, error_msg)
    return jsonify({'success': True, 'task_id': task_id, 'new_status': new_status})

@app.route('/api/config')
async def get_config():
    """读取当前配置文件内容"""
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            config_content = f.read()
        return jsonify({
            'success': True,
            'content': config_content,
            'path': CONFIG_PATH
        })
    except FileNotFoundError:
        return jsonify({'success': False, 'error': '配置文件不存在'}), 404
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/config', methods=['PUT'])
async def save_config():
    """保存配置文件"""
    data = await request.json
    content = data.get('content')

    if not content:
        return jsonify({'success': False, 'error': '配置内容不能为空'}), 400

    try:
        yaml.safe_load(content)

        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            f.write(content)

        return jsonify({
            'success': True,
            'message': '配置已保存，请手动重启服务以加载新配置',
            'path': CONFIG_PATH
        })
    except yaml.YAMLError as e:
        return jsonify({'success': False, 'error': f'YAML 格式错误: {str(e)}'}), 400
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ========== 语音转文字 API ==========

@app.route('/api/transcription/files')
async def get_transcription_files():
    """扫描 .all.bar.mp4 文件列表"""
    cfg = load_transcription_config()
    scan_dir = cfg.get('scan_directory', '/storage')

    try:
        files = await asyncio.to_thread(find_all_bar_mp4_files, scan_dir)
    except Exception as e:
        return jsonify({'success': False, 'error': f'扫描目录失败: {str(e)}'}), 500

    result = []
    for f in files:
        task = get_task_by_path(f)
        tran_txt = _get_output_txt_path(f)
        try:
            size_mb = round(os.path.getsize(f) / (1024 * 1024), 2)
            mtime = os.path.getmtime(f)
        except OSError:
            size_mb = 0
            mtime = 0
        result.append({
            'path': f,
            'name': os.path.basename(f),
            'size_mb': size_mb,
            'has_tran': os.path.exists(tran_txt),
            'mtime': mtime,
            'task': task.to_dict() if task else None,
        })

    return jsonify({'success': True, 'files': result, 'scan_directory': scan_dir})


@app.route('/api/transcription/start', methods=['POST'])
async def start_transcription():
    """启动语音转文字任务"""
    data = await request.json
    video_path = data.get('video_path', '')

    if not video_path:
        return jsonify({'success': False, 'error': '缺少 video_path 参数'}), 400
    if not os.path.exists(video_path):
        return jsonify({'success': False, 'error': '视频文件不存在'}), 404

    existing = get_task_by_path(video_path)
    task_id = _make_task_id(video_path)
    if existing and existing.status in ('pending', 'extracting', 'splitting', 'transcribing'):
        return jsonify({
            'success': True,
            'task_id': task_id,
            'task': existing.to_dict(),
            'message': '任务已在进行中'
        })

    cfg = load_transcription_config()
    if not cfg.get('siliconflow_api_key') or cfg['siliconflow_api_key'].startswith('your_'):
        return jsonify({'success': False, 'error': '请先配置 siliconflow_api_key'}), 400

    try:
        asyncio_task = asyncio.create_task(run_transcription(video_path, cfg))
        asyncio_task.add_done_callback(
            lambda t: print(f"转录任务完成，异常={t.exception()}") if t.exception() else None
        )

        return jsonify({'success': True, 'task_id': task_id, 'message': '任务已启动'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/transcription/tasks')
async def list_transcription_tasks():
    """获取所有转录任务"""
    return jsonify({'success': True, 'tasks': get_all_tasks()})


@app.route('/api/transcription/tasks/<task_id>')
async def get_transcription_task(task_id):
    """查询单个转录任务状态"""
    task = get_task(task_id)
    if not task:
        return jsonify({'success': False, 'error': '任务不存在'}), 404
    return jsonify({'success': True, 'task': task.to_dict()})


@app.route('/api/transcription/result/<path:video_path>')
async def get_transcription_result(video_path=''):
    """获取转录结果文本内容"""
    if not video_path:
        return jsonify({'success': False, 'error': '缺少文件路径'}), 400

    video_path = _normalize_video_path(video_path)
    cfg = load_transcription_config()
    allowed_dir = cfg.get('scan_directory', '/storage')

    task = get_task_by_path(video_path)
    if task and task.status == 'done' and task.result_text:
        return jsonify({
            'success': True,
            'video_path': video_path,
            'output_txt_path': task.output_txt_path,
            'text': task.result_text,
        })

    text = get_tran_content(video_path, allowed_dir)
    if text is not None:
        return jsonify({
            'success': True,
            'video_path': video_path,
            'output_txt_path': _get_output_txt_path(video_path),
            'text': text,
        })

    return jsonify({'success': False, 'error': '转录结果不存在'}), 404


@app.route('/api/transcription/download/<path:video_path>')
async def download_transcription(video_path=''):
    """下载转录结果文件"""
    if not video_path:
        return jsonify({'success': False, 'error': '缺少文件路径'}), 400

    video_path = _normalize_video_path(video_path)
    cfg = load_transcription_config()
    allowed_dir = cfg.get('scan_directory', '/storage')

    text = get_tran_content(video_path, allowed_dir)
    if text is None:
        return jsonify({'success': False, 'error': '转录文件不存在或路径越权'}), 404

    filename = os.path.basename(_get_output_txt_path(video_path))
    return Response(
        text,
        mimetype='text/plain; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'}
    )


@app.route('/api/transcription/tasks/<task_id>', methods=['DELETE'])
async def delete_transcription_task(task_id):
    """删除转录任务记录"""
    if delete_task(task_id):
        return jsonify({'success': True, 'message': '已删除'})
    return jsonify({'success': False, 'error': '任务不存在'}), 404


if __name__ == "__main__":
    app.run(host='0.0.0.0', port=3660, debug=True)
