import sys
import json
import os
import requests
from datetime import datetime
from urllib.parse import quote
import logging
import time

# --- Setup Logging ---
# This logger will write to the same file as the main worker.
log_file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'worker.log')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [TaskRunner] - %(message)s',
    handlers=[
        logging.FileHandler(log_file_path),
        logging.StreamHandler(sys.stderr) # Also print to stderr for subprocess capture
    ]
)

# Set the environment variable right at the start, before any other imports
os.environ['OBJC_DISABLE_INITIALIZE_FORK_SAFETY'] = 'YES'

import markdown
from sqlalchemy.exc import SQLAlchemyError

from ai_analyzer import analyze_pdf_direct_non_stream, analyze_image_with_ai, analyze_text_direct_non_stream, get_ai_config
from app import save_analysis_to_db, app as flask_app, get_recent_history, get_all_history_data, extract_text_from_file
from database import CentralSession
from models import StudyPlan, DailySchedule, PracticeCategory
from services import _synchronize_plan_from_schedule
from wechat_sender import send_wechat_message
import pandas as pd
from collections import defaultdict

def run_file_analysis(task_details):
    """Handles the original file-based analysis."""
    filename = task_details.get('filename')
    goal = task_details.get('goal')
    category = task_details.get('category')
    date = task_details.get('date')
    upload_folder = task_details.get('upload_folder', 'uploads')

    if not filename:
        raise ValueError("Filename is missing from task details.")

    file_path = os.path.join(upload_folder, filename)
    file_ext = os.path.splitext(filename)[1].lower()

    analysis_function = None
    if file_ext == '.pdf':
        analysis_function = analyze_pdf_direct_non_stream
    elif file_ext in ['.png', '.jpg', '.jpeg', '.webp']:
        analysis_function = analyze_image_with_ai
    else:
        raise ValueError(f"Unsupported file type: {file_ext}")

    # Step 1: Perform the AI analysis
    ai_result = analysis_function(file_path, goal)
    if ai_result.get("error"):
        return ai_result

    # Step 2: Save the result to the database
    with flask_app.app_context():
        final_result = save_analysis_to_db(ai_result, category, date)

    # Step 3: Send notification from within the clean process
    try:
        summary = final_result.get("performance_summary", {})
        metadata = final_result.get("report_metadata", {})
        db_data = final_result.get("db_flat_data", {})

        # 从处理过的数据中提取所需字段
        submission_time = db_data.get("submission_time", "N/A")
        practice_type = db_data.get("practice_type", "N/A")
        difficulty = db_data.get("difficulty", "未指定")
        total_questions = summary.get("total_questions", 0)
        correct = summary.get("correct_answers", 0)
        incorrect = summary.get("incorrect_answers", 0)
        accuracy_answered_val = db_data.get("accuracy_rate_answered", 0.0) * 100
        accuracy_answered_str = f"{accuracy_answered_val:.1f}%"

        # --- 动态构建URL和加载配置 ---
        goal_id = None
        app_base_url = "http://127.0.0.1:5002" # Default URL
        
        # We need the app_context to use SQLAlchemy sessions
        with flask_app.app_context():
            session = CentralSession()
            try:
                plan = session.query(StudyPlan).filter_by(plan_date=date).first()
                if plan and plan.goals:
                    # Find the index of the first goal that matches the practice type
                    for i, goal_item in enumerate(plan.goals):
                        if goal_item.get('type') == practice_type:
                            goal_id = i
                            break
            finally:
                session.close()
        
        try:
            with open('config.json', 'r') as f:
                config = json.load(f)
            app_base_url = config.get('app_base_url', app_base_url).rstrip('/')
        except (FileNotFoundError, json.JSONDecodeError):
            logging.warning("config.json not found or malformed, using default base URL.")

        # 根据是否找到goal_id，构建不同的详情URL
        if goal_id is not None:
            # URL-encode the practice_type to handle special characters
            encoded_practice_type = quote(practice_type)
            details_url = f"{app_base_url}/training?type={encoded_practice_type}&date={date}&goal_id={goal_id}"
        else:
            # Fallback to the main page for the date if no specific goal is found
            details_url = f"{app_base_url}/?date={date}"

        # --- 构建卡片消息 ---
        card_payload = {
            "msgtype": "template_card",
            "template_card": {
                "card_type": "news_notice",
                "source": {
                    "icon_url": "https://wework.qpic.cn/wwpic/506411_5Vpkb-2BSRa-4_1628672380/0",
                    "desc": "AI学习助手"
                },
                "main_title": {
                    "title": "AI 分析完成通知",
                    "desc": f"文件: {metadata.get('filename', 'N/A')}"
                },
                "vertical_content_list": [
                    {
                        "title": f"✅ 已答正确率: {accuracy_answered_str}",
                        "desc": f"总题数: {total_questions} | 正确: {correct} | 错误: {incorrect}"
                    }
                ],
                "horizontal_content_list": [
                    { "keyname": "题型", "value": practice_type },
                    { "keyname": "难度", "value": str(difficulty) },
                    { "keyname": "提交时间", "value": submission_time }
                ],
                "jump_list": [
                    { "type": 1, "url": details_url, "title": "查看详细报告" }
                ],
                "card_action": { "type": 1, "url": details_url }
            }
        }

        # 动态添加图片URL (仅当文件是图片且base_url有效时)
        file_ext = os.path.splitext(filename)[1].lower()
        if file_ext in ['.png', '.jpg', '.jpeg', '.webp'] and app_base_url:
            image_url = f"{app_base_url}/uploads/{filename}"
            card_payload["template_card"]["card_image"] = {
                "url": image_url,
                "aspect_ratio": 1.30
            }

        send_wechat_message(card_payload)
    except Exception as notify_e:
        # Log error but don't fail the task
        logging.error(f"Failed to send WeChat notification: {notify_e}", exc_info=True)

    return final_result

