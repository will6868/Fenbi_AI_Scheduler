import os
from flask import Flask, request, jsonify, render_template, Response, send_from_directory
from werkzeug.utils import secure_filename
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import redis
from rq import Queue
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.sql import func
from ai_analyzer import get_ai_config, save_ai_config, test_ai_connection_stream, analyze_pdf_direct_non_stream, analyze_image_with_ai
from tasks import run_analysis_in_subprocess, prepare_and_run_comprehensive_analysis, save_adjusted_schedule_to_db, prepare_and_run_schedule_adjustment, generate_prompts_from_files_task
import json
import subprocess
from datetime import datetime
import argparse
import markdown
import sys
from wechat_sender import send_wechat_message
import fitz  # PyMuPDF
import pandas as pd
import docx

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads'
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.instance_path, exist_ok=True)

# Redis and RQ setup
redis_conn = redis.from_url(os.getenv('REDIS_URL', 'redis://localhost:6379'))
q = Queue(connection=redis_conn)

# --- Database and Service Imports ---
from database import CentralSession, get_db_session
from models import AnalysisResult, StudyPlan, DailySchedule, PracticeCategory, CATEGORY_TO_FOLDER, VALUE_TO_FOLDER, AutomationSettings
from services import _synchronize_plan_from_schedule, get_all_history_data, get_recent_history, get_schedule_and_history_for_ai
from sqlalchemy import create_engine, inspect, text

def extract_text_from_file(file_path):
    """
    Extracts text content from various file types, with a fallback for corrupted docx files.
    """
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
            try:
                doc = docx.Document(file_path)
                full_text = [para.text for para in doc.paragraphs]
                text = '\n'.join(full_text)
            except Exception as docx_error:
                print(f"python-docx failed for {file_path}: {docx_error}. Attempting fallback with textutil.")
                try:
                    # Fallback for macOS using textutil, which is more robust
                    result = subprocess.run(
                        ['textutil', '-convert', 'txt', file_path, '-stdout'],
                        capture_output=True, text=True, check=True
                    )
                    text = result.stdout
                except (subprocess.CalledProcessError, FileNotFoundError) as textutil_error:
                    print(f"textutil fallback also failed for {file_path}: {textutil_error}")
                    raise docx_error # Re-raise the original, more specific error
        else:  # Fallback for .txt and other text-based files
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                text = f.read()
    except Exception as e:
        print(f"Error extracting text from {file_path}: {e}")
        return f"[Could not read file: {os.path.basename(file_path)} due to error: {e}]\n"
    return text

def get_todays_answered_questions_summary(date_str):
    """
    Scans all category databases for a specific date and returns a dictionary
    mapping each practice_type (string) to its total answered questions.
    """
    summary = {}
    base_dir = 'database'

    # To ensure data integrity and prevent scanning irrelevant folders,
    # we will ONLY iterate over the predefined folders in CATEGORY_TO_FOLDER.
    for folder_name in CATEGORY_TO_FOLDER.values():
        category_dir = os.path.join(base_dir, folder_name)
        
        if not os.path.isdir(category_dir):
            continue

        db_path = os.path.join(category_dir, f"{date_str}.db")
        if os.path.exists(db_path):
            try:
                engine = create_engine(f"sqlite:///{db_path}")
                Session = sessionmaker(bind=engine)
                db_session = Session()

                # Query the database to get the sum of answered questions, grouped by the actual
                # 'practice_type' string stored in the table. This avoids any issues with
                # folder names not matching the practice type.
                results = db_session.query(
                    AnalysisResult.practice_type,
                    func.sum(AnalysisResult.questions_answered)
                ).group_by(AnalysisResult.practice_type).all()
                
                # Aggregate the results into the summary dictionary
                for practice_type_str, total_answered in results:
                    if total_answered is not None:
                        summary[practice_type_str] = summary.get(practice_type_str, 0) + total_answered

                db_session.close()
            except Exception as e:
                # Log any errors encountered while processing a database file
                print(f"Could not process database {db_path}: {e}")
                
    return summary

def get_todays_total_incorrect_questions(date_str):
    """
    Scans all category databases for a specific date and returns the total
    number of incorrect questions.
    """
    total_incorrect = 0
    base_dir = 'database'

    # Iterate over all predefined category folders to find all of today's DBs
    for folder_name in CATEGORY_TO_FOLDER.values():
        category_dir = os.path.join(base_dir, folder_name)
        
        if not os.path.isdir(category_dir):
            continue

        db_path = os.path.join(category_dir, f"{date_str}.db")
        if os.path.exists(db_path):
            try:
                engine = create_engine(f"sqlite:///{db_path}")
                Session = sessionmaker(bind=engine)
                db_session = Session()

                # Query the database to get the sum of incorrect answers for the day
                daily_incorrect = db_session.query(
                    func.sum(AnalysisResult.incorrect_answers)
                ).scalar()
                
                if daily_incorrect is not None:
                    total_incorrect += daily_incorrect

                db_session.close()
            except Exception as e:
                print(f"Could not process database {db_path} for incorrect count: {e}")
                
    return total_incorrect

def get_todays_full_records(date_str):
    """
    Scans all category databases for a specific date and returns a list of full
    AnalysisResult records, sorted by submission time.
    """
    records = []
    base_dir = 'database'

    for folder_name in CATEGORY_TO_FOLDER.values():
        db_path = os.path.join(base_dir, folder_name, f"{date_str}.db")
        if os.path.exists(db_path):
            try:
                engine = create_engine(f"sqlite:///{db_path}")
                Session = sessionmaker(bind=engine)
                db_session = Session()

                results = db_session.query(AnalysisResult).all()
                
                for res in results:
                    records.append(res.to_dict())

                db_session.close()
            except Exception as e:
                print(f"Could not process database {db_path} for full records: {e}")
    
    # Sort records by submission time
    records.sort(key=lambda x: x.get('submission_time', ''))
    return records

def get_todays_incorrect_records(date_str):
    """
    Scans all category databases for a specific date and returns a list of records
    containing submission times, incorrect answer counts, and practice type, sorted by time.
    """
    records = []
    base_dir = 'database'

    for folder_name in CATEGORY_TO_FOLDER.values():
        db_path = os.path.join(base_dir, folder_name, f"{date_str}.db")
        if os.path.exists(db_path):
            try:
                engine = create_engine(f"sqlite:///{db_path}")
                Session = sessionmaker(bind=engine)
                db_session = Session()

                results = db_session.query(
                    AnalysisResult.submission_time,
                    AnalysisResult.incorrect_answers,
                    AnalysisResult.practice_type
                ).filter(AnalysisResult.incorrect_answers > 0).all()
                
                for time, incorrect_count, practice_type in results:
                    if time: # Ensure time is not None
                        records.append({'time': time, 'incorrect': incorrect_count, 'type': practice_type})

                db_session.close()
            except Exception as e:
                print(f"Could not process database {db_path} for incorrect records: {e}")
    
    # Sort records by submission time
    records.sort(key=lambda x: x['time'])
    return records

