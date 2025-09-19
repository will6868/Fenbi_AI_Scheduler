import requests
import json
import base64
import mimetypes
from PIL import Image
import io
import os

CONFIG_FILE = 'config.json'

def get_ai_config():
    """Loads AI configuration from the JSON file."""
    try:
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"api_url": "", "api_key": "", "model": ""}

def save_ai_config(data):
    """Saves AI configuration to the JSON file."""
    with open(CONFIG_FILE, 'w') as f:
        json.dump(data, f, indent=4)

def test_ai_connection_stream():
    """Tests the AI connection based on the new API documentation."""
    config = get_ai_config()
    api_url, api_key, model = config.get('api_url'), config.get('api_key'), config.get('model')

    if not all([api_url, api_key, model]):
        yield "错误: AI 配置不完整。"
        return

    # Per documentation, the user-provided URL is the base.
    # e.g., https://new-api.zhaozuohong.vip/v1
    base_url = api_url.rstrip('/')
    # Per user feedback, the model name is a single unit and should not be split.
    # The correct endpoint format is /v1beta/models/{model_name}:action
    # We will construct this from the base URL.
    base_url = api_url.rstrip('/')
    if '/v1' in base_url:
        base_url = base_url.split('/v1')[0] # Get the true base URL
        
    stream_url = f"{base_url}/v1beta/models/{model}:streamGenerateContent?alt=sse&key={api_key}"

    headers = {"Content-Type": "application/json"}
    payload = {"contents": [{"parts":[{"text": "你好！请用中文确认你已准备就绪。"}]}]}

    try:
        proxies = {"http": None, "https": None}
        with requests.post(stream_url, headers=headers, json=payload, stream=True, proxies=proxies) as response:
            response.raise_for_status()
            for chunk in response.iter_content(chunk_size=None):
                yield chunk
    except requests.exceptions.RequestException as e:
        yield f"连接 AI 接口时出错: {e}\nResponse: {e.response.text if e.response else 'N/A'}"

