# Fenbi AI Scheduler

Fenbi AI Scheduler 是一个利用人工智能技术，根据用户提供的课程表和考试要求，智能规划每日学习任务的自动化工具。它可以帮助用户高效备考，合理分配学习时间。

## 主要功能

- **智能任务规划**: 基于 AI 分析课程表和考试大纲，生成每日学习计划。
- **微信消息推送**: 每日自动将学习计划推送到企业微信。
- **Web 界面**: 提供简洁的 Web 界面，用于配置、管理和查看学习计划。
- **灵活配置**: 支持自定义 AI 模型、API 地址等参数。

## 安装与部署

请按照以下步骤在您的本地或服务器环境中部署 Fenbi AI Scheduler。

### 1. 克隆项目

首先，将项目代码克隆到您的本地设备：

```bash
git clone https://github.com/will6868/Fenbi_AI_Scheduler.git
cd Fenbi_AI_Scheduler
```

### 2. 创建虚拟环境

为了隔离项目依赖，建议使用虚拟环境。

```bash
python3 -m venv venv
source venv/bin/activate  # 在 Windows 上使用 `venv\Scripts\activate`
```

### 3. 安装并启动 Redis

本项目使用 Redis 作为消息队列，因此需要先安装并启动 Redis 服务。

**对于 Linux (Ubuntu/Debian):**

```bash
sudo apt update
sudo apt install redis-server
sudo systemctl start redis-server
sudo systemctl enable redis-server  # 设置开机自启
```

**对于 Windows:**

Windows 用户可以从 Redis 的 GitHub 发布页面下载最新的 `.msi` 安装包。

1.  访问 [Redis on Windows 发布页面](https://github.com/tporadowski/redis/releases)。
2.  下载最新的 `Redis-x.x.x-x64-xxx.msi` 文件。
3.  运行安装程序，并按照提示完成安装。安装过程中请确保勾选 "Add the Redis installation folder to the PATH environment variable"。
4.  安装完成后，Redis 服务会自动在后台运行。

### 4. 安装依赖

安装项目所需的所有 Python 库：

```bash
pip install -r requirements.txt
```

### 5. 配置应用

项目需要一个 `config.json` 文件来存储敏感信息和关键配置。我们提供了一个模板文件 `config.json.example`，您可以复制并修改它。

```bash
cp config.json.example config.json
```

接下来，编辑 `config.json` 文件，填入您的个人信息：

```json
{
    "api_url": "在此处输入您的 AI 服务 API URL",
    "api_key": "在此处输入您的 API 密钥",
    "model": "在此处输入您希望使用的 AI 模型名称",
    "wechat_webhook_url": "在此处输入您的企业微信 Webhook URL (可选，用于接收每日计划)",
    "app_base_url": "在此处输入您的应用基础 URL (例如: http://localhost:58000)"
}
```

- `api_url`, `api_key`, `model`: AI 服务的相关凭证。
- `wechat_webhook_url`: 如果您想每天接收学习计划的推送，请配置此项。
- `app_base_url`: 应用部署后可访问的地址。

### 6. 数据库说明

本项目的数据库是自动创建的，您**无需**执行任何手动初始化命令。

当您首次运行应用并开始使用各项功能时，所需的数据库文件会自动在 `database/` 和 `instance/` 目录下生成。

## 运行项目

您可以通过以下任一方式运行此应用。

### 直接运行 (用于开发)

```bash
python3 app.py
```

应用将在 `http://127.0.0.1:58000` 上启动。

### 使用 Gunicorn (用于生产)

为了获得更好的性能和稳定性，建议在生产环境中使用 Gunicorn。

```bash
gunicorn -w 4 -b 0.0.0.0:58000 app:app
```

### 使用启动脚本

项目中还提供了一个 `start.sh` 脚本，它会以后台进程的方式启动 Gunicorn 服务。

```bash
chmod +x start.sh
./start.sh
```

## 如何使用

1.  **启动应用**: 根据上述指南运行应用。
2.  **访问 Web 界面**: 在浏览器中打开 `http://<您的服务器地址或localhost>:58000`。
3.  **上传文件**:
    -   在 "课程表" 部分，上传您的课程安排 `.xlsx` 文件。
    -   在 "考试要求" 部分，上传您的考试大纲 `.docx` 文件。
4.  **生成计划**: 点击 "生成学习计划" 按钮，AI 将开始分析并为您创建每日任务。
5.  **查看计划**: 生成的计划会显示在主页，并且如果您配置了微信 Webhook，计划将自动推送到您的企业微信。

## 贡献

欢迎任何形式的贡献！如果您有任何建议或发现了 Bug，请随时提交 Issue 或 Pull Request。
