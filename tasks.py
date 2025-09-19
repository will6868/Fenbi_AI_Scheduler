import json
import os
import subprocess
import sys
import requests
import markdown
from datetime import datetime
from sqlalchemy.exc import SQLAlchemyError
from ai_analyzer import get_ai_config, analyze_text_direct_non_stream
from database import CentralSession
from models import DailySchedule, PracticeCategory
from services import _synchronize_plan_from_schedule
from rq import get_current_job
import fitz  # PyMuPDF
import pandas as pd
import docx


def run_analysis_in_subprocess(filename, goal, category, date, upload_folder):
    """
    A wrapper function that RQ will execute.
    This function calls an external script in a clean subprocess to perform the analysis,
    avoiding fork-safety issues on macOS.
    """
    try:
        task_details = {
            "filename": filename,
            "goal": goal,
            "category": category,
            "date": date,
            "upload_folder": upload_folder
        }
        task_json_string = json.dumps(task_details)

        # Execute the external script
        # We use sys.executable to ensure we're using the same Python interpreter
        result = subprocess.run(
            [sys.executable, 'task_runner.py', task_json_string],
            capture_output=True,
            text=True,
            check=True  # This will raise CalledProcessError if the script returns a non-zero exit code
        )

        # The script prints the final JSON result to stdout
        analysis_result = json.loads(result.stdout)
        
        return analysis_result

    except subprocess.CalledProcessError as e:
        # If the script fails, stderr will contain the error message
        error_output = e.stderr
        try:
            # Try to parse the error JSON from the script
            error_json = json.loads(error_output)
            return error_json
        except json.JSONDecodeError:
            # If stderr is not JSON, return a generic error
            return {"error": "Task runner script failed with non-JSON output.", "details": error_output}
    except Exception as e:
        # Catch any other errors in this wrapper function
        return {"error": "An unexpected error occurred in the RQ wrapper function.", "details": str(e)}



def prepare_and_run_dashboard_analysis():
    """
    A wrapper function that RQ will execute for the main dashboard analysis.
    This calls the task_runner.py script to perform data aggregation and AI analysis
    in a clean subprocess.
    """
    try:
        task_details = {
            "task_type": "dashboard_analysis",
        }
        task_json_string = json.dumps(task_details)

        # Execute the external script in a completely new and clean process
        result = subprocess.run(
            [sys.executable, 'task_runner.py', task_json_string],
            capture_output=True,
            text=True,
            check=True
        )
        return json.loads(result.stdout)

    except subprocess.CalledProcessError as e:
        error_output = e.stderr
        try:
            return json.loads(error_output)
        except json.JSONDecodeError:
            return {"error": "Task runner script failed with non-JSON output.", "details": error_output}
    except Exception as e:
        return {"error": "An unexpected error occurred in the RQ wrapper function.", "details": str(e)}

def prepare_and_run_schedule_adjustment(combined_data, user_request, start_date_str):
    """
    A wrapper function that RQ will execute.
    This function calls the external task_runner.py script in a clean subprocess
    to perform the schedule adjustment.
    """
    try:
        task_details = {
            "task_type": "schedule_adjustment",
            "combined_data": combined_data,
            "user_request": user_request,
            "start_date_str": start_date_str,
        }
        task_json_string = json.dumps(task_details)

        # Execute the external script in a completely new and clean process
        result = subprocess.run(
            [sys.executable, 'task_runner.py', task_json_string],
            capture_output=True,
            text=True,
            check=True
        )

        # The script prints the final JSON result to stdout
        ai_result = json.loads(result.stdout)
        # Package the result with the start_date_str for the next job
        return {
            "ai_result": ai_result,
            "start_date_str": start_date_str
        }

    except subprocess.CalledProcessError as e:
        # If the script fails, stderr will contain the error message
        error_output = e.stderr
        try:
            error_json = json.loads(error_output)
            return error_json
        except json.JSONDecodeError:
            return {"error": "Task runner script failed with non-JSON output.", "details": error_output}
    except Exception as e:
        return {"error": "An unexpected error occurred in the RQ wrapper function.", "details": str(e)}