def run_dashboard_analysis(task_details, is_automated=False):
    """
    Handles the data aggregation and AI analysis for the main analysis page.
    If is_automated is True, it will validate the output length.
    """
    # 1. Fetch all historical data
    all_history = get_all_history_data()
    if not all_history:
        return {"analysis": "没有足够的数据来进行分析。请先完成一些练习。"}

    # 2. Aggregate the data using pandas for robust analysis
    df = pd.DataFrame(all_history)
    
    # Convert relevant columns to numeric, coercing errors
    numeric_cols = ['total_questions', 'questions_answered', 'correct_answers', 'incorrect_answers', 'unanswered_questions', 'total_time_minutes']
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors='coerce')
    
    df['submission_time'] = pd.to_datetime(df['submission_time'], errors='coerce')
    df.dropna(subset=['submission_time'], inplace=True)
    df.sort_values('submission_time', inplace=True)

    # Overall Trend Analysis
    df['accuracy_answered'] = (df['correct_answers'] / df['questions_answered']).fillna(0) * 100
    daily_accuracy = df.resample('D', on='submission_time')['accuracy_answered'].mean().reset_index()
    # Convert timestamp to string to ensure JSON serializability
    daily_accuracy['submission_time'] = daily_accuracy['submission_time'].dt.strftime('%Y-%m-%d')
    overall_trend = daily_accuracy.to_dict('records')

    # Type-specific Aggregates and Trends
    type_aggregates = defaultdict(lambda: {'total_q': 0, 'correct_q': 0, 'total_time': 0, 'records': 0})
    type_trends = defaultdict(list)

    for p_type, group in df.groupby('practice_type'):
        agg = type_aggregates[p_type]
        agg['total_q'] = int(group['questions_answered'].sum())
        agg['correct_q'] = int(group['correct_answers'].sum())
        agg['total_time'] = int(group['total_time_minutes'].sum())
        agg['records'] = len(group)
        agg['avg_accuracy'] = (agg['correct_q'] / agg['total_q'] * 100) if agg['total_q'] > 0 else 0

        daily_type_accuracy = group.resample('D', on='submission_time')['accuracy_answered'].mean().reset_index()
        # Convert timestamp to string to ensure JSON serializability
        daily_type_accuracy['submission_time'] = daily_type_accuracy['submission_time'].dt.strftime('%Y-%m-%d')
        type_trends[p_type] = daily_type_accuracy.to_dict('records')

    # Prepare the final data structure for the AI
    dashboard_data = {
        "overallTrend": overall_trend,
        "typeAggregates": dict(type_aggregates),
        "typeTrends": dict(type_trends)
    }

    # 3. Call the AI analysis function with the prepared data
    config = get_ai_config()
    api_url, api_key, model = config.get('api_url'), config.get('api_key'), config.get('model')

    if not all([api_url, api_key, model]):
        ai_result = {"error": "AI configuration is incomplete."}
    else:
        prompt = f"""
        You are an expert data analyst and strategic learning coach for a civil service exam candidate.
        Your task is to provide a comprehensive, multi-dimensional analysis based on the user's aggregated performance data.
        The entire response MUST be in Chinese.

        **Aggregated Performance Data:**
        ```json
        {json.dumps(dashboard_data, indent=2, ensure_ascii=False)}
        ```

        **Your Analysis MUST Cover Three Dimensions:**

        1.  **Past Performance Summary (回顾过去):**
            -   Summarize the user's historical performance based on the provided data.
            -   Identify consistent strengths and weaknesses across different question types (`typeAggregates`).
            -   Analyze the `overallTrend` and `typeTrends` to comment on their learning trajectory. Was there improvement, decline, or stagnation?

        2.  **Current Standing Assessment (立足现在):**
            -   Based on the most recent data points, what is the user's current skill level?
            -   Which areas require immediate attention?
            -   Highlight any recent breakthroughs or regressions.

        3.  **Future Projections & Strategic Recommendations (展望未来):**
            -   Based on the trends, what is the likely outcome if the user continues on their current path?
            -   Provide concrete, actionable, and targeted recommendations.
            -   Suggestions should be specific. For example, instead of "practice more," suggest "Focus on '资料分析' for the next 3 days, specifically targeting questions involving percentage growth, as your accuracy in this area has been consistently below 60%."
            -   Recommend a focus for the next study period (e.g., next week).

        **Output Format:**
        Return a single JSON object with one key: `"analysis"`.
        The value of `"analysis"` should be a well-structured, insightful, and encouraging text report in Chinese. Use markdown for formatting (e.g., headings, bold text, lists) to improve readability.
        """

        json_schema = {
            "type": "OBJECT",
            "properties": {"analysis": {"type": "STRING"}},
            "required": ["analysis"]
        }

        base_url = api_url.rstrip('/')
        if '/v1' in base_url:
            base_url = base_url.split('/v1')[0]
        api_endpoint = f"{base_url}/v1beta/models/{model}:generateContent?key={api_key}"

        headers = {"Content-Type": "application/json"}
        payload = {
          "contents": [{"parts": [{"text": prompt}]}],
          "generationConfig": {
            "response_mime_type": "application/json",
            "response_schema": json_schema,
            "temperature": 0.6
          }
        }

        try:
            proxies = {"http": None, "https": None}
            response = requests.post(api_endpoint, headers=headers, json=payload, timeout=600, proxies=proxies)
            response.raise_for_status()
            response_json = response.json()
            full_text = response_json['candidates'][0]['content']['parts'][0]['text']
            json_start = full_text.find('{')
            json_end = full_text.rfind('}')
            if json_start != -1 and json_end != -1:
                json_string = full_text[json_start : json_end + 1].strip()
                ai_result = json.loads(json_string)
            else:
                ai_result = {"error": "Could not find a valid JSON block in the AI response.", "details": full_text}
        except requests.exceptions.RequestException as e:
            error_details = e.response.text if e.response is not None else "No response from server."
            ai_result = {"error": f"AI API call failed: {e}", "details": error_details}
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            error_details = response.text if 'response' in locals() else 'No response'
            ai_result = {"error": f"Failed to parse AI response: {e}", "details": error_details}

    # 4. Send notification from within the clean process
    try:
        status_text = "分析异常"
        analysis_summary = "AI分析返回了未知的数据格式，请检查后台日志。"

        if ai_result and "analysis" in ai_result:
            status_text = "已生成"
            # 截取报告的前50个字符作为摘要
            analysis_summary = ai_result["analysis"][:100] + "..." if len(ai_result["analysis"]) > 100 else ai_result["analysis"]
        elif ai_result and "error" in ai_result:
            analysis_summary = f"AI分析失败: {ai_result.get('details', '无')}"

        generation_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        app_base_url = "http://127.0.0.1:5002"
        try:
            with open('config.json', 'r') as f:
                config = json.load(f)
            app_base_url = config.get('app_base_url', app_base_url).rstrip('/')
        except (FileNotFoundError, json.JSONDecodeError):
            logging.warning("config.json not found, using default base URL.")

        details_url = f"{app_base_url}/analysis"

        if ai_result and "analysis" in ai_result:
            # 为完整分析报告添加标题，然后直接发送
            full_report_markdown = f"### AI 综合分析报告\n\n{ai_result['analysis']}"
            send_wechat_message(full_report_markdown)
        else:
            # 错误回退
            error_message = f"### AI 综合分析报告\n\n分析失败: {ai_result.get('details', '无')}"
            send_wechat_message(error_message)
    except Exception as notify_e:
        # Log error but don't fail the task
        logging.error(f"Failed to send comprehensive analysis WeChat notification: {notify_e}", exc_info=True)

    # 5. Validate and Perform Markdown Conversion
    if ai_result and "analysis" in ai_result:
        # Validation for automated tasks
        if is_automated and len(ai_result["analysis"]) < 400:
            raise ValueError(f"Validation failed: Dashboard analysis report is too short ({len(ai_result['analysis'])} chars). Retrying.")
        
        # Perform the markdown conversion that used to be in a separate job
        ai_result["analysis"] = markdown.markdown(ai_result["analysis"])
    elif is_automated:
        # If the task failed before generating analysis, raise an error to retry
        error_detail = ai_result.get('details', 'Unknown error')
        raise ValueError(f"Task failed before validation: {error_detail}")

    return ai_result

