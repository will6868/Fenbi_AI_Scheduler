#!/bin/bash

# --- 服务器与项目配置 ---
SERVER_IP="8.141.16.67"
SERVER_USER="root"
SERVER_PASS="Xutao147"
PROJECT_NAME="JIHUA"
REMOTE_DIR="/root/$PROJECT_NAME"
ARCHIVE_NAME="project.tar.gz"
# 假设 app.py 是主程序入口文件，如果不是请修改
ENTRY_POINT="app.py" 

# --- 脚本开始 ---
echo "🚀 开始部署项目到服务器 $SERVER_IP..."

# 检查本地是否安装 sshpass 工具
if ! command -v sshpass &> /dev/null
then
    echo "❌ 检测到 sshpass 未安装，请先安装该工具。"
    echo "   - macOS: brew install hudochenkov/sshpass/sshpass"
    echo "   - Debian/Ubuntu: sudo apt-get install sshpass"
    exit 1
fi

echo "1. 正在创建项目压缩包..."
# 打包项目文件，排除掉一些无需上传的文件
tar --exclude='.DS_Store' --exclude="$ARCHIVE_NAME" --exclude='deploy.sh' --exclude='*.log' -czvf $ARCHIVE_NAME .
if [ $? -ne 0 ]; then
    echo "❌ 创建压缩包失败，部署中止。"
    exit 1
fi
echo "✅ 压缩包创建成功: $ARCHIVE_NAME"

echo "2. 正在上传项目到服务器..."
sshpass -p "$SERVER_PASS" scp -o StrictHostKeyChecking=no $ARCHIVE_NAME ${SERVER_USER}@${SERVER_IP}:/tmp/
if [ $? -ne 0 ]; then
    echo "❌ 上传失败，请检查服务器信息、密码和网络连接。部署中止。"
    exit 1
fi
echo "✅ 项目上传成功。"

echo "3. 正在服务器上配置并启动项目..."
# 通过 SSH 连接到远程服务器并执行一系列命令
sshpass -p "$SERVER_PASS" ssh -o StrictHostKeyChecking=no ${SERVER_USER}@${SERVER_IP} << 'EOF'
    # --- 以下是在服务器上执行的命令 ---
    
    # 从父脚本中获取变量
    REMOTE_DIR="/root/JIHUA"
REMOTE_DIR="/root/JIHUA"
    PROJECT_NAME="JIHUA"
    ARCHIVE_NAME="project.tar.gz"
    ENTRY_POINT="app.py"

    echo "   -> 正在更新软件包列表..."
    apt-get update -y > /dev/null
    
    echo "   -> 正在安装系统依赖: python3, pip, venv, screen..."
    apt-get install -y python3-pip python3-venv screen > /dev/null
    
    echo "   -> 正在创建项目目录: $REMOTE_DIR"
    mkdir -p $REMOTE_DIR
    
    echo "   -> 正在解压项目文件..."
    tar -xzvf /tmp/$ARCHIVE_NAME -C $REMOTE_DIR > /dev/null
    
    cd $REMOTE_DIR
    
    echo "   -> 正在配置 Python 虚拟环境..."
    python3 -m venv venv
    
    echo "   -> 正在激活虚拟环境并安装项目依赖..."
    source venv/bin/activate
    pip install -r requirements.txt
    
    echo "   -> 正在启动应用程序和 Worker..."
    # 定义会话名称
    APP_SCREEN_NAME="jihua"
    WORKER_SCREEN_NAME="worker"

    # 如果已存在同名 screen 会话，先关闭它们
    echo "   -> 正在停止旧的 screen 会话..."
    screen -S $APP_SCREEN_NAME -X quit || true
    screen -S $WORKER_SCREEN_NAME -X quit || true
    
    # 在新的后台 screen 会话中启动应用
    echo "   -> 正在后台启动主应用 (jihua)..."
    # 使用 gunicorn 启动，假设 app.py 中的 Flask 实例名为 app
    screen -dmS $APP_SCREEN_NAME bash -c "source venv/bin/activate; gunicorn --workers 4 --bind 0.0.0.0:8000 app:app"
    
    # 在另一个后台 screen 会话中启动 worker
    echo "   -> 正在后台启动 RQ Worker (worker)..."
    screen -dmS $WORKER_SCREEN_NAME bash -c "source venv/bin/activate; python3 worker.py"
    
    echo "✅ 应用和 Worker 已在 screen 会话中成功启动！"
    echo "   - 主应用会话: 'jihua' (通过 'screen -r jihua' 查看)"
    echo "   - Worker 会话: 'worker' (通过 'screen -r worker' 查看)"
    echo "   - 若要从会话中分离（让应用在后台继续运行），请按 Ctrl+A 然后按 D。"
    
    # --- 服务器命令执行完毕 ---
EOF

if [ $? -ne 0 ]; then
    echo "❌ 远程服务器配置失败，请检查上面的日志输出。"
    exit 1
fi

echo "4. 正在清理本地临时文件..."
rm $ARCHIVE_NAME
echo "✅ 本地清理完成。"

echo "🎉 部署脚本执行完毕！"
echo ""
echo "⚠️ 重要安全警告 ⚠️"
echo "此脚本在文件中保存了您的明文密码，非常不安全。"
echo "强烈建议您配置 SSH 密钥认证来替代密码登录，以增强服务器安全性。"
