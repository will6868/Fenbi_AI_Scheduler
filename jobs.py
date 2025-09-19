import json
import subprocess
import sys
from datetime import datetime, time
import logging

# Import necessary components but keep it minimal for the worker context
from redis import Redis
from rq import Queue, Retry
from rq_scheduler import Scheduler

from app import app as flask_app
from models import AutomationSettings
from database import CentralSession

# This is the main queue the application uses
q = Queue(connection=Redis.from_url('redis://'))

def run_task_in_subprocess(task_type, task_details):
    """
    A generic wrapper to run any task type in a clean subprocess.
    """
    try:
        # Add task_type to the details payload
        task_details['task_type'] = task_type
        task_json_string = json.dumps(task_details)

        result = subprocess.run(
            [sys.executable, 'task_runner.py', task_json_string],
            capture_output=True,
            text=True,
            check=True,
            encoding='utf-8'
        )
        return json.loads(result.stdout)

    except subprocess.CalledProcessError as e:
        error_output = e.stderr
        try:
            error_json = json.loads(error_output)
            return error_json
        except (json.JSONDecodeError, TypeError):
            return {"error": "Task runner script failed with non-JSON output.", "details": error_output}
    except Exception as e:
        return {"error": "An unexpected error occurred in the RQ wrapper function.", "details": str(e)}

def schedule_automated_tasks():
    """
    Checks the database for enabled automation settings and enqueues tasks
    if their scheduled time has been reached. This function should be run
    periodically (e.g., every minute).
    """
    logging.info("Running automation task scheduler...")
    with flask_app.app_context():
        session = CentralSession()
        try:
            settings = session.query(AutomationSettings).first()
            if not settings:
                logging.info("No automation settings found.")
                return

            now = datetime.now()
            today_str = now.strftime('%Y-%m-%d')

            # --- Helper to check if a task should run ---
            def should_run(task_key, enabled, exec_time_str, last_run_times):
                if not enabled or not exec_time_str:
                    return False
                
                try:
                    # First, check if the task has already run today. This is a safeguard.
                    last_run_str = last_run_times.get(task_key)
                    if last_run_str:
                        last_run_dt = datetime.fromisoformat(last_run_str)
                        if last_run_dt.date() == now.date():
                            return False # Already ran today

                    # Second, check if it is the exact minute to run the task.
                    exec_time = datetime.strptime(exec_time_str, '%H:%M').time()
                    is_correct_time = now.hour == exec_time.hour and now.minute == exec_time.minute
                    
                    return is_correct_time

                except (ValueError, TypeError):
                    logging.error(f"Invalid time format for task {task_key}: {exec_time_str}")
                    return False

            # --- Task Execution Logic ---
            def execute_task(task_key, task_func, *args, **kwargs):
                logging.info(f"Enqueuing automated task: {task_key}")
                
                # Extract retry from kwargs if it exists, otherwise it's None
                retry_policy = kwargs.pop('retry', None)

                # Update last_run time BEFORE enqueuing to prevent race conditions
                if settings.last_run is None:
                    settings.last_run = {}
                settings.last_run[task_key] = now.isoformat()
                session.commit()

                # Now, enqueue the task with the retry policy
                q.enqueue(task_func, *args, **kwargs, retry=retry_policy)

            # --- Check for AI Comprehensive Analysis ---
            task_key = 'comprehensive_analysis'
            if should_run(task_key, settings.enabled.get(task_key), settings.execution_time.get(task_key), settings.last_run or {}):
                execute_task(
                    task_key,
                    run_task_in_subprocess,
                    'automated_comprehensive_analysis',
                    {'plan_date': today_str},
                    retry=Retry(max=3)
                )

            # --- Check for AI Data Analysis ---
            task_key = 'data_analysis'
            if should_run(task_key, settings.enabled.get(task_key), settings.execution_time.get(task_key), settings.last_run or {}):
                execute_task(
                    task_key,
                    run_task_in_subprocess,
                    'automated_data_analysis',
                    {},  # This task doesn't require specific details
                    retry=Retry(max=3)
                )

            # --- Check for Daily Plan Generation ---
            task_key = 'daily_plan'
            if should_run(task_key, settings.enabled.get(task_key), settings.execution_time.get(task_key), settings.last_run or {}):
                from services import get_schedule_and_history_for_ai
                from tasks import prepare_and_run_schedule_adjustment, save_adjusted_schedule_to_db

                logging.info(f"Enqueuing automated task chain for: {task_key}")

                # Update last_run time BEFORE enqueuing to prevent race conditions
                if settings.last_run is None:
                    settings.last_run = {}
                settings.last_run[task_key] = now.isoformat()
                session.commit()

                # Prepare data for the task
                combined_data = get_schedule_and_history_for_ai(start_date_str=today_str, days=7)
                user_request = "仔细规划今日计划"

                # Step 1: Enqueue the AI adjustment job
                adjustment_job = q.enqueue(
                    prepare_and_run_schedule_adjustment,
                    args=(combined_data, user_request, today_str),
                    retry=Retry(max=3),
                    job_timeout='15m'
                )

                # Step 2: Enqueue the database save job, making it dependent on the first job
                q.enqueue(
                    save_adjusted_schedule_to_db,
                    depends_on=adjustment_job,
                    retry=Retry(max=1)
                )

        except Exception as e:
            logging.error(f"Error during task scheduling: {e}", exc_info=True)
        finally:
            session.close()
