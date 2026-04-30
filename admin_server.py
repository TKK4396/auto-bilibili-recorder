from quart import Quart, jsonify, request, render_template
import json
import yaml
import os
from db_manager import DBManager

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

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=3660, debug=True)