def run_schedule_adjustment(task_details, is_automated=False):
    """
    Handles the AI-based schedule adjustment.
    If is_automated is True, it will validate the output structure.
    """
    if is_automated:
        # For automated runs, we need to fetch the data first.
        start_date_str = task_details.get("date")
        if not start_date_str:
            raise ValueError("Missing 'date' for automated schedule adjustment.")
        
        # The automated task needs the combined_data, which is fetched here.
        from services import get_schedule_and_history_for_ai
        combined_data = get_schedule_and_history_for_ai(start_date_str=start_date_str, days=7)
        user_request = task_details.get("user_request", "仔细规划今日计划")
    else:
        # For manual runs, data is passed directly.
        combined_data = task_details.get("combined_data")
        user_request = task_details.get("user_request")
        start_date_str = task_details.get("start_date_str")

    if not all([combined_data, user_request, start_date_str]):
        raise ValueError("Missing required details for schedule adjustment.")

    config = get_ai_config()
    api_url, api_key, model = config.get('api_url'), config.get('api_key'), config.get('model')

    if not all([api_url, api_key, model]):
        return {"error": "AI configuration is incomplete."}

    course_prompt = ""
    exam_prompt = ""
    try:
        with open('user_data/course_schedule/generated_prompt.txt', 'r', encoding='utf-8') as f:
            course_prompt = f.read()
    except FileNotFoundError:
        pass

    try:
        with open('user_data/exam_requirements/generated_prompt.txt', 'r', encoding='utf-8') as f:
            exam_prompt = f.read()
    except FileNotFoundError:
        pass

    prompt = f"""
    You are a world-class AI assistant and expert study coach. Your goal is to create a smart, adaptive, and data-driven study plan. Respond as efficiently as possible.

    **Core Directives (Strictly Follow):**
    1.  **Course Schedule Constraints:**
        ```
        {course_prompt}
        ```
    2.  **Exam Requirements:**
        ```
        {exam_prompt}
        ```

    **Context:**
    1.  **Today's Date:** {start_date_str}
    2.  **User's Multi-Day Plan & Goals:** The user's existing schedule for the upcoming days.
        ```json
        {json.dumps(combined_data.get("multi_day_plan", {}), indent=2, ensure_ascii=False)}
        ```
    3.  **User's Historical Performance:** Recent practice session data, including accuracy and AI-generated analysis of weaknesses.
        ```json
        {json.dumps(combined_data.get("historical_performance", []), indent=2, ensure_ascii=False)}
        ```
    4.  **User's Immediate Request:** The specific change the user wants to make right now.
        "{user_request}"

    **Your Core Task:**
    Analyze the user's request in the context of their long-term goals and recent performance. Adjust the schedule and goals holistically, not just for today, but for subsequent days to ensure goals are met.

    **Mandatory Instructions:**
    1.  **Dynamic Adjustment & Phase Focus (NEW CRITICAL RULE):**
        -   **Phase Division:** The user's exam is at the end of November. You MUST structure the plan with distinct phases.
        -   **Phase 1 (Now - End of October): Foundational Period.** While accommodating the user's school schedule, the primary focus is on tackling 1-2 major weak areas (like Quantitative Relations, Data Analysis) and completing the basic introduction to Shen Lun.
        -   **Phase 2 (November - Exam): Sprint & Simulation Period.** As the user's school courses decrease, leverage the larger blocks of free time to shift focus to full-length mock exams and comprehensive review to identify and fill any remaining knowledge gaps.
        -   **Flexibility:** The plan must account for special events mentioned in the course schedule (e.g., sports meet, holidays). Suggest a "weekly review" session on weekends to allow for minor adjustments to the upcoming week's plan based on progress.
        -   **Shen Lun Embargo:** Do NOT schedule any "申论" (Shen Lun) training sessions before October 15, 2025. The user wants to focus exclusively on other subjects until that date.
    2.  **Dual-Focus Adjustments (CRITICAL):**
        -   **Schedule Adjustments:** If the user's request is about time or activities (e.g., "cancel a session", "add a new training"), you must modify the `detailed
        -   **Goal Adjustments:** If the user's request is about performance targets (e.g., "change question count to 40", "set accuracy to 80%"), you MUST modify the corresponding goal object within the `study_goals` array.
        -   **Do not confuse the two.** A request to change the number of questions for "资料分析" should change `target_questions` in `study_goals`, NOT the `details` field of a schedule item.
    2.  **Data-Driven Planning:**
        -   Analyze `historical_performance`. Identify weak points (e.g., low accuracy in "资料分析").
        -   Proactively schedule **targeted training sessions** to address weaknesses.
    3.  **Comprehensive & Rational Adjustments:**
        -   When fulfilling a request (e.g., canceling a session), intelligently reschedule the missed content. Do not simply delete it.
        -   Distribute workload logically. Avoid overloading any single day.
        -   **Crucially, do not delete or overwrite existing, unrelated activities** (like "起床和早餐", "睡觉"). Integrate your changes.
    4.  **Precise Time Management:**
        -   Every schedule item **must** have a `start_time` and `end_time`.
        -   Calculate reasonable durations (45-90 minutes for study).
    5.  **Clear Communication:**
        -   In the `suggestion` text, explain *why* you made the changes, referencing the user's request or performance data.
    6.  **Activity Formatting Rules (ABSOLUTE):**
        -   The schedule item is an object with a separate `"details"` field.
        -   The `"activity"` string MUST ONLY contain `Category-SubCategory`.
        -   The `"details"` string contains descriptive text (e.g., `昨日言语错题`). Use `""` if no details.
        -   **Part 1: Category (in `activity`):** Must be one of: `["深度复盘", "言语理解与表达", "数量关系", "判断推理", "资料分析", "常识判断", "早饭", "午饭", "午休", "晚饭", "睡觉", "模拟测试", "特殊", "上课"]`.
        -   **Part 2: SubCategory (in `activity`):** If it exists, must be one of: `["复盘", "言语", "数量", "判断", "资料", "常识", "早饭", "午饭", "午休", "晚饭", "睡觉", "模考", "特殊", "上课"]`.
        -   **No Fabrication:** Do not invent details. If not specified, use a generic term like "练习" in the `"details"` field. Example: `{{"activity": "言语理解-言语", "details": "练习"}}`.

    **Examples of CORRECT Formatting:**
    - `{{"activity": "模拟测试-模考", "details": "xx试卷"}}`
    - `{{"activity": "资料分析-资料", "details": "增长率计算"}}`
    - `{{"activity": "特殊-特殊", "details": "去医院"}}`

    **Additional Logic Rules:**
    -   **深度复盘有效性规则 (Deep Review Validity Rule):** “深度复盘”活动必须有效。有效性定义为：从当天计划开始到第一个“深度复盘”之间，或两个“深度复盘”活动之间，必须至少存在一个训练类型的活动（例如“言语理解与表达”、“资料分析”等）。如果计划中的某个“深度复盘”不满足此条件（例如，一天开始的第一个活动就是“深度复盘”，或两个“深度复盘”紧挨着），你必须：1. 将这个无效的“深度复盘”活动的`activity`改为`"特殊-特殊"`。 2. 将其`details`字段设置为`"无效的深度复盘：缺少复盘所需的训练内容"`。 3. 在`suggestion`文本中明确解释为什么进行了此项修改。
    -   **Consolidate Trivial Breaks:** Merge small breaks into larger rest blocks like `"午休"`.
    -   **Strict "Special" Category Usage:** Use `"特殊-特殊"` only for user-specified events (e.g., "I have a doctor's appointment"), or for flagging an invalid "深度复盘" as per the rule above. Do not invent other "special" activities.

    **Weekend Constraint:**
    - `"模拟测试"` can ONLY be scheduled on a Saturday or Sunday.

    **Output Format (CRITICAL UPDATE):**
    Your final output must be a single, valid JSON object with THREE keys:
    1.  `"suggestion"`: A multi-line string summarizing all changes and the reasoning.
    2.  `"updated_schedules"`: An object where each key is a date (YYYY-MM-DD). The value is a **complete, new array of schedule items** for that day, sorted by start time. Each item must have FOUR keys: `"start_time"`, `"end_time"`, `"activity"`, and `"details"`.
    3.  `"updated_goals"`: An object where each key is a date (YYYY-MM-DD). The value is a **complete, new array of goal items** for that day. This key should ONLY be included if the user's request led to a change in any goal's `target_questions`, `target_accuracy`, or `target_time_minutes`.

    Example of a correct schedule item:
    `{{{{ "start_time": "14:00", "end_time": "15:30", "activity": "资料分析-资料", "details": "增长率计算" }}}}`

    Example of a correct goal item:
    `{{{{ "type": "资料分析", "target_questions": 40, "target_accuracy": 75, "target_time_minutes": 60 }}}}`

    Wrap the entire JSON output in a single ```json ... ``` code block.
    """

    base_url = api_url.rstrip('/')
    if '/v1' in base_url:
        base_url = base_url.split('/v1')[0]
    api_endpoint = f"{base_url}/v1beta/models/{model}:generateContent?key={api_key}"

    headers = {"Content-Type": "application/json"}
    payload = {
      "contents": [{"parts": [{"text": prompt}]}],
      "generationConfig": {"temperature": 0.1}
    }

    retries = 3
    last_exception = None

    for attempt in range(retries):
        try:
            proxies = {"http": None, "https": None}
            response = requests.post(api_endpoint, headers=headers, json=payload, timeout=600, proxies=proxies)
            response.raise_for_status()
            break  # Success, exit the loop
        except requests.exceptions.RequestException as e:
            last_exception = e
            logging.warning(f"AI API call failed on attempt {attempt + 1}/{retries}. Error: {e}.")
            if attempt < retries - 1:
                logging.info("Retrying in 5 seconds...")
                time.sleep(5)
    else:
        # This block executes if the loop completes without a `break` (i.e., all retries failed).
        logging.error(f"AI API call failed after {retries} attempts.")
        if is_automated:
            raise last_exception  # Re-raise the last exception to trigger RQ retry
        error_details = last_exception.response.text if last_exception.response is not None else "No response from server."
        return {"error": f"AI API call failed after retries: {last_exception}", "details": error_details}

    # If we're here, `response` is a successful response object. Now, parse it.
    try:
        response_json = response.json()
        full_text = response_json['candidates'][0]['content']['parts'][0]['text']
        logging.info(f"Raw AI response for schedule adjustment:\n---\n{full_text}\n---")
        
        # Enhanced JSON extraction logic
        json_string = None
        if '```json' in full_text:
            json_start = full_text.find('```json') + 7
            json_end = full_text.rfind('```')
            if json_start != -1 and json_end != -1:
                json_string = full_text[json_start:json_end].strip()
        else:
            # Fallback for responses without markdown formatting
            json_start = full_text.find('{')
            json_end = full_text.rfind('}')
            if json_start != -1 and json_end != -1:
                json_string = full_text[json_start : json_end + 1].strip()

        if json_string:
            result = json.loads(json_string)
            if 'suggestion' not in result:
                result['suggestion'] = "我已经根据您的要求更新了计划。"
            if 'updated_schedules' in result and isinstance(result['updated_schedules'], dict):
                for date, schedule_items in result['updated_schedules'].items():
                    if isinstance(schedule_items, list):
                        schedule_items.sort(key=lambda x: x.get('start_time', '')) # Corrected key to 'start_time'
            
            # Validation for automated tasks
            if is_automated and 'updated_schedules' not in result:
                raise ValueError("Validation failed: Daily plan generation did not return 'updated_schedules'. Retrying.")

            return result
        else:
            # If the response is not valid JSON, and it's an automated task, raise to retry
            if is_automated:
                raise ValueError(f"Validation failed: AI response was not valid JSON. Details: {full_text}")
            return {"error": "Could not find a valid JSON block in the AI response.", "details": full_text}
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        if is_automated:
            raise  # Re-raise the exception to trigger RQ retry
        error_details = response.text if 'response' in locals() else 'No response'
        return {"error": f"Failed to parse AI response: {e}", "details": error_details}

