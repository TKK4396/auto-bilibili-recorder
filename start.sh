#!/bin/bash
# 启动主服务 (process_video.py) 和管理页面 (admin_server.py)

. /webhook/venv/bin/activate

cleanup() {
    echo "收到终止信号，正在停止所有服务..."
    kill $ADMIN_PID $MAIN_PID 2>/dev/null
    wait $ADMIN_PID $MAIN_PID 2>/dev/null
    echo "所有服务已停止"
    exit 0
}

trap cleanup SIGTERM SIGINT

# 后台运行管理页面
python3 -u /webhook/admin_server.py &
ADMIN_PID=$!

# 后台运行主服务
python3 -u /webhook/process_video.py &
MAIN_PID=$!

echo "服务已启动: admin_server(PID=$ADMIN_PID) process_video(PID=$MAIN_PID)"

# 等待任一进程退出（无论哪个崩溃都会触发容器重启）
wait -n $ADMIN_PID $MAIN_PID
exit_code=$?

echo "进程退出 (exit code: $exit_code)，正在清理..."
cleanup