def prepare_and_run_comprehensive_analysis(plan_date):
    """
    A wrapper function that RQ will execute.
    This function calls the external task_runner.py script in a clean subprocess
    to perform the comprehensive analysis, avoiding fork-safety issues on macOS.
    """
    try:
        task_details = {
            "task_type": "comprehensive_analysis",
            "plan_date": plan_date,
        }
        task_json_string = json.dumps(task_details)

        # Execute the external script in a completely new and clean process
        result = subprocess.run(
            [sys.executable, 'task_runner.py', task_json_string],
            capture_output=True,
            text=True,
            check=True
        )

        # The script prints the final JSON result to stdout
        analysis_result = json.loads(result.stdout)

        return analysis_result

    except subprocess.CalledProcessError as e:
        # If the script fails, stderr will contain the error message
        error_output = e.stderr
        try:
            error_json = json.loads(error_output)
            return error_json
        except json.JSONDecodeError:
            return {"error": "Task runner script failed with non-JSON output.", "details": error_output}
    except Exception as e:
        return {"error": "An unexpected error occurred in the RQ wrapper function.", "details": str(e)}

def save_adjusted_schedule_to_db():
    """
    This function now gets the result from its dependency and calls the
    task_runner.py script in a clean subprocess to perform the final save and notification.
    """
    import redis
    import os
    from rq import get_current_job
    from rq.job import Job

    redis_conn = redis.from_url(os.getenv('REDIS_URL', 'redis://localhost:6379'))
    job = get_current_job(connection=redis_conn)

    if not job.dependency_ids:
        return {"error": "Database save job is missing a dependency."}

    parent_job_id = job.dependency_ids[0]
    parent_job = Job.fetch(parent_job_id, connection=redis_conn)
    packaged_result = parent_job.result

    if not packaged_result or "ai_result" not in packaged_result:
        return {"error": "Dependency job did not return a valid result.", "details": str(packaged_result)}

    try:
        task_details = {
            "task_type": "save_schedule_and_notify",
            "ai_result": packaged_result["ai_result"]
        }
        task_json_string = json.dumps(task_details)

        # Execute the external script in a completely new and clean process
        result = subprocess.run(
            [sys.executable, 'task_runner.py', task_json_string],
            capture_output=True,
            text=True,
            check=True
        )

        # The script prints the final JSON result to stdout
        final_result = json.loads(result.stdout)
        return final_result

    except subprocess.CalledProcessError as e:
        error_output = e.stderr
        try:
            return json.loads(error_output)
        except json.JSONDecodeError:
            return {"error": "Task runner script failed with non-JSON output.", "details": error_output}
    except Exception as e:
        return {"error": "An unexpected error occurred in the RQ wrapper function.", "details": str(e)}

import redis
from rq import Queue, Retry
from rq.job import Job
import os

# Redis and RQ setup outside the function to avoid re-initialization
redis_conn = redis.from_url(os.getenv('REDIS_URL', 'redis://localhost:6379'))
q = Queue(connection=redis_conn)

def convert_markdown_to_html(job_id):
    """
    This function is now simplified. It just fetches the result from the
    dependency job, as the markdown conversion and notification are now
    handled within the task_runner.py script.
    """
    try:
        # Fetch the job that this one depends on
        ai_job = Job.fetch(job_id, connection=redis_conn)
        return ai_job.result
    except Exception as e:
        # Handle cases where the job can't be fetched or result is invalid
        return {"error": "Failed to fetch final result.", "details": str(e)}

def extract_text_from_file(file_path):
    """Extracts text content from various file types."""
    text = ""
    try:
        if file_path.endswith('.pdf'):
            with fitz.open(file_path) as doc:
                for page in doc:
                    text += page.get_text()
        elif file_path.endswith(('.xlsx', '.xls')):
            df = pd.read_excel(file_path, engine='openpyxl' if file_path.endswith('.xlsx') else None)
            text = df.to_string()
        elif file_path.endswith('.docx'):
            doc = docx.Document(file_path)
            full_text = [para.text for para in doc.paragraphs]
            text = '\n'.join(full_text)
        else: # Fallback for .txt and other text-based files
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                text = f.read()
    except Exception as e:
        print(f"Error extracting text from {file_path}: {e}")
        return f"[Could not read file: {os.path.basename(file_path)} due to error: {e}]\n"
    return text