def analyze_pdf_direct_non_stream(pdf_path, goal=None):
    """
    Analyzes a PDF file directly by sending its base64 content to the AI,
    and returns a structured JSON object.
    Includes a completion score if a goal is provided.
    """
    filename = os.path.basename(pdf_path)
    config = get_ai_config()
    api_url, api_key, model = config.get('api_url'), config.get('api_key'), config.get('model')

    if not all([api_url, api_key, model]):
        return {"error": "AI configuration is incomplete."}

    try:
        with open(pdf_path, "rb") as f:
            pdf_bytes = f.read()
        pdf_b64 = base64.b64encode(pdf_bytes).decode('utf-8')
    except FileNotFoundError:
        return {"error": f"PDF file not found at {pdf_path}"}

    goal_prompt_section = ""
    if goal:
        goal_prompt_section = f"""
        **User's Training Goal:**
        - Target Questions: {goal.get('target_questions', 'N/A')}
        - Target Accuracy: {goal.get('target_accuracy', 'N/A')}%
        - Target Time (minutes): {goal.get('target_time_minutes', 'N/A')}

        **Mandatory Task: Completion Score Calculation**
        You MUST add a new field called "completion_score" to the root of the JSON output. This is an integer from 0 to 100 representing the quality of this training session.
        Calculate this score based on the following STRICT criteria:
        1.  **Base Score (70 points max):**
            -   Accuracy Component (40 points): (Actual Accuracy / Target Accuracy) * 40. Cap at 40.
            -   Volume Component (30 points): (Actual Questions Answered / Target Questions) * 30. Cap at 30.
        2.  **Efficiency Score (30 points max):**
            -   Start with 30 points.
            -   Time Penalty: If Total Time > Target Time, subtract ( (Total Time - Target Time) / Target Time ) * 20 points.
            -   "Fishing" Penalty: If the average time per question is unreasonably long (e.g., >5 minutes for a simple question), subtract 10-20 points based on severity.
            -   Efficiency Bonus: If Total Time is < 80% of Target Time with accuracy >= Target Accuracy, add 10 points.
        3.  **Final Score:** Sum of Base Score and Efficiency Score. Round to the nearest integer. The final score cannot exceed 100.
        """

    prompt = f"""
    You are a strict and meticulous civil service exam training analyst.
    Analyze the provided document, which contains a user's completed exercise.
    Your task is to extract all key metrics and structure them into the specified JSON format.
    The output MUST be a single JSON object adhering to the provided schema.
    The filename of the submitted file is '{filename}'. You must include this in the JSON output.

    **CRITICAL INSTRUCTION: The field "answer_details" is ABSOLUTELY FORBIDDEN.**
    Do NOT include the "answer_details" field in your response under any circumstances.
    The downstream processing system does not expect it, and its inclusion will cause a critical failure.
    Your primary directive is to adhere strictly to the schema provided below, which intentionally omits "answer_details".

    **JSON Output Structure:**
    Your output MUST follow this exact nested structure. Do not add any other fields.

    {{
      "report_metadata": {{
        "filename": "{filename}",
        "timestamp": "...",
        "difficulty": ...
      }},
      "performance_summary": {{
        "total_questions": ...,
        "questions_answered": ...,
        "correct_answers": ...,
        "incorrect_answers": ...,
        "unanswered_questions": ...,
        "total_time_minutes": ...,
        "accuracy_percentage": ...
      }},
      "completion_score": ...
    }}

    {goal_prompt_section}
    """
    
    # Define the new, detailed, and nested JSON schema
    json_schema = {
        "type": "OBJECT",
        "properties": {
            "report_metadata": {
                "type": "OBJECT",
                "properties": {
                    "filename": {"type": "STRING"},
                    "timestamp": {"type": "STRING"},
                    "difficulty": {"type": "NUMBER"}
                },
                "required": ["filename", "timestamp", "difficulty"]
            },
            "performance_summary": {
                "type": "OBJECT",
                "properties": {
                    "total_questions": {"type": "INTEGER"},
                    "questions_answered": {"type": "INTEGER"},
                    "correct_answers": {"type": "INTEGER"},
                    "incorrect_answers": {"type": "INTEGER"},
                    "unanswered_questions": {"type": "INTEGER"},
                    "total_time_minutes": {"type": "INTEGER"},
                    "accuracy_percentage": {"type": "INTEGER", "description": "An integer from 0 to 100"}
                },
                "required": [
                    "total_questions",
                    "questions_answered",
                    "correct_answers",
                    "incorrect_answers",
                    "unanswered_questions",
                    "total_time_minutes",
                    "accuracy_percentage"
                ]
            },
            "completion_score": {"type": "INTEGER"}
        },
        "required": ["report_metadata", "performance_summary", "completion_score"]
    }

    # Correct URL construction based on documentation and user feedback
    base_url = api_url.rstrip('/')
    if '/v1' in base_url:
        base_url = base_url.split('/v1')[0] # Get the true base URL
        
    api_endpoint = f"{base_url}/v1beta/models/{model}:generateContent?key={api_key}"

    headers = {"Content-Type": "application/json"}
    payload = {
      "contents": [{
        "parts":[
          {"text": prompt},
          {"inline_data": {"mime_type": "application/pdf", "data": pdf_b64}}
        ]
      }],
      "generationConfig": {
        "response_mime_type": "application/json",
        "response_schema": json_schema,
        "temperature": 0.0
      }
    }

    try:
        # Add a 600-second timeout to handle large PDF uploads
        proxies = {"http": None, "https": None}
        response = requests.post(api_endpoint, headers=headers, json=payload, timeout=600, proxies=proxies)
        response.raise_for_status()
        response_json = response.json()
        
        # Robust JSON extraction
        full_text = response_json['candidates'][0]['content']['parts'][0]['text']
        json_start = full_text.find('{')
        json_end = full_text.rfind('}')
        
        if json_start != -1 and json_end != -1:
            json_string = full_text[json_start : json_end + 1].strip()
            return json.loads(json_string)
        else:
            # Fallback for markdown-style code blocks
            json_start_md = full_text.find('```json')
            if json_start_md != -1:
                json_string = full_text[json_start_md + 7:]
                json_end_md = json_string.rfind('```')
                if json_end_md != -1:
                    json_string = json_string[:json_end_md].strip()
                    return json.loads(json_string)
            
            return {"error": "Could not find a valid JSON block in the AI response.", "details": full_text}
    except requests.exceptions.RequestException as e:
        error_details = "No response from server."
        if e.response is not None:
            error_details = e.response.text
        print(f"AI API Request failed: {e}")
        print(f"Response body: {error_details}")
        return {
            "error": f"AI API call or JSON parsing failed: {e}",
            "details": error_details
        }
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        error_details = response.text if 'response' in locals() else 'No response'
        return {
            "error": f"AI API call or JSON parsing failed: {e}",
            "details": error_details,
            "request_prompt": prompt,
            "request_payload": payload
        }

