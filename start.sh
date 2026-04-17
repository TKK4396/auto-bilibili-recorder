#!/bin/bash
# 启动主服务 (process_video.py) 和管理页面 (admin_server.py)

. /webhook/venv/bin/activate

# 后台运行管理页面
python3 -u /webhook/admin_server.py &
ADMIN_PID=$!

# 后台运行主服务
python3 -u /webhook/process_video.py &
MAIN_PID=$!

# 等待任意进程退出
wait $MAIN_PID