def run_comprehensive_analysis(task_details, is_automated=False):
    """
    Handles the new comprehensive dashboard analysis by fetching data and calling the AI directly.
    If is_automated is True, it will validate the output length.
    """
    plan_date = task_details.get('plan_date')
    if not plan_date:
        raise ValueError("plan_date is missing for comprehensive analysis.")

    # Step 1: Prepare the data
    session = CentralSession()
    try:
        plan = session.query(StudyPlan).filter_by(plan_date=plan_date).first()
    finally:
        session.close()

    if not plan:
        return {"error": "No plan found for the selected date", "details": f"Date: {plan_date}"}

    history_list = get_recent_history(days=7)
    plan_dict = plan.to_dict()

    # Step 2: Perform AI analysis (logic moved from tasks.py)
    config = get_ai_config()
    api_url, api_key, model = config.get('api_url'), config.get('api_key'), config.get('model')

    if not all([api_url, api_key, model]):
        return {"error": "AI configuration is incomplete."}

    prompt = f"""
    You are a professional civil service exam coach.
    Based on the user's study plan (which includes target questions, accuracy, and time in minutes) and their historical performance data, provide a comprehensive analysis and suggest an adjusted plan for today.

    **CRITICAL RULE: The entire response, especially the 'analysis_text', MUST be in Chinese.**

    **Today's Plan:**
    {json.dumps(plan_dict, indent=2, ensure_ascii=False)}

    **Historical Performance Data:**
    {json.dumps(history_list, indent=2, ensure_ascii=False)}

    Your task is to return a JSON object with one key: "analysis_text".

    **CRITICAL INSTRUCTIONS:**
    1.  **DO NOT MODIFY THE PLAN:** You are strictly a coach providing advice. You MUST NOT output a new or adjusted plan object. Your role is to analyze and suggest, not to implement.
    2.  **PROVIDE SUGGESTIONS IN TEXT:** Within the "analysis_text", you must provide actionable suggestions on how the user could adjust their plan (e.g., "建议将'资料分析'的练习时间增加15分钟，因为近期正确率有所下降。").
    3.  **SINGLE JSON KEY:** The final JSON output MUST only contain the "analysis_text" key.

    The "analysis_text" should be a detailed, encouraging, and insightful analysis of the user's performance. Consider trends in accuracy, speed (average time per question), and time management (total time vs. target time). Identify strengths, weaknesses, and provide clear, actionable advice for plan adjustments.
    """

    json_schema = {
        "type": "OBJECT",
        "properties": {"analysis_text": {"type": "STRING"}},
        "required": ["analysis_text"]
    }

    base_url = api_url.rstrip('/')
    if '/v1' in base_url:
        base_url = base_url.split('/v1')[0]
    api_endpoint = f"{base_url}/v1beta/models/{model}:generateContent?key={api_key}"

    headers = {"Content-Type": "application/json"}
    payload = {
      "contents": [{"parts": [{"text": prompt}]}],
      "generationConfig": {
        "response_mime_type": "application/json",
        "response_schema": json_schema,
        "temperature": 0.5
      }
    }

    try:
        proxies = {"http": None, "https": None}
        response = requests.post(api_endpoint, headers=headers, json=payload, timeout=600, proxies=proxies)
        response.raise_for_status()
        response_json = response.json()
        full_text = response_json['candidates'][0]['content']['parts'][0]['text']
        json_start = full_text.find('{')
        json_end = full_text.rfind('}')
        if json_start != -1 and json_end != -1:
            json_string = full_text[json_start : json_end + 1].strip()
            analysis_result = json.loads(json_string)
        else:
            analysis_result = {"error": "Could not find a valid JSON block in the AI response.", "details": full_text}
    except requests.exceptions.RequestException as e:
        error_details = e.response.text if e.response is not None else "No response from server."
        analysis_result = {"error": f"AI API call failed: {e}", "details": error_details}
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        error_details = response.text if 'response' in locals() else 'No response'
        analysis_result = {"error": f"Failed to parse AI response: {e}", "details": error_details}

    # Step 3: Validate and Send Notification
    if "analysis_text" in analysis_result:
        # Validation for automated tasks
        if is_automated and len(analysis_result["analysis_text"]) < 400:
            raise ValueError(f"Validation failed: Comprehensive analysis report is too short ({len(analysis_result['analysis_text'])} chars). Retrying.")
        
        # Send notification
        try:
            full_report_markdown = f"### AI 专项分析报告 ({plan_date})\n\n{analysis_result['analysis_text']}"
            send_wechat_message(full_report_markdown)
        except Exception as notify_e:
            logging.error(f"Failed to send comprehensive analysis WeChat notification: {notify_e}", exc_info=True)
    else:
        # If there's an error, it should be propagated to trigger a retry for automated tasks
        if is_automated:
            error_detail = analysis_result.get('details', 'Unknown error')
            raise ValueError(f"Task failed before validation: {error_detail}")
        
        # For manual tasks, just send the error message
        try:
            error_message = f"### AI 专项分析报告 ({plan_date})\n\n分析失败: {analysis_result.get('details', '无')}"
            send_wechat_message(error_message)
        except Exception as notify_e:
            logging.error(f"Failed to send comprehensive analysis failure notification: {notify_e}", exc_info=True)

    return analysis_result