def get_total_reviewable_for_category(category_str, date_str):
    """
    Scans the specific category database for a given date and returns the total
    number of incorrect AND unanswered questions for that category.
    If the database file does not exist, it returns 0.
    """
    print(f"[DEBUG] get_total_reviewable_for_category called with: category='{category_str}', date='{date_str}'")
    
    # Determine the database path without creating a session yet
    folder_name = VALUE_TO_FOLDER.get(category_str)
    if not folder_name:
        # Handle custom goals that might not have a predefined folder
        folder_name = secure_filename(category_str.replace(" ", "_"))
    
    db_path = os.path.join('database', folder_name, f"{date_str}.db")

    # If the database file doesn't exist, no submissions were made.
    if not os.path.exists(db_path):
        print(f"[DEBUG] Database file not found at {db_path}. Returning 0 reviewable questions.")
        return 0

    total_reviewable = 0
    try:
        # Now that we know the file exists, get a session to it.
        session = get_db_session(category_str, date_str)
        
        # Query the database to get the sum of incorrect and unanswered answers.
        # func.coalesce is used to treat NULL values as 0.
        category_total = session.query(
            func.sum(func.coalesce(AnalysisResult.incorrect_answers, 0) + func.coalesce(AnalysisResult.unanswered_questions, 0))
        ).scalar()
        
        if category_total is not None:
            total_reviewable = category_total
            
        session.close()
    except Exception as e:
        # This can happen if the DB exists but is corrupt, etc.
        print(f"Could not process database for {category_str} on {date_str} for reviewable count: {e}")
    
    print(f"[DEBUG] Returning total_reviewable: {total_reviewable}")
    return total_reviewable

def get_dashboard_data(date_str):
    """Helper function to fetch and calculate all dashboard data for a given date."""
    # 1. Get the plan for the given date
    session = CentralSession()
    plan = session.query(StudyPlan).filter_by(plan_date=date_str).first()
    schedule = session.query(DailySchedule).filter_by(schedule_date=date_str).first()
    session.close()

    # 2. Get a summary of all answered questions for the given date.
    answered_by_category = get_todays_answered_questions_summary(date_str)

    # 3. --- Dynamic Goal Update for 'Review' Type ---
    if plan and plan.goals and schedule and schedule.schedule_items:
        # Sort schedule items by time to ensure chronological order
        schedule.schedule_items.sort(key=lambda x: x.get('start_time', ''))
        
        # Align goals with their corresponding schedule items that are training-related
        training_items = [
            item for item in schedule.schedule_items 
            if any(k in item.get('activity', '') for k in ['训练', '学习', '测试', '复盘', '模考', '专项', '言语', '数量', '判断', '资料', '常识'])
        ]
        
        for i, goal in enumerate(plan.goals):
            # Ensure we don't go out of bounds if the plan and schedule are mismatched
            if i >= len(training_items):
                break

            current_item = training_items[i]
            is_review_goal = '复盘' in goal.get('type', '') or '错题' in goal.get('type', '')

            if is_review_goal:
                # --- UNIFIED LOGIC ---
                # Directly call the get_training_details logic to get the calculated goal.
                # This ensures the dashboard and the training page are perfectly in sync.
                # We need to simulate the request arguments for the function.
                with app.test_request_context(f'/?date={date_str}&goal_id={i}'):
                    response = get_training_details()
                    if response.status_code == 200:
                        data = response.get_json()
                        # Update the goal in the plan with the accurately calculated target
                        goal['target_questions'] = data.get('goal', {}).get('target_questions', 0)
                    else:
                        # Handle cases where the details couldn't be fetched
                        goal['target_questions'] = 0

    # 4. Calculate progress for each goal and grand totals
    goals_progress = []
    total_target_today = 0
    # This is the RAW total of all questions answered today, used for display.
    total_answered_today = sum(answered_by_category.values())

    if plan and plan.goals and schedule and schedule.schedule_items:
        # --- NEW TIME-AWARE PROGRESS CALCULATION ---
        total_target_today = sum(g.get('target_questions', 0) for g in plan.goals)
        
        # Get all individual submission records for the day, which include submission times.
        all_todays_records = get_todays_full_records(date_str)

        # Align goals with their corresponding schedule items that are training-related
        schedule.schedule_items.sort(key=lambda x: x.get('start_time', ''))
        training_items = [
            item for item in schedule.schedule_items 
            if any(k in item.get('activity', '') for k in ['训练', '学习', '测试', '复盘', '模考', '专项', '言语', '数量', '判断', '资料', '常识'])
        ]

        for i, goal in enumerate(plan.goals):
            goal_type = goal.get('type')
            target = goal.get('target_questions', 0)
            answered_for_this_goal = 0
            start_time = None
            end_time = None

            # Find the time window for the current goal from the schedule
            if i < len(training_items):
                current_item = training_items[i]
                start_time = current_item.get('start_time', '00:00')
                end_time = current_item.get('end_time', '23:59')
                start_time_str = f"{date_str} {start_time}:00"
                end_time_str = f"{date_str} {end_time}:59"

                # Convert schedule times to datetime objects for accurate comparison
                start_time_dt = datetime.strptime(f"{date_str} {current_item.get('start_time', '00:00')}", '%Y-%m-%d %H:%M')
                end_time_dt = datetime.strptime(f"{date_str} {current_item.get('end_time', '23:59')}", '%Y-%m-%d %H:%M')

                # Iterate through all of today's records to find ones that match this goal's type and time window
                for record in all_todays_records:
                    record_type = record.get('practice_type')
                    record_time_str = record.get('submission_time')
                    
                    if record_type == goal_type and record_time_str:
                        record_time_dt = None
                        try:
                            # Normalize and parse timestamp, trying formats with and without seconds
                            normalized_ts = record_time_str.replace('.', '-')
                            try:
                                record_time_dt = datetime.strptime(normalized_ts, '%Y-%m-%d %H:%M:%S')
                            except ValueError:
                                record_time_dt = datetime.strptime(normalized_ts, '%Y-%m-%d %H:%M')
                        except ValueError:
                            # Log error if timestamp format is unrecognizable
                            print(f"Could not parse record timestamp during progress calculation: {record_time_str}")
                            continue
                        
                        # Perform the comparison using datetime objects
                        if record_time_dt and start_time_dt <= record_time_dt <= end_time_dt:
                            answered_for_this_goal += record.get('questions_answered', 0)

            percentage = 0
            if target > 0:
                percentage = min(100, round((answered_for_this_goal / target) * 100))
            elif answered_for_this_goal > 0:
                percentage = 100
            
            goals_progress.append({
                "id": i,
                "type": goal_type,
                "target_questions": target,
                "answered_questions": answered_for_this_goal,
                "percentage": percentage,
                "target_accuracy": goal.get('target_accuracy'),
                "target_time_minutes": goal.get('target_time_minutes'),
                "start_time": start_time,
                "end_time": end_time
            })
    # Fallback for when there is no schedule, though this is less likely
    elif plan and plan.goals:
        total_target_today = sum(g.get('target_questions', 0) for g in plan.goals)
        for i, goal in enumerate(plan.goals):
             goals_progress.append({
                "id": i, "type": goal.get('type'), "target_questions": goal.get('target_questions', 0),
                "answered_questions": 0, "percentage": 0, "target_accuracy": goal.get('target_accuracy'),
                "target_time_minutes": goal.get('target_time_minutes')
            })

    # 5. Calculate grand total completion percentage, capping progress at 100% for each goal.
    # This is the CAPPED total used for the overall progress percentage.
    progress_total_answered = sum(min(g.get('answered_questions', 0), g.get('target_questions', 0)) for g in goals_progress)
    
    completion_percentage = 0
    if total_target_today > 0:
        completion_percentage = min(100, round((progress_total_answered / total_target_today) * 100))
    elif total_answered_today > 0: # If no target but work was done, show 100%
        completion_percentage = 100

    # 6. Return a dictionary of all calculated data
    return {
        "total_answered": total_answered_today, # Display the raw total
        "total_target": total_target_today,
        "percentage": completion_percentage, # Use the capped percentage
        "goals_progress": goals_progress
    }

