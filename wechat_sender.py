import requests
import json
import sys
import math
import re

# 定义企业微信Markdown消息的最大字节数。官方限制为4096字节。
# 我们设置为3800字节，以留出足够的安全余量，并生成更长的、符合用户预期的消息段落。
CHUNK_SIZE = 3800

def _sanitize_markdown(content):
    """
    清理Markdown，移除企业微信不支持的语法（如列表），转换为纯文本替代。
    """
    lines = content.split('\n')
    sanitized_lines = []
    for line in lines:
        trimmed_line = line.lstrip()
        # 将有序列表 "1. " 转换为 "(1) "
        match = re.match(r'^(\d+)\.\s+', trimmed_line)
        if match:
            indentation = line[:len(line) - len(trimmed_line)]
            rest_of_line = trimmed_line[len(match.group(0)):]
            sanitized_lines.append(f"{indentation}({match.group(1)}) {rest_of_line}")
            continue
        # 将无序列表 "* " 或 "- " 转换为 "• "
        if trimmed_line.startswith('* ') or trimmed_line.startswith('- '):
            indentation = line[:len(line) - len(trimmed_line)]
            sanitized_lines.append(indentation + '• ' + trimmed_line[2:])
            continue
        sanitized_lines.append(line)
    return '\n'.join(sanitized_lines)

def _send_raw_payload(webhook_url, payload):
    """内部函数，用于发送一个原始的、已构建好的payload字典。"""
    headers = {'Content-Type': 'application/json'}
    try:
        # 确保 payload 是 UTF-8 编码的 JSON 字符串
        payload_json = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        response = requests.post(webhook_url, headers=headers, data=payload_json, timeout=10)
        
        if response.status_code == 200 and response.json().get("errcode") == 0:
            print(f"企业微信消息发送成功！消息类型: {payload.get('msgtype')}", file=sys.stderr)
        else:
            print(f"企业微信消息发送失败: {response.text}", file=sys.stderr)
    except requests.exceptions.RequestException as e:
        print(f"调用企业微信Webhook时出错: {e}", file=sys.stderr)

def send_wechat_message(message_data):
    """
    向企业微信发送消息。
    - 如果 message_data 是一个字典, 将其作为完整的 payload 直接发送 (用于卡片等)。
    - 如果 message_data 是一个字符串, 将其作为 Markdown 内容处理。
      如果内容过长，会自动按行、按字节进行双重安全分割，确保消息能成功发送。
    """
    try:
        with open('config.json', 'r') as f:
            config = json.load(f)
        webhook_url = config.get('wechat_webhook_url')

        if not webhook_url:
            print("未找到企业微信的Webhook地址，请在设置页面中配置。", file=sys.stderr)
            return

        if isinstance(message_data, dict):
            _send_raw_payload(webhook_url, message_data)
            return

        if not isinstance(message_data, str):
            print(f"不支持的消息数据类型: {type(message_data)}", file=sys.stderr)
            return

        # 在处理之前，先清理所有不支持的Markdown语法
        message_content = _sanitize_markdown(message_data.strip())
        
        if len(message_content.encode('utf-8')) <= CHUNK_SIZE:
            payload = {"msgtype": "markdown", "markdown": {"content": message_content}}
            _send_raw_payload(webhook_url, payload)
            return

        # --- 终极稳健的分割逻辑 ---
        header = "### AI分析报告"
        header_found = any(line.strip().startswith('###') for line in message_content.split('\n'))
        if header_found:
            for line in message_content.split('\n'):
                if line.strip().startswith('###'):
                    header = line.strip()
                    break
        
        # 预留给标题、分段信息和一些安全余量
        MAX_CONTENT_BYTES = CHUNK_SIZE - len(header.encode('utf-8')) - 50

        # 1. 预处理：将所有原始行进行拆分，确保没有任何一个单行超过长度限制
        safe_lines = []
        for line in message_content.split('\n'):
            if len(line.encode('utf-8')) > MAX_CONTENT_BYTES:
                temp_line = line
                while len(temp_line.encode('utf-8')) > MAX_CONTENT_BYTES:
                    # 从字符数估算一个安全的切点
                    avg_bytes_per_char = len(temp_line.encode('utf-8')) / len(temp_line) if len(temp_line) > 0 else 1
                    estimated_chars = int(MAX_CONTENT_BYTES / avg_bytes_per_char * 0.9) # 留10%余量
                    
                    # 确保切点有效
                    if estimated_chars <= 0: estimated_chars = 1
                    
                    split_pos = estimated_chars
                    # 调整切点，避免切断多字节字符
                    while len(temp_line[:split_pos].encode('utf-8')) > MAX_CONTENT_BYTES:
                        split_pos -= 1
                    
                    safe_lines.append(temp_line[:split_pos])
                    temp_line = temp_line[split_pos:]
                if temp_line:
                    safe_lines.append(temp_line)
            else:
                safe_lines.append(line)

        # 2. 组合：将安全行组合成最终的消息块
        chunks = []
        current_chunk_lines = []
        current_len_bytes = 0
        for line in safe_lines:
            line_bytes_len = len(line.encode('utf-8'))
            if current_len_bytes + line_bytes_len + 1 > MAX_CONTENT_BYTES:
                if current_chunk_lines:
                    chunks.append("\n".join(current_chunk_lines))
                current_chunk_lines = [line]
                current_len_bytes = line_bytes_len
            else:
                current_chunk_lines.append(line)
                current_len_bytes += line_bytes_len + 1
        
        if current_chunk_lines:
            chunks.append("\n".join(current_chunk_lines))

        # 3. 发送
        final_chunks = [c for c in chunks if c.strip()]
        num_chunks = len(final_chunks)
        for i, chunk in enumerate(final_chunks):
            part_header = f"{header} (第 {i+1}/{num_chunks} 部分)"
            
            if chunk.strip().startswith('###') and header_found:
                message_to_send = chunk.replace(header, part_header, 1)
            else:
                message_to_send = f"{part_header}\n\n{chunk}"

            chunk_payload = {"msgtype": "markdown", "markdown": {"content": message_to_send}}
            _send_raw_payload(webhook_url, chunk_payload)

    except FileNotFoundError:
        print("配置文件 'config.json' 未找到。", file=sys.stderr)
    except Exception as e:
        print(f"发送企业微信消息时出现未知错误: {e}", file=sys.stderr)


if __name__ == '__main__':
    # --- 测试1: 发送卡片消息 ---
    test_card_payload = {
        "msgtype": "template_card",
        "template_card": {
            "card_type": "text_notice",
            "source": {"desc": "AI学习助手"},
            "main_title": {"title": "AI 分析完成通知 (卡片测试)"},
            "horizontal_content_list": [{"keyname": "状态", "value": "测试成功"}],
            "card_action": {"type": 1, "url": "https://work.weixin.qq.com"}
        }
    }
    print("--- 正在发送测试卡片消息 ---")
    send_wechat_message(test_card_payload)

    # --- 测试2: 发送超长Markdown消息 ---
    long_message = (
        "### 超长Markdown消息测试\n\n"
        "这是消息的第一部分。" + "测试内容..." * 200 + "\n\n"
        "这是消息的第二部分。" + "更多测试..." * 200 + "\n\n"
        "这是消息的结尾部分。"
    )
    print(f"\n--- 正在发送总长度为 {len(long_message)} 的超长消息 ---")
    send_wechat_message(long_message)