def generate_prompts_from_files_task(upload_type, files, additional_info):
    """
    A wrapper function that RQ will execute for a specific upload type.
    This function calls the external task_runner.py script in a clean subprocess
    to perform the prompt generation, avoiding fork-safety issues.
    It also updates the job's meta for progress tracking.
    """
    job = get_current_job()

    def update_progress(status, progress):
        if job:
            job.meta['status'] = status
            job.meta['progress'] = progress
            job.save_meta()

    try:
        update_progress('正在准备处理...', 5)
        
        task_details = {
            "task_type": "prompt_generation",
            "upload_type": upload_type,
            "files": files,
            "additional_info": additional_info,
        }
        task_json_string = json.dumps(task_details)

        update_progress('正在启动分析子进程...', 10)

        # Execute the external script in a completely new and clean process
        result = subprocess.run(
            [sys.executable, 'task_runner.py', task_json_string],
            capture_output=True,
            text=True,
            check=True
        )

        update_progress('处理完成', 100)
        
        # The script prints the final JSON result to stdout
        return json.loads(result.stdout)

    except subprocess.CalledProcessError as e:
        error_output = e.stderr
        update_progress(f'处理失败: {error_output}', 100)
        try:
            return json.loads(error_output)
        except json.JSONDecodeError:
            return {"error": "Task runner script failed with non-JSON output.", "details": error_output}
    except Exception as e:
        update_progress(f'处理失败: {str(e)}', 100)
        return {"error": "An unexpected error occurred in the RQ wrapper function.", "details": str(e)}

# --- NEW AUTOMATED TASKS ---

def run_automated_comprehensive_analysis():
    """
    RQ task to run the comprehensive analysis automatically.
    Relies on RQ's retry mechanism.
    """
    try:
        task_details = {
            "task_type": "automated_comprehensive_analysis",
            "date": datetime.now().strftime('%Y-%m-%d')
        }
        task_json_string = json.dumps(task_details)
        result = subprocess.run(
            [sys.executable, 'task_runner.py', task_json_string],
            capture_output=True, text=True, check=True, encoding='utf-8'
        )
        analysis_result = json.loads(result.stdout)

        # Validation: Check if the report is long enough to be meaningful.
        if len(analysis_result.get("analysis_report", "")) < 400:
            # Raise an exception to signal failure to RQ.
            raise ValueError("Generated report is too short.")

        return analysis_result
    except (subprocess.CalledProcessError, ValueError, json.JSONDecodeError) as e:
        # Re-raise the exception to let RQ handle the retry.
        raise e

def run_automated_data_analysis():
    """
    RQ task to run the data analysis automatically.
    Relies on RQ's retry mechanism.
    """
    try:
        task_details = {
            "task_type": "automated_data_analysis",
            "date": datetime.now().strftime('%Y-%m-%d')
        }
        task_json_string = json.dumps(task_details)
        result = subprocess.run(
            [sys.executable, 'task_runner.py', task_json_string],
            capture_output=True, text=True, check=True, encoding='utf-8'
        )
        analysis_result = json.loads(result.stdout)

        # Validation: Check if the report is long enough to be meaningful.
        if len(analysis_result.get("analysis_report", "")) < 400:
            # Raise an exception to signal failure to RQ.
            raise ValueError("Generated report is too short.")

        return analysis_result
    except (subprocess.CalledProcessError, ValueError, json.JSONDecodeError) as e:
        # Re-raise the exception to let RQ handle the retry.
        raise e

from datetime import timedelta

def run_automated_daily_plan():
    """
    RQ task to generate the current day's plan automatically.
    Relies on RQ's retry mechanism.
    """
    try:
        task_details = {
            "task_type": "automated_daily_plan",
            "date": datetime.now().strftime('%Y-%m-%d'),
            "user_request": "根据过去计划情况和数据,帮我规划今天的详细训练计划"
        }
        task_json_string = json.dumps(task_details)
        result = subprocess.run(
            [sys.executable, 'task_runner.py', task_json_string],
            capture_output=True, text=True, check=True, encoding='utf-8'
        )
        plan_result = json.loads(result.stdout)

        # Validation: Check for the 'plan_adjustment' key and its content.
        if 'plan_adjustment' not in plan_result or not plan_result['plan_adjustment']:
            raise ValueError("Invalid or empty plan format received.")

        return plan_result
    except (subprocess.CalledProcessError, ValueError, json.JSONDecodeError) as e:
        # Re-raise the exception to let RQ handle the retry.
        raise e