@app.route('/get_dashboard_data', methods=['GET'])
def get_dashboard_data_route():
    date_str = request.args.get('date')
    if not date_str:
        return jsonify({"error": "Date parameter is required"}), 400
    data = get_dashboard_data(date_str)
    return jsonify(data)

@app.route('/')
def index():
    # Allow fetching data for a specific date via query parameter, default to today
    date_str = request.args.get('date', datetime.now().strftime('%Y-%m-%d'))
    dashboard_data = get_dashboard_data(date_str)
    # Pass the date to the template so the date picker can be set correctly
    dashboard_data['selected_date'] = date_str
    return render_template('index.html', **dashboard_data)

@app.route('/training')
def training():
    return render_template('training.html')

@app.route('/settings')
def settings():
    return render_template('settings.html')

@app.route('/save_settings', methods=['POST'])
def save_settings():
    data = request.json
    
    # 提取AI配置和Webhook URL
    ai_config_data = {
        'api_url': data.get('api_url'),
        'api_key': data.get('api_key'),
        'model': data.get('model')
    }
    webhook_url = data.get('wechat_webhook_url')

    # 保存AI配置
    save_ai_config(ai_config_data)

    # 保存企业微信的Webhook配置
    if webhook_url:
        try:
            with open('config.json', 'r+') as f:
                config = json.load(f)
                config['wechat_webhook_url'] = webhook_url
                f.seek(0)
                json.dump(config, f, indent=4)
                f.truncate()
        except (FileNotFoundError, json.JSONDecodeError):
            # 如果文件不存在或为空，则创建一个新的
            with open('config.json', 'w') as f:
                json.dump({'wechat_webhook_url': webhook_url}, f, indent=4)

    return jsonify({"status": "success"})

@app.route('/test_ai', methods=['POST'])
def test_ai():
    return Response(test_ai_connection_stream(), mimetype='text/event-stream')

@app.route('/upload', methods=['POST'])
def upload_files():
    if 'files[]' not in request.files:
        return jsonify({"error": "No file part"}), 400
    
    files = request.files.getlist('files[]')
    uploaded_filenames = []

    for file in files:
        if file.filename == '':
            continue
        if file:
            filename = secure_filename(file.filename)
            file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
            uploaded_filenames.append(filename)
            
    return jsonify({"uploaded_files": uploaded_filenames})

@app.route('/uploads/<path:filename>')
def serve_upload(filename):
    """Serves a file from the uploads directory."""
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

def save_analysis_to_db(analysis_result, category_from_request, date_from_request):
    """
    A helper function to transform AI analysis result and save it to the database.
    This function will be called by the RQ worker.
    """
    try:
        summary = analysis_result.get("performance_summary", {})
        metadata = analysis_result.get("report_metadata", {})
        metrics = analysis_result.get("calculated_metrics", {})
        
        correct = summary.get("correct_answers", 0)
        incorrect = summary.get("incorrect_answers", 0)
        total = summary.get("total_questions", 0)
        unanswered = summary.get("unanswered_questions", summary.get("unanswered", 0))
        answered = correct + incorrect
        
        accuracy_overall = (correct / total) if total > 0 else 0.0
        accuracy_answered = (correct / answered) if answered > 0 else 0.0

        practice_type_str = category_from_request or \
                            metadata.get("subject") or \
                            metadata.get("exercise_type") or \
                            metadata.get("subject_category")

        flat_data = {
            "practice_type": practice_type_str,
            "submission_time": metadata.get("submission_timestamp") or metadata.get("timestamp"),
            "difficulty": metadata.get("difficulty"),
            "total_questions": total,
            "questions_answered": answered,
            "correct_answers": correct,
            "incorrect_answers": incorrect,
            "unanswered_questions": unanswered,
            "total_time_minutes": summary.get("total_time_minutes"),
            "accuracy_rate_overall": accuracy_overall,
            "accuracy_rate_answered": accuracy_answered,
            "completion_score": analysis_result.get("completion_score"),
            "incorrect_question_numbers": [item["question_number"] for item in analysis_result.get("answer_details", []) if item["status"] == "incorrect"],
            "answer_card": {str(item["question_number"]): item["status"] for item in analysis_result.get("answer_details", [])},
            "ability_analysis": analysis_result.get("ability_analysis", {})
        }

        if not practice_type_str:
            raise ValueError("Could not determine 'practice_type' from request or AI response")

        now = datetime.now()
        date_str = date_from_request or now.strftime('%Y-%m-%d')

        submission_timestamp_str = flat_data.get("submission_time")
        if submission_timestamp_str == "USE_CURRENT_TIME" or not submission_timestamp_str:
            flat_data["submission_time"] = now.strftime('%Y-%m-%d %H:%M:%S')
        else:
            try:
                normalized_ts_str = submission_timestamp_str.replace('.', '-')
                dt_obj = None
                try:
                    dt_obj = datetime.strptime(normalized_ts_str, '%Y-%m-%d %H:%M:%S')
                except ValueError:
                    dt_obj = datetime.strptime(normalized_ts_str, '%Y-%m-%d %H:%M')
                flat_data["submission_time"] = dt_obj.strftime('%Y-%m-%d %H:%M:%S')
            except ValueError:
                print(f"Warning: Could not parse AI-provided timestamp '{submission_timestamp_str}'. Falling back to current time.")
                flat_data["submission_time"] = now.strftime('%Y-%m-%d %H:%M:%S')

        session = get_db_session(practice_type_str, date_str)
        try:
            new_record = AnalysisResult(**flat_data)
            session.add(new_record)
            session.commit()
        except Exception as e:
            session.rollback()
            raise e
        finally:
            session.close()
        
        # --- FIX: Return a well-structured dictionary for downstream tasks ---
        # Instead of returning the raw AI result, return a dictionary that
        # contains all the processed and calculated data, ensuring that the
        # notification sender gets reliable information.

        # Reconstruct the necessary parts for the notification message
        reconstructed_summary = {
            "total_questions": flat_data.get("total_questions", 0),
            "correct_answers": flat_data.get("correct_answers", 0),
            "incorrect_answers": flat_data.get("incorrect_answers", 0),
            "unanswered_questions": flat_data.get("unanswered_questions", 0),
            "total_time_minutes": flat_data.get("total_time_minutes", 0)
        }

        reconstructed_metadata = {
            "filename": metadata.get("filename"), # Preserve original filename if available
            "subject": flat_data.get("practice_type", "N/A"),
            "submission_timestamp": flat_data.get("submission_time")
        }
        
        reconstructed_metrics = {
            "accuracy_rate_answered_str": f"{flat_data.get('accuracy_rate_answered', 0.0) * 100:.1f}%"
        }

        # Merge the original result with our reconstructed, reliable data.
        # The reconstructed data takes precedence.
        final_structured_result = analysis_result.copy()
        final_structured_result.update({
            "performance_summary": reconstructed_summary,
            "report_metadata": reconstructed_metadata,
            "calculated_metrics": reconstructed_metrics,
            "db_flat_data": flat_data # Also include the flattened data for potential future use
        })
        
        return final_structured_result

    except Exception as e:
        print(f"Database save failed: {e}")
        # We need to return an error that can be stored in the job result
        return {"error": "Failed to save data to the database.", "details": str(e)}




@app.route('/analyze', methods=['POST'])
def analyze():
    data = request.json
    filenames = data.get('filenames', [])
    goal = data.get('goal')
    category_from_request = data.get('category')
    date_from_request = data.get('date')

    if not filenames:
        return jsonify({"error": "No filenames provided"}), 400

    filename_to_analyze = filenames[0]
    
    # Enqueue the subprocess wrapper task.
    # This single job will handle analysis and database saving in an isolated process.
    job = q.enqueue(
        run_analysis_in_subprocess,
        filename_to_analyze,
        goal,
        category_from_request,
        date_from_request,
        app.config['UPLOAD_FOLDER'],
        result_ttl=86400
    )
    
    return jsonify({"job_id": job.id})