def analyze_text_direct_non_stream(text_content):
    """
    Analyzes a raw text string using the AI and returns a structured JSON object.
    This is used for tasks like generating prompts from extracted file content.
    The incoming `text_content` is assumed to be the full, final prompt.
    """
    config = get_ai_config()
    api_url, api_key, model = config.get('api_url'), config.get('api_key'), config.get('model')

    if not all([api_url, api_key, model]):
        return {"error": "AI configuration is incomplete."}

    # This function now expects the caller to have already constructed the full prompt.
    # It simply sends this prompt to the AI and requests a JSON response.
    json_schema = {
        "type": "OBJECT",
        "properties": {
            "generated_prompt": {"type": "STRING"}
        },
        "required": ["generated_prompt"]
    }

    base_url = api_url.rstrip('/')
    if '/v1' in base_url:
        base_url = base_url.split('/v1')[0]
    api_endpoint = f"{base_url}/v1beta/models/{model}:generateContent?key={api_key}"

    headers = {"Content-Type": "application/json"}
    payload = {
      "contents": [{"parts": [{"text": text_content}]}],
      "generationConfig": {
        "response_mime_type": "application/json",
        "response_schema": json_schema,
        "temperature": 0.2
      }
    }

    try:
        proxies = {"http": None, "https": None}
        response = requests.post(api_endpoint, headers=headers, json=payload, timeout=300, proxies=proxies)
        response.raise_for_status()
        response_json = response.json()

        full_text = response_json['candidates'][0]['content']['parts'][0]['text']
        json_start = full_text.find('{')
        json_end = full_text.rfind('}')
        
        if json_start != -1 and json_end != -1:
            json_string = full_text[json_start : json_end + 1].strip()
            return json.loads(json_string)
        else:
            return {"error": "Could not find a valid JSON block in the AI response.", "details": full_text}
            
    except requests.exceptions.RequestException as e:
        error_details = "No response from server."
        if e.response is not None:
            error_details = e.response.text
        return {"error": f"AI API call failed: {e}", "details": error_details}
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        error_details = response.text if 'response' in locals() else 'No response'
        return {"error": f"Failed to parse AI response: {e}", "details": error_details}


