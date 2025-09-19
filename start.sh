#!/bin/bash

# --- 本地开发配置 ---
GUNICORN_WORKERS=4
GUNICORN_BIND="0.0.0.0:5002" # 使用本地开发端口
APP_INSTANCE="app:app"
GUNICORN_PID_FILE="gunicorn.pid"
WORKER_PID_FILE="worker.pid"

# --- 函数定义 ---

# 目录初始化函数
setup_directories() {
    echo "   -> 检查并创建所需目录..."
    DIRS=("database" "instance" "uploads" "user_data" "user_data/course_schedule" "user_data/exam_requirements" "uploads/temp_prompt_files")
    for dir in "${DIRS[@]}"; do
        if [ ! -d "$dir" ]; then
            mkdir -p "$dir"
            echo "      -> 目录 '$dir' 已创建。"
        fi
    done
}

# 启动函数
start() {
    setup_directories # 初始化目录
    echo "🚀 开始启动应用 (本地开发模式)..."
    
    # 检查并激活虚拟环境
    if [ -d "venv" ]; then
        echo "   -> 激活虚拟环境..."
        source venv/bin/activate
    else
        echo "   -> 警告: 未找到 'venv' 虚拟环境，将使用系统 Python 环境。"
    fi
    
    echo "1. 正在启动主应用 (Gunicorn)..."
    # 检查进程是否已在运行
    if [ -f $GUNICORN_PID_FILE ]; then
        echo "   -> Gunicorn PID 文件已存在，请先执行 stop。"
    else
        # 使用 nohup 在后台运行，并将日志重定向
        nohup gunicorn --workers $GUNICORN_WORKERS --bind $GUNICORN_BIND --pid $GUNICORN_PID_FILE $APP_INSTANCE >> gunicorn.log 2>&1 &
        echo "   ✅ Gunicorn 已在后台启动。日志请查看 gunicorn.log"
    fi

    echo "2. 正在启动 RQ Worker..."
    if [ -f $WORKER_PID_FILE ]; then
        echo "   -> Worker PID 文件已存在，请先执行 stop。"
    else
        # 使用 nohup 在后台运行，并将日志重定向
        nohup python worker.py >> worker.log 2>&1 &
        echo $! > $WORKER_PID_FILE
        echo "   ✅ RQ Worker 已在后台启动。日志请查看 worker.log"
    fi
    
    echo "🎉 启动完成！"
    echo "   -> Gunicorn PID: $(cat gunicorn.pid 2>/dev/null || echo 'N/A')"
    echo "   -> Worker PID: $(cat worker.pid 2>/dev/null || echo 'N/A')"
}

# 停止函数
stop() {
    echo "🛑 正在停止应用..."
    
    echo "1. 正在停止主应用 (Gunicorn)..."
    if [ -f $GUNICORN_PID_FILE ]; then
        kill $(cat $GUNICORN_PID_FILE)
        rm $GUNICORN_PID_FILE
        echo "   ✅ Gunicorn 已停止。"
    else
        echo "   -> Gunicorn 未在运行。"
    fi
    
    echo "2. 正在停止 RQ Worker..."
    if [ -f $WORKER_PID_FILE ]; then
        kill $(cat $WORKER_PID_FILE)
        rm $WORKER_PID_FILE
        echo "   ✅ RQ Worker 已停止。"
    else
        echo "   -> RQ Worker 未在运行。"
    fi
    
    echo "✅ 停止操作完成！"
}

# 状态检查函数
status() {
    echo "📊 查看当前状态..."
    
    if [ -f $GUNICORN_PID_FILE ]; then
        echo "   -> Gunicorn 正在运行, PID: $(cat $GUNICORN_PID_FILE)"
    else
        echo "   -> Gunicorn 已停止。"
    fi
    
    if [ -f $WORKER_PID_FILE ]; then
        echo "   -> RQ Worker 正在运行, PID: $(cat $WORKER_PID_FILE)"
    else
        echo "   -> RQ Worker 已停止。"
    fi
}

# --- 主逻辑 ---
case "$1" in
    start)
        start
        ;;
    stop)
        stop
        ;;
    restart)
        stop
        sleep 2
        start
        ;;
    status)
        status
        ;;
    *)
        echo "用法: $0 {start|stop|restart|status}"
        exit 1
        ;;
esac

exit 0