@app.route('/analysis_status/<job_id>', methods=['GET'])
def analysis_status(job_id):
    job = q.fetch_job(job_id)
    if job is None:
        return jsonify({'status': 'not found'}), 404
    
    response = {
        'status': job.get_status(),
        'result': job.result,
    }
    return jsonify(response)

# Fetches historical analysis results. Can be filtered by type and date.
@app.route('/get_history', methods=['GET'])
def get_history():
    practice_type_str = request.args.get('type')
    date_str = request.args.get('date')
    goal_id_str = request.args.get('goal_id') # New optional parameter

    # --- Scenario 1: Specific type and date are provided (for training page) ---
    if practice_type_str and date_str:
        try:
            session = get_db_session(practice_type_str, date_str)
            results_for_day = [res.to_dict() for res in session.query(AnalysisResult).all()]
            session.close()
        except Exception as e:
            print(f"Could not read from specific database for {practice_type_str} on {date_str}: {e}")
            return jsonify([])

        # If goal_id is provided, filter results by the goal's time window
        if goal_id_str is not None:
            try:
                goal_id = int(goal_id_str)
                central_session = CentralSession()
                schedule = central_session.query(DailySchedule).filter_by(schedule_date=date_str).first()
                central_session.close()

                if schedule and schedule.schedule_items:
                    schedule.schedule_items.sort(key=lambda x: x.get('start_time', ''))
                    training_items = [
                        item for item in schedule.schedule_items 
                        if any(k in item.get('activity', '') for k in ['训练', '学习', '测试', '复盘', '模考', '专项', '言语', '数量', '判断', '资料', '常识'])
                    ]

                    if goal_id < len(training_items):
                        current_item = training_items[goal_id]
                        start_time_str = f"{date_str} {current_item.get('start_time', '00:00')}:00"
                        end_time_str = f"{date_str} {current_item.get('end_time', '23:59')}:59"
                        start_time_dt = datetime.strptime(start_time_str, '%Y-%m-%d %H:%M:%S')
                        end_time_dt = datetime.strptime(end_time_str, '%Y-%m-%d %H:%M:%S')

                        filtered_results = []
                        for record in results_for_day:
                            record_time_str = record.get('submission_time')
                            if record_time_str:
                                record_time_dt = None
                                try:
                                    normalized_ts = record_time_str.replace('.', '-')
                                    try:
                                        record_time_dt = datetime.strptime(normalized_ts, '%Y-%m-%d %H:%M:%S')
                                    except ValueError:
                                        record_time_dt = datetime.strptime(normalized_ts, '%Y-%m-%d %H:%M')
                                except ValueError:
                                    continue
                                
                                if record_time_dt and start_time_dt <= record_time_dt <= end_time_dt:
                                    filtered_results.append(record)
                        
                        all_results = filtered_results
                    else:
                        all_results = [] # Goal ID out of bounds for training items
                else:
                    all_results = results_for_day # No schedule, return all for the day
            except (ValueError, TypeError):
                all_results = results_for_day # Invalid goal_id, return all for the day
        else:
            all_results = results_for_day # No goal_id, return all for the day
    
    # --- Scenario 2: No filters, get all history (for analysis page) ---
    else:
        all_results = []
        base_dir = 'database'
        for category_enum, folder_name in CATEGORY_TO_FOLDER.items():
            category_dir = os.path.join(base_dir, folder_name)
            if not os.path.isdir(category_dir):
                continue

            for db_file in os.listdir(category_dir):
                if db_file.endswith('.db'):
                    db_path = os.path.join(category_dir, db_file)
                    try:
                        engine = create_engine(f"sqlite:///{db_path}")
                        Session = sessionmaker(bind=engine)
                        session = Session()
                        results_for_file = session.query(AnalysisResult).all()
                        all_results.extend([res.to_dict() for res in results_for_file])
                        session.close()
                    except Exception as e:
                        print(f"Could not read from database {db_path}: {e}")
                        continue
    
    # Sort all collected results by submission time before returning
    all_results.sort(key=lambda x: x.get('submission_time', ''), reverse=True)
    
    return jsonify(all_results)

@app.route('/save_plan', methods=['POST'])
def save_plan():
    data = request.json
    plan_date = data.get('plan_date')
    goals = data.get('goals')

    if not plan_date or not goals:
        return jsonify({"error": "Date and goals are required"}), 400

    session = CentralSession()
    try:
        plan = session.query(StudyPlan).filter_by(plan_date=plan_date).first()
        if plan:
            plan.goals = goals
        else:
            plan = StudyPlan(plan_date=plan_date, goals=goals)
            session.add(plan)
        session.commit()
        return jsonify({"status": "success", "plan": plan.to_dict()})
    except SQLAlchemyError as e:
        session.rollback()
        return jsonify({"error": f"Database error: {e}"}), 500
    finally:
        session.close()

@app.route('/get_plan', methods=['GET'])
def get_plan():
    plan_date = request.args.get('date')
    if not plan_date:
        return jsonify({"error": "Date parameter is required"}), 400
    
    session = CentralSession()
    try:
        plan = session.query(StudyPlan).filter_by(plan_date=plan_date).first()
        if plan:
            return jsonify(plan.to_dict())
        else:
            return jsonify({"plan_date": plan_date, "goals": []})
    finally:
        session.close()

# Route for comprehensive AI analysis of the study plan and history
from datetime import datetime, timedelta

@app.route('/comprehensive_analysis', methods=['POST'])
def comprehensive_analysis():
    data = request.json
    plan_date = data.get('plan_date')

    if not plan_date:
        return jsonify({"error": "Plan date is required"}), 400

    # Enqueue the wrapper task that prepares data and runs the analysis in the background.
    # This makes the endpoint immediately responsive.
    job = q.enqueue(prepare_and_run_comprehensive_analysis, plan_date, result_ttl=3600)
    return jsonify({"job_id": job.id})

@app.route('/comprehensive_analysis_status/<job_id>', methods=['GET'])
def comprehensive_analysis_status(job_id):
    job = q.fetch_job(job_id)
    if job is None:
        return jsonify({'status': 'not found'}), 404
    
    response = {
        'status': job.get_status(),
        'result': job.result,
    }
    return jsonify(response)