def analyze_image_with_ai(image_path, goal=None):
    """
    Analyzes an image using the AI model and returns a structured JSON object with detailed metrics.
    Includes a completion score if a goal is provided.
    """
    filename = os.path.basename(image_path)
    config = get_ai_config()
    api_url, api_key, model = config.get('api_url'), config.get('api_key'), config.get('model')

    if not all([api_url, api_key, model]):
        return {"error": "AI configuration is incomplete."}

    try:
        with open(image_path, "rb") as f:
            image_bytes = f.read()
        image_b64 = base64.b64encode(image_bytes).decode('utf-8')
        mime_type = mimetypes.guess_type(image_path)[0]
        if not mime_type:
            return {"error": f"Could not determine MIME type for {image_path}"}
    except FileNotFoundError:
        return {"error": f"Image file not found at {image_path}"}

    goal_prompt_section = ""
    if goal:
        goal_prompt_section = f"""
        **User's Training Goal:**
        - Target Questions: {goal.get('target_questions', 'N/A')}
        - Target Accuracy: {goal.get('target_accuracy', 'N/A')}%
        - Target Time (minutes): {goal.get('target_time_minutes', 'N/A')}

        **Mandatory Task: Completion Score Calculation**
        You MUST add a new field called "completion_score" to the root of the JSON output. This is an integer from 0 to 100 representing the quality of this training session.
        Calculate this score based on the following STRICT criteria:
        1.  **Base Score (70 points max):**
            -   Accuracy Component (40 points): (Actual Accuracy / Target Accuracy) * 40. Cap at 40.
            -   Volume Component (30 points): (Actual Questions Answered / Target Questions) * 30. Cap at 30.
        2.  **Efficiency Score (30 points max):**
            -   Start with 30 points.
            -   Time Penalty: If Total Time > Target Time, subtract ( (Total Time - Target Time) / Target Time ) * 20 points.
            -   "Fishing" Penalty: If the average time per question is unreasonably long (e.g., >5 minutes for a simple question), subtract 10-20 points based on severity.
            -   Efficiency Bonus: If Total Time is < 80% of Target Time with accuracy >= Target Accuracy, add 10 points.
        3.  **Final Score:** Sum of Base Score and Efficiency Score. Round to the nearest integer. The final score cannot exceed 100.
        """

    prompt = f"""
    You are a hyper-vigilant data extraction bot. Your ONLY function is to read the text from the provided image and populate the JSON schema. You are strictly forbidden from inventing, guessing, or estimating any value.
    The filename of the submitted file is '{filename}'. You must include this in the JSON output.

    **ABSOLUTE RULES OF EXTRACTION:**

    1.  **NO FABRICATION:** You must not invent any data. Every single value you provide in the JSON output must be explicitly visible in the image. If a value is not present in the image, you must raise an error or indicate its absence in a designated way, but you must not create data.

    2.  **DATA COMPLETENESS:** All data points required by the JSON schema must be present in the uploaded image. Before generating the JSON, verify that you can find every required field in the image.

    3.  **DATA INTEGRITY & CROSS-VALIDATION:** The numerical data in the image is internally consistent. You MUST use this fact to validate your extractions. For example, the following relationships must hold true:
        - `questions_answered` MUST equal `correct_answers` + `incorrect_answers`.
        - `total_questions` MUST equal `questions_answered` + `unanswered_questions`.
        If your extracted numbers do not satisfy these checks, you MUST re-examine the image and correct your values until they do. Do not output the JSON until the numbers are consistent.

    4.  **MANDATORY EXTRACTION OF KEY FIELDS:** You have four critical fields to extract.
        -   **`filename`**: This is the filename of the image being processed. Its value is `{filename}`. You MUST place this value inside the `report_metadata` object, mapping it to the `filename` field.
        -   **`total_time_minutes`:** Find the Chinese label "总用时". The value next to it (e.g., "25分钟") is the total time. Extract the number and map it to the `total_time_minutes` field in the `performance_summary` object.
        -   **`timestamp`:** Find the Chinese label "交卷时间". Extract the full timestamp text that follows it and map it to the `timestamp` field.
        -   **`difficulty`:** Find the Chinese label "难度：". This value may be in the **top-right corner** of one of the images. Extract the numerical value that follows the label. **Crucially, you MUST place this value inside the `report_metadata` object, mapping it to the `difficulty` field.** For example, if you see "难度：0.65", the final JSON should look like `{{"report_metadata": {{"difficulty": 0.65, ...}}, ...}}`.

    5.  **STRICT SCHEMA ADHERENCE:** You must only populate the fields defined in the provided JSON schema. Do not look for or require fields that are not in the schema, such as `time_spent`. The correct field for time is `total_time_minutes`.

    6.  **EXCEPTION FOR MISSING `timestamp`:** If, and ONLY IF, the `timestamp` value is COMPLETELY ABSENT from the image after a thorough search, you MUST use the exact placeholder string `"USE_CURRENT_TIME"` for this field.

    7.  **EXCEPTION FOR MISSING `difficulty`:** If, and ONLY IF, the `difficulty` value is COMPLETELY ABSENT from the image, you MUST use the default numerical value `0.5` for this field.

    8.  **COMPLETE THE SCHEMA:** You MUST return ALL fields required by the schema. No omissions.

    **FINAL CHECKLIST - Before outputting, confirm you have followed these rules:**
    - All data is from the image, no fabricated values.
    - All required JSON fields are present in the image.
    - The numerical values have been cross-validated (e.g., answered = correct + incorrect).
    - `filename`: I have used the provided filename `{filename}`.
    - `timestamp`: Was it on the image? If yes, I extracted it exactly. If NO, I used the string "USE_CURRENT_TIME".
    - `difficulty`: Was it on the image? If yes, I extracted it exactly. If NO, I used the number 0.5.

    **CRITICAL INSTRUCTION: The field "answer_details" is ABSOLUTELY FORBIDDEN.**
    Do NOT include the "answer_details" field in your response under any circumstances.
    The downstream processing system does not expect it, and its inclusion will cause a critical failure.
    Your primary directive is to adhere strictly to the schema provided below, which intentionally omits "answer_details".

    {goal_prompt_section}
    """

    # Define the new, detailed, and nested JSON schema
    json_schema = {
        "type": "OBJECT",
        "properties": {
            "report_metadata": {
                "type": "OBJECT",
                "properties": {
                    "filename": {"type": "STRING"},
                    "practice_type": {
                        "type": "STRING",
                        "enum": [
                            "言语理解与表达",
                            "数量关系",
                            "判断推理",
                            "资料分析",
                            "常识判断"
                        ]
                    },
                    "timestamp": {"type": "STRING", "description": "The time of submission, e.g., '2023-10-27 18:20:00'"},
                    "difficulty": {"type": "NUMBER", "description": "Estimated difficulty from 0.0 to 1.0"}
                },
                "required": ["filename", "practice_type", "timestamp", "difficulty"]
            },
            "performance_summary": {
                "type": "OBJECT",
                "properties": {
                    "total_questions": {"type": "INTEGER"},
                    "questions_answered": {"type": "INTEGER"},
                    "correct_answers": {"type": "INTEGER"},
                    "incorrect_answers": {"type": "INTEGER"},
                    "unanswered_questions": {"type": "INTEGER"},
                    "total_time_minutes": {"type": "INTEGER"}
                },
                "required": ["total_questions", "questions_answered", "correct_answers", "incorrect_answers", "unanswered_questions", "total_time_minutes"]
            },
            "calculated_metrics": {
                "type": "OBJECT",
                "properties": {
                    "accuracy_rate_overall": {"type": "NUMBER", "description": "A float between 0.0 and 1.0"},
                    "accuracy_rate_answered": {"type": "NUMBER", "description": "A float between 0.0 and 1.0"}
                },
                "required": ["accuracy_rate_overall", "accuracy_rate_answered"]
            },
            "completion_score": {"type": "INTEGER"}
        },
        "required": ["report_metadata", "performance_summary", "calculated_metrics"]
    }
    # completion_score is only required if a goal is provided, so it's not in the main required list.
    if goal:
        json_schema["required"].append("completion_score")

    base_url = api_url.rstrip('/')
    if '/v1' in base_url:
        base_url = base_url.split('/v1')[0]
    api_endpoint = f"{base_url}/v1beta/models/{model}:generateContent?key={api_key}"

    headers = {"Content-Type": "application/json"}
    payload = {
      "contents": [{
        "parts":[
          {"text": prompt},
          {"inline_data": {"mime_type": mime_type, "data": image_b64}}
        ]
      }],
      "generationConfig": {
        "response_mime_type": "application/json",
        "response_schema": json_schema,
        "temperature": 0.0
      }
    }

    try:
        proxies = {"http": None, "https": None}
        response = requests.post(api_endpoint, headers=headers, json=payload, timeout=600, proxies=proxies)
        response.raise_for_status()
        response_json = response.json()

        # Robust JSON extraction
        full_text = response_json['candidates'][0]['content']['parts'][0]['text']
        json_start = full_text.find('{')
        json_end = full_text.rfind('}')
        
        if json_start != -1 and json_end != -1:
            json_string = full_text[json_start : json_end + 1].strip()
            return json.loads(json_string)
        else:
            # Fallback for markdown-style code blocks
            json_start_md = full_text.find('```json')
            if json_start_md != -1:
                json_string = full_text[json_start_md + 7:]
                json_end_md = json_string.rfind('```')
                if json_end_md != -1:
                    json_string = json_string[:json_end_md].strip()
                    return json.loads(json_string)

            return {"error": "Could not find a valid JSON block in the AI response.", "details": full_text}
    except requests.exceptions.RequestException as e:
        error_details = "No response from server."
        if e.response is not None:
            error_details = e.response.text
        print(f"AI API Request failed: {e}")
        print(f"Response body: {error_details}")
        return {
            "error": f"AI API call or JSON parsing failed: {e}",
            "details": error_details
        }
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        error_details = response.text if 'response' in locals() else 'No response'
        return {
            "error": f"AI API call or JSON parsing failed: {e}",
            "details": error_details,
            "request_prompt": prompt,
            "request_payload": payload
        }