def run_save_schedule_and_notify(task_details):
    """
    Handles saving the schedule to the DB and sending a notification.
    This entire function runs in the clean subprocess.
    """
    ai_result = task_details.get("ai_result")
    if not ai_result:
        raise ValueError("Missing 'ai_result' for saving schedule.")
    if "error" in ai_result:
        return ai_result

    try:
        # Pre-Save Validation for Mock Exams
        updated_schedules_from_ai = ai_result.get('updated_schedules', {})
        for date_str, new_items in updated_schedules_from_ai.items():
            date_obj = datetime.strptime(date_str, '%Y-%m-%d')
            is_weekend = date_obj.weekday() >= 5
            if not is_weekend:
                for item in new_items:
                    if PracticeCategory.MOCK_EXAM.value in item.get('activity', ''):
                        return {"error": f"AI 尝试在非周末 ({date_str}) 安排 '{PracticeCategory.MOCK_EXAM.value}'。请调整您的指令。"}

        # Process the multi-day response and update the database
        session = CentralSession()
        try:
            # --- 1. Update Schedules and Synchronize Plans ---
            updated_schedules = ai_result.get('updated_schedules', {})
            for date_str, new_items in updated_schedules.items():
                schedule = session.query(DailySchedule).filter_by(schedule_date=date_str).first()
                if schedule:
                    schedule.schedule_items = new_items
                else:
                    new_schedule = DailySchedule(schedule_date=date_str, schedule_items=new_items)
                    session.add(new_schedule)
                # This function creates or updates the plan based on the schedule
                _synchronize_plan_from_schedule(session, date_str, new_items)

            # --- 2. Explicitly Update Goals if Provided by AI ---
            updated_goals_from_ai = ai_result.get('updated_goals', {})
            for date_str, new_goals in updated_goals_from_ai.items():
                plan = session.query(StudyPlan).filter_by(plan_date=date_str).first()
                if plan:
                    # Overwrite the entire goals list with the AI's new version
                    plan.goals = new_goals
                else:
                    # If for some reason a plan doesn't exist, create one.
                    # This is a fallback, as _synchronize_plan_from_schedule should have created it.
                    new_plan = StudyPlan(plan_date=date_str, goals=new_goals)
                    session.add(new_plan)

            session.commit()
        except SQLAlchemyError as e:
            session.rollback()
            return {"error": f"Database error during schedule adjustment: {e}"}
        finally:
            session.close()

        # --- Send WeChat Notification after successful save ---
        try:
            suggestion_text = ai_result.get('suggestion', '计划已调整，请在系统中查看。')
            # 移除不支持的 ">" 引用语法，并优化格式
            message = f"""### AI 学习计划调整通知
**状态**: <font color="info">已更新</font>
**AI 建议**: {suggestion_text}

您的学习计划已根据您的请求和近期表现自动调整。"""
            send_wechat_message(message)
        except Exception as notify_e:
            logging.error(f"Failed to send schedule adjustment WeChat notification: {notify_e}", exc_info=True)

        # FIX: Ensure we always return a dictionary to prevent the frontend
        # from receiving `null` and causing a TypeError.
        return ai_result or {}

    except ValueError as e:
        return {"error": f"AI返回了无效的日期格式: {e}"}
    except Exception as e:
        return {"error": f"An unexpected error occurred while saving the schedule: {e}"}