@app.route('/generate_feedback', methods=['POST'])
def generate_feedback():
    current_result_data = request.json
    
    # 1. Get today's date to fetch plan and today's results
    today_str = current_result_data['submission_time'].split(' ')[0].replace('.', '-')
    
    # 2. Fetch today's study plan
    session = CentralSession()
    try:
        plan = session.query(StudyPlan).filter_by(plan_date=today_str).first()
    finally:
        session.close()
    
    # 3. Fetch today's completed exercises for the same practice type
    # This requires reading from the correct daily DB file.
    practice_type_str = current_result_data['practice_type']
    try:
        # get_db_session now correctly handles the string directly
        db_session = get_db_session(practice_type_str, today_str)
        todays_completed = db_session.query(AnalysisResult).all()
        db_session.close()
    except (ValueError, StopIteration) as e:
        return jsonify({"error": f"Invalid practice type for feedback: {e}"}), 400
    except Exception as e:
        print(f"Could not read today's history for feedback: {e}")
        todays_completed = []

    completed_questions = sum(res.questions_answered for res in todays_completed)
    
    # 4. Fetch the most recent previous exercise of the same type
    # This is now complex as it requires searching previous days' files.
    # For simplicity, we will omit this feature for now in this dynamic setup.
    previous_result = None

    # 5. Generate feedback
    feedback_parts = []
    
    # Plan progress
    if plan and plan.goals:
        goal = next((g for g in plan.goals if g['type'] == practice_type), None)
        if goal:
            target_q = goal.get('target_questions', 0)
            feedback_parts.append(f"今日计划: {practice_type} {completed_questions}/{target_q} 题。")
        else:
            feedback_parts.append(f"今日已完成 {practice_type} {completed_questions} 题。")
    else:
        feedback_parts.append(f"今日已完成 {practice_type} {completed_questions} 题。")

    # Performance comparison
    if previous_result:
        prev_acc = float(previous_result.accuracy_rate.replace('%', ''))
        curr_acc = float(current_result_data['accuracy_rate'].replace('%', ''))
        if curr_acc > prev_acc:
            feedback_parts.append(f"表现提升！正确率从 {prev_acc}% 上升到 {curr_acc}%。")
        elif curr_acc < prev_acc:
            feedback_parts.append(f"本次表现略有下滑，正确率从 {prev_acc}% 降至 {curr_acc}%。别灰心，看看错题分析。")
        else:
            feedback_parts.append("表现稳定，继续保持！")

    # Suggestion based on weak points (example logic)
    if current_result_data['incorrect_answers'] > 0:
        weakest_ability = min(current_result_data.get('ability_analysis', {}), key=current_result_data.get('ability_analysis', {}).get)
        if weakest_ability:
            feedback_parts.append(f"建议: 关注错题，特别是 '{weakest_ability}' 相关的知识点。")

    return jsonify({"feedback": " ".join(feedback_parts)})

@app.route('/schedule')
def schedule():
    return render_template('schedule.html')

@app.route('/analyze_schedule', methods=['POST'])
def analyze_schedule():
    data = request.json
    schedule = data.get('schedule', [])
    
    # 在这里，您可以调用真正的AI分析服务
    # 为了演示，我们返回一个基于简单规则的模拟分析
    
    training_sessions = [item for item in schedule if '训练' in item['activity']]
    sleep_time = next((item for item in schedule if '睡觉' in item['activity']), None)
    
    analysis = "这是一个不错的计划！"
    
    if len(training_sessions) >= 2:
        analysis += " 您安排了两次训练，很棒。请确保训练之间有足够的休息。"
        
    if sleep_time:
        sleep_hour = int(sleep_time['time'].split(':')[0])
        if sleep_hour >= 23 or sleep_hour < 5:
            analysis += " 您的睡眠时间有点晚，建议早点休息以保证恢复。"
            
    return jsonify({"analysis": analysis})

# Route to fetch the schedule for a specific day
@app.route('/get_daily_schedule', methods=['GET'])
def get_daily_schedule():
    schedule_date = request.args.get('date')
    if not schedule_date:
        return jsonify({"error": "Date parameter is required"}), 400
    
    session = CentralSession()
    try:
        schedule = session.query(DailySchedule).filter_by(schedule_date=schedule_date).first()
        if schedule:
            return jsonify(schedule.to_dict())
        else:
            # If no schedule exists, create a default one AND a default study plan
            default_schedule_items = [
                { "start_time": "07:00", "end_time": "07:30", "activity": "特殊-特殊", "details": "起床和洗漱" },
                { "start_time": "09:00", "end_time": "10:30", "activity": "资料分析-资料", "details": "专项训练" },
                { "start_time": "11:00", "end_time": "12:00", "activity": "言语理解-言语", "details": "专项训练" },
                { "start_time": "14:30", "end_time": "16:00", "activity": "特殊-特殊", "details": "自主安排学习" },
            ]
            new_schedule = DailySchedule(schedule_date=schedule_date, schedule_items=default_schedule_items)
            session.add(new_schedule)

            plan = session.query(StudyPlan).filter_by(plan_date=schedule_date).first()
            if not plan:
                # Dynamically generate goals from the default schedule items
                generated_goals = []
                for item in default_schedule_items:
                    activity = item.get('activity', '')
                    # Check if the activity is a training session
                    keywords = ['训练', '学习', '测试', '复盘', '模考', '专项', '言语', '数量', '判断', '资料', '常识']
                    if any(keyword in item.get('details', '') for keyword in keywords) or any(keyword in activity for keyword in keywords):
                        # Extract the training type from the activity, e.g., "资料分析-资料" -> "资料分析"
                        training_type = activity.split('-', 1)[0].strip()
                        
                        # Calculate duration to set a default target time
                        duration_minutes = 0
                        try:
                            start_t = datetime.strptime(item['start_time'], '%H:%M').time()
                            end_t = datetime.strptime(item['end_time'], '%H:%M').time()
                            duration_minutes = (datetime.combine(datetime.min, end_t) - datetime.combine(datetime.min, start_t)).total_seconds() / 60
                        except (ValueError, KeyError):
                            duration_minutes = 60 # Default to 60 mins if time is invalid

                        generated_goals.append({
                            "type": training_type,
                            "target_questions": 20,  # Default target questions
                            "target_accuracy": 75, # Default target accuracy
                            "target_time_minutes": int(duration_minutes)
                        })

                new_plan = StudyPlan(plan_date=schedule_date, goals=generated_goals)
                session.add(new_plan)
            
            session.commit()
            return jsonify(new_schedule.to_dict())
    except SQLAlchemyError as e:
        session.rollback()
        return jsonify({"error": f"Failed to get or create schedule: {e}"}), 500
    finally:
        session.close()



@app.route('/save_daily_schedule', methods=['POST'])
def save_daily_schedule():
    data = request.json
    schedule_date = data.get('schedule_date')
    schedule_items = data.get('schedule_items')

    if not schedule_date or not schedule_items:
        return jsonify({"error": "Date and schedule items are required"}), 400

    # --- Weekend-Only Validation for Mock Exams ---
    try:
        date_obj = datetime.strptime(schedule_date, '%Y-%m-%d')
        # Monday is 0 and Sunday is 6. We want to allow Saturday (5) and Sunday (6).
        is_weekend = date_obj.weekday() >= 5 
        
        if not is_weekend:
            for item in schedule_items:
                activity = item.get('activity', '')
                if PracticeCategory.MOCK_EXAM.value in activity:
                    return jsonify({
                        "error": f"'{PracticeCategory.MOCK_EXAM.value}' 只能安排在周末。"
                    }), 400
    except ValueError:
        return jsonify({"error": "Invalid date format. Please use YYYY-MM-DD."}), 400

    session = CentralSession()
    try:
        schedule = session.query(DailySchedule).filter_by(schedule_date=schedule_date).first()
        if schedule:
            schedule.schedule_items = schedule_items
        else:
            schedule = DailySchedule(schedule_date=schedule_date, schedule_items=schedule_items)
            session.add(schedule)
        
        # After saving the schedule, automatically synchronize the study plan
        _synchronize_plan_from_schedule(session, schedule_date, schedule_items)
        
        session.commit()
        return jsonify({"status": "success", "schedule": schedule.to_dict()})
    except SQLAlchemyError as e:
        session.rollback()
        return jsonify({"error": f"Database error: {e}"}), 500
    finally:
        session.close()




