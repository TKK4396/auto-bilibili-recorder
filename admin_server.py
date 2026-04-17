from quart import Quart, jsonify, request, render_template
import json
from db_manager import DBManager

app = Quart(__name__)

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

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=3660, debug=True)
