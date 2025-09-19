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

### 3. 安装依赖

安装项目所需的所有 Python 库：

```bash
pip install -r requirements.txt
```

### 4. 配置应用

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
    "app_base_url": "在此处输入您的应用基础 URL (例如: http://localhost:5000)"
}
```

- `api_url`, `api_key`, `model`: AI 服务的相关凭证。
- `wechat_webhook_url`: 如果您想每天接收学习计划的推送，请配置此项。
- `app_base_url`: 应用部署后可访问的地址。

### 5. 初始化数据库

首次运行时，需要初始化数据库。

```bash
flask db init
flask db migrate -m "Initial migration."
flask db upgrade
```
*注意: 根据 `app.py` 的代码，数据库会在首次运行时自动创建，但如果模型发生变化，以上命令会很有用。*

## 运行项目

您可以通过以下任一方式运行此应用。

### 直接运行 (用于开发)

```bash
python3 app.py
```

应用将在 `http://127.0.0.1:5000` 上启动。

### 使用 Gunicorn (用于生产)

为了获得更好的性能和稳定性，建议在生产环境中使用 Gunicorn。

```bash
gunicorn -w 4 -b 0.0.0.0:5000 app:app
```

### 使用启动脚本

项目中还提供了一个 `start.sh` 脚本，它会以后台进程的方式启动 Gunicorn 服务。

```bash
chmod +x start.sh
./start.sh
```

## 如何使用

1.  **启动应用**: 根据上述指南运行应用。
2.  **访问 Web 界面**: 在浏览器中打开 `http://<您的服务器地址或localhost>:5000`。
3.  **上传文件**:
    -   在 "课程表" 部分，上传您的课程安排 `.xlsx` 文件。
    -   在 "考试要求" 部分，上传您的考试大纲 `.docx` 文件。
4.  **生成计划**: 点击 "生成学习计划" 按钮，AI 将开始分析并为您创建每日任务。
5.  **查看计划**: 生成的计划会显示在主页，并且如果您配置了微信 Webhook，计划将自动推送到您的企业微信。

## 贡献

欢迎任何形式的贡献！如果您有任何建议或发现了 Bug，请随时提交 Issue 或 Pull Request。