@app.route('/adjust_schedule_with_ai', methods=['POST'])
def adjust_schedule_with_ai():
    data = request.json
    start_date_str = data.get('date')
    user_request = data.get('request')
    days_to_plan = 3

    if not start_date_str or not user_request:
        return jsonify({"error": "Date and user request are required"}), 400

    multi_day_data = {}
    start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()

    for i in range(days_to_plan):
        current_date = start_date + timedelta(days=i)
        current_date_str = current_date.strftime('%Y-%m-%d')
        session = CentralSession()
        try:
            plan = session.query(StudyPlan).filter_by(plan_date=current_date_str).first()
            schedule = session.query(DailySchedule).filter_by(schedule_date=current_date_str).first()
            multi_day_data[current_date_str] = {
                "study_goals": plan.goals if plan else [],
                "detailed_schedule": schedule.schedule_items if schedule else []
            }
        finally:
            session.close()

    combined_data_for_ai = {
        "multi_day_plan": multi_day_data,
        "historical_performance": []
    }

    # Enqueue the AI adjustment task. Using a string reference to the task
    # can help prevent issues with stale code in worker processes.
    ai_job = q.enqueue(
        'tasks.prepare_and_run_schedule_adjustment',
        combined_data_for_ai,
        user_request,
        start_date_str,
        result_ttl=3600
    )

    # Enqueue the database saving task, which depends on the successful
    # completion of the AI adjustment task. The result of the first task
    # will be passed as the first argument to the second task.
    db_job = q.enqueue(
        'tasks.save_adjusted_schedule_to_db',
        depends_on=ai_job,
        result_ttl=3600
    )

    return jsonify({"job_id": db_job.id})

@app.route('/adjust_schedule_status/<job_id>', methods=['GET'])
def adjust_schedule_status(job_id):
    job = q.fetch_job(job_id)
    if job is None:
        return jsonify({'status': 'not found'}), 404
    
    response = {
        'status': job.get_status(),
        'result': job.result,
    }
    return jsonify(response)

@app.route('/get_training_details', methods=['GET'])
def get_training_details():
    """
    Fetches a single, specific goal by its ID (index) for a given date
    and calculates its progress, including dynamic calculation for review goals.
    """
    date_str = request.args.get('date')
    goal_id_str = request.args.get('goal_id')

    print(f"\n[DEBUG] --- Processing /get_training_details for date={date_str}, goal_id={goal_id_str} ---")

    if not date_str or not goal_id_str:
        return jsonify({"error": "Date and goal_id parameters are required"}), 400

    try:
        goal_id = int(goal_id_str)
    except ValueError:
        return jsonify({"error": "Invalid goal_id format"}), 400

    # 1. Fetch the study plan and schedule
    central_session = CentralSession()
    try:
        plan = central_session.query(StudyPlan).filter_by(plan_date=date_str).first()
        schedule = central_session.query(DailySchedule).filter_by(schedule_date=date_str).first()
    finally:
        central_session.close()

    # 2. Find the specific goal by its index
    if not plan or not plan.goals or goal_id >= len(plan.goals):
        return jsonify({"error": "Goal not found"}), 404
    
    # Make a copy to avoid modifying the original plan object
    goal = dict(plan.goals[goal_id])
    print(f"[DEBUG] Found goal: {goal}")
    practice_type_str = goal.get('type')

    # --- Dynamic Recalculation for Review Goals ---
    is_review_goal = '复盘' in practice_type_str or '错题' in practice_type_str
    if is_review_goal and schedule and schedule.schedule_items:
        schedule.schedule_items.sort(key=lambda x: x.get('start_time', ''))
        training_items = [
            item for item in schedule.schedule_items 
            if any(k in item.get('activity', '') for k in ['训练', '学习', '测试', '复盘', '模考', '专项', '言语', '数量', '判断', '资料', '常识'])
        ]
        print(f"[DEBUG] Found {len(training_items)} training items in schedule.")

        if goal_id < len(training_items):
            current_item = training_items[goal_id]
            print(f"[DEBUG] Matched with schedule item: {current_item}")
            details_text = current_item.get('details', '')
            print(f"[DEBUG] Extracted details text: '{details_text}'")
            
            target_count = 0

            # --- UNIFIED REVIEW LOGIC ---
            # 1. Define the time window for this review session.
            # It starts from the end of the last review session, or the beginning of the day.
            last_review_end_time_str = f"{date_str} 00:00:00"
            for i in range(goal_id - 1, -1, -1):
                if '复盘' in plan.goals[i].get('type', '') or '错题' in plan.goals[i].get('type', ''):
                    if i < len(training_items):
                        last_review_end_time_str = f"{date_str} {training_items[i].get('end_time', '00:00')}:00"
                    break
            
            # The window ends at the start of the current review session.
            current_review_start_time_str = f"{date_str} {current_item.get('start_time', '23:59')}:00"
            print(f"[DEBUG] Review time window: ({last_review_end_time_str}, {current_review_start_time_str})")

            # 2. Determine which subjects to review based on the details text.
            # If no subjects are specified, all subjects within the time window will be reviewed.
            subjects_to_review = []
            keyword_to_canonical = {
                "言语": PracticeCategory.VERBAL_COMPREHENSION.value,
                "数量": PracticeCategory.QUANTITATIVE_RELATIONS.value,
                "判断": PracticeCategory.JUDGEMENT_REASONING.value,
                "资料": PracticeCategory.DATA_ANALYSIS.value,
                "常识": PracticeCategory.COMMON_SENSE.value,
                "图形": PracticeCategory.GRAPHICAL_REASONING.value,
            }
            for keyword, canonical_name in keyword_to_canonical.items():
                if keyword in details_text:
                    subjects_to_review.append(canonical_name)
            
            print(f"[DEBUG] Subjects to review based on details: {subjects_to_review or 'All'}")

            # 3. Fetch all records for the day and filter them by the time window and subject.
            all_records_full = get_todays_full_records(date_str)
            
            # --- FIX: Track record IDs that have already been counted in a review ---
            processed_record_ids = set()
            
            # Convert boundary times to datetime objects for correct comparison
            last_review_end_dt = datetime.strptime(last_review_end_time_str, '%Y-%m-%d %H:%M:%S')
            current_review_start_dt = datetime.strptime(current_review_start_time_str, '%Y-%m-%d %H:%M:%S')

            for record in all_records_full:
                record_id = record.get('id')
                if record_id in processed_record_ids:
                    continue # Skip records already counted in a previous review session on the same day

                record_time_str = record.get('submission_time', '')
                record_type = record.get('practice_type')
                
                # Parse the record's timestamp into a datetime object
                record_time_dt = None
                if record_time_str:
                    try:
                        normalized_ts = record_time_str.replace('.', '-')
                        try:
                            record_time_dt = datetime.strptime(normalized_ts, '%Y-%m-%d %H:%M:%S')
                        except ValueError:
                            record_time_dt = datetime.strptime(normalized_ts, '%Y-%m-%d %H:%M')
                    except ValueError:
                        # This warning is kept as it's useful for identifying data format issues.
                        print(f"[WARNING] Could not parse record timestamp during review calculation: {record_time_str}")
                        continue

                # --- ENHANCED DEBUGGING AND LOGIC ---
                is_in_time_window = record_time_dt and last_review_end_dt <= record_time_dt < current_review_start_dt
                
                # Detailed trace log for every record checked
                print(f"[TRACE] Checking Record: type='{record_type}', time='{record_time_dt}'. Window: ['{last_review_end_dt}', '{current_review_start_dt}'). In window? {is_in_time_window}")

                if is_in_time_window:
                    is_subject_match = not subjects_to_review or record.get('practice_type') in subjects_to_review
                    
                    # Log subject match result only if it's in the time window
                    print(f"[TRACE] Record is in time window. Subjects to review: {subjects_to_review or 'All'}. Subject match? {is_subject_match}")

                    if is_subject_match:
                        incorrect = record.get('incorrect_answers', 0) or 0
                        unanswered = record.get('unanswered_questions', 0) or 0
                        target_count += incorrect + unanswered
                        
                        # Mark this record as processed for subsequent review goals on the same day
                        processed_record_ids.add(record_id)
                        
                        print(f"[TRACE] Match FOUND. incorrect={incorrect}, unanswered={unanswered}. New target_count={target_count}")
            
            goal['target_questions'] = target_count
            print(f"[DEBUG] Set target_questions for this goal to: {target_count}")

    # 3. Calculate progress for this specific goal (using the potentially updated target)
    # --- NEW TIME-AWARE PROGRESS CALCULATION FOR A SINGLE GOAL ---
    answered_for_this_goal = 0
    target_questions = goal.get('target_questions', 0)

    if schedule and schedule.schedule_items:
        # Align goals with their corresponding schedule items that are training-related
        schedule.schedule_items.sort(key=lambda x: x.get('start_time', ''))
        training_items = [
            item for item in schedule.schedule_items 
            if any(k in item.get('activity', '') for k in ['训练', '学习', '测试', '复盘', '模考', '专项', '言语', '数量', '判断', '资料', '常识'])
        ]

        # Find the time window for the current goal from the schedule
        if goal_id < len(training_items):
            current_item = training_items[goal_id]
            start_time_str = f"{date_str} {current_item.get('start_time', '00:00')}:00"
            end_time_str = f"{date_str} {current_item.get('end_time', '23:59')}:59"

            # Convert schedule times to datetime objects for accurate comparison
            start_time_dt = datetime.strptime(start_time_str, '%Y-%m-%d %H:%M:%S')
            end_time_dt = datetime.strptime(end_time_str, '%Y-%m-%d %H:%M:%S')

            # Get all records for the day and filter by time and type
            all_todays_records = get_todays_full_records(date_str)
            for record in all_todays_records:
                record_type = record.get('practice_type')
                record_time_str = record.get('submission_time')
                
                if record_type == practice_type_str and record_time_str:
                    record_time_dt = None
                    try:
                        # Normalize and parse timestamp, trying formats with and without seconds
                        normalized_ts = record_time_str.replace('.', '-')
                        try:
                            record_time_dt = datetime.strptime(normalized_ts, '%Y-%m-%d %H:%M:%S')
                        except ValueError:
                            record_time_dt = datetime.strptime(normalized_ts, '%Y-%m-%d %H:%M')
                    except ValueError:
                        print(f"Could not parse record timestamp for training details: {record_time_str}")
                        continue
                    
                    # Perform the comparison using datetime objects
                    if record_time_dt and start_time_dt <= record_time_dt <= end_time_dt:
                        answered_for_this_goal += record.get('questions_answered', 0)

    percentage = 0
    if target_questions > 0:
        percentage = min(100, round((answered_for_this_goal / target_questions) * 100))
    elif answered_for_this_goal > 0:
        percentage = 100

    # 4. Return the single goal with its calculated progress
    print(f"[DEBUG] Final goal data being returned: {goal}")
    print(f"[DEBUG] --- Finished processing goal_id={goal_id_str} ---")
    return jsonify({
        "goal": goal,
        "progress": {
            "answered_questions": answered_for_this_goal,
            "percentage": percentage
        }
    })