def run_prompt_generation(task_details):
    """
    Handles file content extraction, AI prompt generation, and saving the prompt
    for a single category (course_schedule or exam_requirements).
    """
    upload_type = task_details.get('upload_type')
    files = task_details.get('files', [])
    additional_info = task_details.get('additional_info', '')

    if not upload_type or upload_type not in ['course_schedule', 'exam_requirements']:
        raise ValueError("Invalid or missing 'upload_type' in task details.")

    target_dir = os.path.join('user_data', upload_type)
    prompt_template = ""
    if upload_type == 'course_schedule':
        prompt_template = "根据以下课程表和补充信息，生成一个学习计划的prompt:\n课程表内容:\n{content}\n补充信息:\n{info}"
    else: # exam_requirements
        prompt_template = "根据以下考试要求和补充信息，生成一个备考计划的prompt:\n考试要求内容:\n{content}\n补充信息:\n{info}"

    try:
        # --- FIX: Copy uploaded files to the target directory before processing ---
        os.makedirs(target_dir, exist_ok=True)
        final_file_paths = []
        for temp_file_path in files:
            if os.path.exists(temp_file_path):
                filename = os.path.basename(temp_file_path)
                # Remove the temporary prefix like "course_schedule_"
                if filename.startswith(upload_type + '_'):
                    filename = filename[len(upload_type) + 1:]
                
                final_dest_path = os.path.join(target_dir, filename)
                
                # shutil.move is more robust for moving files
                import shutil
                shutil.move(temp_file_path, final_dest_path)
                final_file_paths.append(final_dest_path)

        # Now, extract content from the *new* file locations
        contents = []
        for f in final_file_paths:
            extracted_text = extract_text_from_file(f)
            # --- ROBUSTNESS FIX: Check for file read errors immediately ---
            if extracted_text.startswith("[Could not read file"):
                error_message = f"无法读取文件 '{os.path.basename(f)}'。请确保文件未损坏且格式正确。\n\n详细错误: {extracted_text}"
                # Write the error to the output file and stop processing.
                with open(os.path.join(target_dir, 'generated_prompt.txt'), 'w', encoding='utf-8') as err_f:
                    err_f.write(error_message)
                # Return a failure message to the task queue.
                return {"error": "File reading failed.", "details": error_message}
            contents.append(extracted_text)
        
        content = "\n".join(contents)
        
        # Only proceed if content was actually extracted
        if not content.strip():
             with open(os.path.join(target_dir, 'generated_prompt.txt'), 'w', encoding='utf-8') as err_f:
                    err_f.write("未能从上传的文件中提取任何文本内容。请检查文件是否为空。")
             return {"error": "No content extracted from files."}

        final_prompt_text = prompt_template.format(content=content, info=additional_info)
        ai_result = analyze_text_direct_non_stream(final_prompt_text)

        # --- SIMPLIFIED FIX ---
        # Since analyze_text_direct_non_stream is guaranteed to return a JSON object
        # with a "generated_prompt" key, we can access it directly.
        generated_prompt_str = ""
        if ai_result and "generated_prompt" in ai_result:
            generated_prompt_str = ai_result["generated_prompt"]
        elif ai_result and "error" in ai_result:
            # If there was an error, write the error details to the file for debugging.
            generated_prompt_str = f"AI分析失败: {ai_result.get('details', '未知错误')}"
        else:
            # Fallback for unexpected response structures.
            generated_prompt_str = f"收到了意外的AI响应格式: {ai_result}"


        # The target directory is already created
        with open(os.path.join(target_dir, 'generated_prompt.txt'), 'w', encoding='utf-8') as f:
            f.write(generated_prompt_str)
        
        # The temporary files are already moved, so no need to delete them.
        # The original 'files' list contains paths to non-existent temp files now.

        return {"message": f"{upload_type.replace('_', ' ').title()} 的提示已成功生成并保存。"}

    except Exception as e:
        # Clean up any remaining temporary files if an error occurs
        for f in files:
            if os.path.exists(f):
                os.remove(f)
        raise e