@app.route('/analysis')
def analysis():
    all_data = get_all_history_data()
    return render_template('analysis.html', all_data=json.dumps(all_data))


@app.route('/analyze_all_data', methods=['POST'])
def analyze_all_data():
    """
    Triggers a robust, backend-driven AI analysis of all historical data.
    """
    # This endpoint no longer receives data. It triggers a backend process.
    # The new wrapper function handles data prep and AI analysis in a subprocess.
    from tasks import prepare_and_run_dashboard_analysis
    
    # Enqueue the main task runner which now handles data prep, AI analysis,
    # WeChat notification, and markdown conversion all in one clean subprocess.
    job = q.enqueue(prepare_and_run_dashboard_analysis, result_ttl=3600)
    
    return jsonify({"job_id": job.id})

@app.route('/dashboard_analysis_status/<job_id>', methods=['GET'])
def dashboard_analysis_status(job_id):
    job = q.fetch_job(job_id)
    if job is None:
        return jsonify({'status': 'not found'}), 404
    
    response = {
        'status': job.get_status(),
        'result': job.result,
    }
    return jsonify(response)

@app.route('/test_wechat_push', methods=['POST'])
def test_wechat_push():
    try:
        test_message = """
        ### 企业微信推送测试

        **状态**: <font color="info">成功</font>
        > 这是一条测试消息，用于验证您的Webhook配置是否正确。
        """
        # 这里我们直接调用函数，因为它会处理所有内部逻辑
        send_wechat_message(test_message)
        return jsonify({"message": "测试消息已发送，请在企业微信中查看。"}), 200
    except Exception as e:
        return jsonify({"error": f"发送失败: {e}"}), 500

@app.route('/get_generated_prompts', methods=['GET'])
def get_generated_prompts():
    """Fetches the content of the generated prompt files."""
    try:
        with open('user_data/course_schedule/generated_prompt.txt', 'r', encoding='utf-8') as f:
            course_prompt = f.read()
    except FileNotFoundError:
        course_prompt = ""
    
    try:
        with open('user_data/exam_requirements/generated_prompt.txt', 'r', encoding='utf-8') as f:
            exam_prompt = f.read()
    except FileNotFoundError:
        exam_prompt = ""
        
    return jsonify({"course_prompt": course_prompt, "exam_prompt": exam_prompt})

@app.route('/upload_prompt_data', methods=['POST'])
def upload_prompt_data():
    """Handles file uploads for either course schedule or exam requirements."""
    upload_type = request.args.get('type')
    if upload_type not in ['course_schedule', 'exam_requirements']:
        return jsonify({"error": "Invalid upload type specified"}), 400

    files = request.files.getlist('files')
    additional_info = request.form.get('additional_info', '')

    if not files or all(f.filename == '' for f in files):
        return jsonify({"error": "No files selected for upload"}), 400

    temp_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'temp_prompt_files')
    os.makedirs(temp_dir, exist_ok=True)

    file_paths = []
    for file in files:
        if file:
            filename = secure_filename(file.filename)
            file_path = os.path.join(temp_dir, f"{upload_type}_{filename}")
            file.save(file_path)
            file_paths.append(file_path)

    # Enqueue the updated task which now takes the upload_type
    job = q.enqueue(generate_prompts_from_files_task, upload_type, file_paths, additional_info, result_ttl=3600)
    
    return jsonify({"task_id": job.id})

@app.route('/task_status/<task_id>', methods=['GET'])
def task_status(task_id):
    job = q.fetch_job(task_id)
    if job is None:
        return jsonify({'status': 'not found'}), 404
    
    if job.is_finished:
        # Assuming the task result is a dictionary with a message
        result = job.result or {}
        response_data = {
            'status': '处理完成',
            'progress': 100,
            'message': result.get('message', '任务成功完成！')
        }
    elif job.is_failed:
        response_data = {
            'status': '处理失败',
            'progress': job.meta.get('progress', 0),
            'error': str(job.exc_info)
        }
    else: # In progress or queued
        response_data = {
            'status': job.meta.get('status', '正在处理...'),
            'progress': job.meta.get('progress', 0)
        }
        
    return jsonify(response_data)