def main():
    """
    Executes a task in an isolated process based on the task_type.
    """
    logging.info(f"Task runner started with args: {sys.argv}")
    if len(sys.argv) != 2:
        logging.error("Usage: python task_runner.py <task_json_string>")
        print(json.dumps({"error": "Usage: python task_runner.py <task_json_string>"}), file=sys.stderr)
        sys.exit(1)

    try:
        task_details = json.loads(sys.argv[1])
        logging.info(f"Received task: {task_details.get('task_type', 'unknown')}")
    except json.JSONDecodeError:
        logging.error("Invalid JSON string provided.", exc_info=True)
        print(json.dumps({"error": "Invalid JSON string provided."}), file=sys.stderr)
        sys.exit(1)

    # Determine the task type, default to 'file' for backward compatibility
    task_type = task_details.get('task_type', 'file')

    try:
        result = None
        if task_type == 'file':
            result = run_file_analysis(task_details)
        elif task_type == 'comprehensive_analysis':
            result = run_comprehensive_analysis(task_details)
        elif task_type == 'dashboard_analysis':
            result = run_dashboard_analysis(task_details)
        elif task_type == 'schedule_adjustment':
            result = run_schedule_adjustment(task_details)
        elif task_type == 'save_schedule_and_notify':
            result = run_save_schedule_and_notify(task_details)
        elif task_type == 'prompt_generation':
            result = run_prompt_generation(task_details)
        elif task_type == 'automated_comprehensive_analysis':
            result = run_comprehensive_analysis(task_details, is_automated=True)
        elif task_type == 'automated_data_analysis':
            result = run_dashboard_analysis(task_details, is_automated=True)
        elif task_type == 'automated_daily_plan':
            # Step 1: Get the adjusted schedule from AI
            logging.info("Running automated daily plan: Step 1 - Adjusting schedule with AI.")
            ai_result = run_schedule_adjustment(task_details, is_automated=True)
            
            # Step 2: If AI adjustment is successful, save the result and notify
            if ai_result and "error" not in ai_result:
                logging.info("Running automated daily plan: Step 2 - Saving schedule and notifying.")
                save_task_details = {"ai_result": ai_result}
                result = run_save_schedule_and_notify(save_task_details)
            else:
                # If there was an error in step 1, propagate it
                logging.error(f"Automated daily plan failed at AI adjustment step: {ai_result}")
                result = ai_result
        else:
            raise ValueError(f"Unknown task_type: {task_type}")
        
        logging.info(f"Task {task_type} completed successfully.")
        # Print the final result to stdout for the parent process
        print(json.dumps(result))

    except Exception as e:
        # Catch any unexpected errors and report them
        logging.error(f"An error occurred in the task runner for task type {task_type}.", exc_info=True)
        print(json.dumps({"error": "An error occurred in the task runner.", "details": str(e)}), file=sys.stderr)
        sys.exit(1)

if __name__ == '__main__':
    main()