@app.route('/get_automation_settings', methods=['GET'])
def get_automation_settings():
    session = CentralSession()
    try:
        settings = session.query(AutomationSettings).filter_by(task_name='general').first()
        if settings:
            return jsonify(settings.to_dict())
        else:
            # Return default settings if none are found
            return jsonify({
                "enabled": {"comprehensive_analysis": False, "data_analysis": False, "daily_plan": False},
                "execution_time": {"comprehensive_analysis": "22:00", "data_analysis": "22:00", "daily_plan": "23:00"}
            })
    finally:
        session.close()

@app.route('/save_automation_settings', methods=['POST'])
def save_automation_settings():
    data = request.json
    session = CentralSession()
    try:
        settings = session.query(AutomationSettings).filter_by(task_name='general').first()
        if settings:
            settings.enabled = data.get('enabled', settings.enabled)
            settings.execution_time = data.get('execution_time', settings.execution_time)
        else:
            settings = AutomationSettings(
                task_name='general',
                enabled=data.get('enabled'),
                execution_time=data.get('execution_time')
            )
            session.add(settings)
        session.commit()
        return jsonify({"status": "success"})
    except SQLAlchemyError as e:
        session.rollback()
        return jsonify({"error": f"Database error: {e}"}), 500
    finally:
        session.close()


@app.route('/file_manager')
def file_manager():
    return render_template('file_manager.html')

@app.route('/api/files', methods=['GET'])
def list_files():
    directory = request.args.get('path', '.')
    base_path = os.path.abspath(os.path.join(os.path.dirname(__file__)))
    
    if directory == 'database':
        current_path = os.path.join(base_path, 'database')
    elif directory.startswith('database/'):
        current_path = os.path.join(base_path, directory)
    elif directory == 'uploads':
        current_path = os.path.join(base_path, 'uploads')
    elif directory.startswith('uploads/'):
        current_path = os.path.join(base_path, directory)
    else:
        return jsonify({"error": "Access denied"}), 403

    if not os.path.exists(current_path) or not os.path.isdir(current_path):
        return jsonify({"error": "Directory not found"}), 404

    files_list = []
    for item in os.listdir(current_path):
        item_path = os.path.join(current_path, item)
        rel_path = os.path.relpath(item_path, base_path)
        
        file_info = {
            "name": item,
            "path": rel_path,
            "is_dir": os.path.isdir(item_path)
        }
        files_list.append(file_info)
        
    return jsonify(files_list)

@app.route('/api/file', methods=['GET'])
def get_file_content():
    file_path = request.args.get('path')
    if not file_path:
        return jsonify({"error": "File path is required"}), 400

    base_path = os.path.abspath(os.path.join(os.path.dirname(__file__)))
    full_path = os.path.abspath(os.path.join(base_path, file_path))

    if not full_path.startswith(os.path.join(base_path, 'database')) and not full_path.startswith(os.path.join(base_path, 'uploads')):
        return jsonify({"error": "Access denied"}), 403

    try:
        if os.path.isdir(full_path):
            return jsonify({"error": "Cannot view content of a directory"}), 400
        
        if file_path.endswith('.db'):
            engine = create_engine(f"sqlite:///{full_path}")
            inspector = inspect(engine)
            tables = inspector.get_table_names()
            return jsonify({"type": "database", "tables": tables, "path": file_path})

        content = extract_text_from_file(full_path)
        return jsonify({"type": "text", "content": content})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/file/delete', methods=['POST'])
def delete_file():
    data = request.json
    file_path = data.get('path')
    if not file_path:
        return jsonify({"error": "File path is required"}), 400

    base_path = os.path.abspath(os.path.join(os.path.dirname(__file__)))
    full_path = os.path.abspath(os.path.join(base_path, file_path))

    if not full_path.startswith(os.path.join(base_path, 'database')) and not full_path.startswith(os.path.join(base_path, 'uploads')):
        return jsonify({"error": "Access denied"}), 403
    
    try:
        if os.path.isdir(full_path):
            # For simplicity, we'll prevent deleting non-empty directories
            if not os.listdir(full_path):
                os.rmdir(full_path)
                return jsonify({"status": "success", "message": "Directory deleted successfully."})
            else:
                return jsonify({"error": "Directory is not empty"}), 400
        else:
            os.remove(full_path)
            return jsonify({"status": "success", "message": "File deleted successfully."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/db/table_data', methods=['GET'])
def get_table_data():
    db_path = request.args.get('path')
    table_name = request.args.get('table')
    if not db_path or not table_name:
        return jsonify({"error": "Database path and table name are required"}), 400

    base_path = os.path.abspath(os.path.dirname(__file__))
    full_path = os.path.abspath(os.path.join(base_path, db_path))

    if not full_path.startswith(os.path.join(base_path, 'database')):
        return jsonify({"error": "Access denied"}), 403

    try:
        engine = create_engine(f"sqlite:///{full_path}")
        with engine.connect() as connection:
            df = pd.read_sql_table(table_name, connection)
            data = df.to_dict(orient='records')
            columns = df.columns.tolist()
            # Find primary key
            inspector = inspect(engine)
            pk_constraint = inspector.get_pk_constraint(table_name)
            primary_key = pk_constraint['constrained_columns'][0] if pk_constraint['constrained_columns'] else None
            return jsonify({"columns": columns, "rows": data, "primary_key": primary_key})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/db/update_row', methods=['POST'])
def update_db_row():
    data = request.json
    db_path = data.get('path')
    table_name = data.get('table')
    row_data = data.get('row_data')
    primary_key = data.get('primary_key')
    
    if not all([db_path, table_name, row_data, primary_key]):
        return jsonify({"error": "Path, table name, row data, and primary key are required"}), 400

    base_path = os.path.abspath(os.path.dirname(__file__))
    full_path = os.path.abspath(os.path.join(base_path, db_path))

    if not full_path.startswith(os.path.join(base_path, 'database')):
        return jsonify({"error": "Access denied"}), 403

    try:
        engine = create_engine(f"sqlite:///{full_path}")
        with engine.connect() as connection:
            pk_value = row_data.get(primary_key)
            if pk_value is None:
                 return jsonify({"error": f"Primary key '{primary_key}' not found in row data."}), 400

            set_clause = ", ".join([f'"{col}" = :{col}' for col in row_data.keys() if col != primary_key])
            stmt = text(f'UPDATE "{table_name}" SET {set_clause} WHERE "{primary_key}" = :{primary_key}')
            
            with connection.begin():
                connection.execute(stmt, row_data)

        return jsonify({"status": "success", "message": "Row updated successfully."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/db/delete_row', methods=['POST'])
def delete_db_row():
    data = request.json
    db_path = data.get('path')
    table_name = data.get('table')
    primary_key = data.get('primary_key')
    pk_value = data.get('pk_value')

    if not all([db_path, table_name, primary_key, pk_value is not None]):
        return jsonify({"error": "Path, table name, primary key, and pk value are required"}), 400

    base_path = os.path.abspath(os.path.dirname(__file__))
    full_path = os.path.abspath(os.path.join(base_path, db_path))

    if not full_path.startswith(os.path.join(base_path, 'database')):
        return jsonify({"error": "Access denied"}), 403

    try:
        engine = create_engine(f"sqlite:///{full_path}")
        with engine.connect() as connection:
            stmt = text(f'DELETE FROM "{table_name}" WHERE "{primary_key}" = :pk_value')
            with connection.begin():
                connection.execute(stmt, {"pk_value": pk_value})

        return jsonify({"status": "success", "message": "Row deleted successfully."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Run the Flask application.')
    parser.add_argument('--port', type=int, default=5002,
                        help='The port to run the application on.')
    args = parser.parse_args()
    app.run(debug=True, port=args.port)
