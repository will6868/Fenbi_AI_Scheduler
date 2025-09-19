from datetime import datetime, timedelta
from models import StudyPlan, PracticeCategory, DailySchedule, AnalysisResult, CATEGORY_TO_FOLDER
from database import CentralSession, get_db_session
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

def _synchronize_plan_from_schedule(session, schedule_date, schedule_items):
    """
    Analyzes a list of schedule items, creates a distinct goal for each training item,
    and updates or creates a StudyPlan for the given date.
    """
    plan = session.query(StudyPlan).filter_by(plan_date=schedule_date).first()
    
    final_goals = []

    # Sort schedule items by time to ensure goals are in chronological order
    schedule_items.sort(key=lambda x: x.get('start_time', ''))

    for item in schedule_items:
        activity = item.get('activity', '')
        
        training_type = None
        # Define keywords that identify a study-related activity
        keywords = ['训练', '学习', '测试', '复盘', '模考', '专项', '言语', '数量', '判断', '资料', '常识']
        
        if any(keyword in activity for keyword in keywords):
            # Extract the specific subject from the activity string, e.g., "资料分析-资料" -> "资料分析"
            training_type = activity.split('-', 1)[0].strip()

            if training_type:
                # --- NEW: Standardize simplified names to their canonical enum values ---
                # This ensures data is saved to and read from the correct, standardized folder.
                name_to_canonical = {
                    "言语理解": PracticeCategory.VERBAL_COMPREHENSION.value,
                    "数量关系": PracticeCategory.QUANTITATIVE_RELATIONS.value,
                    "判断推理": PracticeCategory.JUDGEMENT_REASONING.value,
                    "资料分析": PracticeCategory.DATA_ANALYSIS.value,
                    "常识判断": PracticeCategory.COMMON_SENSE.value,
                    "图形推理": PracticeCategory.GRAPHICAL_REASONING.value,
                    "专项智能": PracticeCategory.SPECIAL_INTELLIGENT_PRACTICE.value,
                    "行测全真模拟考试": PracticeCategory.MOCK_EXAM.value,
                    "模考": PracticeCategory.MOCK_EXAM.value
                }
                # Use the full canonical name if a match is found, otherwise use the type as-is.
                training_type = name_to_canonical.get(training_type, training_type)

                # Standardize mock exam name (serves as a fallback)
                if PracticeCategory.MOCK_EXAM.value in training_type:
                    training_type = PracticeCategory.MOCK_EXAM.value

                # Calculate duration of the activity
                duration_minutes = 0
                try:
                    start_t = datetime.strptime(item['start_time'], '%H:%M').time()
                    end_t = datetime.strptime(item['end_time'], '%H:%M').time()
                    duration = (datetime.combine(datetime.min, end_t) - datetime.combine(datetime.min, start_t))
                    duration_minutes = max(0, duration.total_seconds() / 60) # Ensure non-negative
                except (ValueError, KeyError):
                    duration_minutes = 60 # Default to 60 mins if time is missing or invalid

                # Create a new, distinct goal for each training item
                # For now, we use default values for questions and accuracy.
                # A future enhancement could be a UI to set these per schedule item.
                final_goals.append({
                    "type": training_type,
                    "target_questions": 20,
                    "target_accuracy": 75,
                    "target_time_minutes": int(duration_minutes)
                })

    # Update or create the study plan in the database
    if not plan:
        plan = StudyPlan(plan_date=schedule_date, goals=final_goals)
        session.add(plan)
    else:
        plan.goals = final_goals

def get_all_history_data():
    """
    Scans all category databases and returns a list of all records.
    """
    all_results = []
    base_dir = 'database'
    
    for folder_name in os.listdir(base_dir):
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
    
    all_results.sort(key=lambda x: x.get('submission_time', ''), reverse=True)
    return all_results

def get_recent_history(days=7):
    """
    Fetches all analysis results from the last N days.
    """
    all_results = []
    base_dir = 'database'
    end_date = datetime.now().date()
    start_date = end_date - timedelta(days=days)

    for i in range((end_date - start_date).days + 1):
        current_date = start_date + timedelta(days=i)
        date_str = current_date.strftime('%Y-%m-%d')
        
        for folder_name in CATEGORY_TO_FOLDER.values():
            db_path = os.path.join(base_dir, folder_name, f"{date_str}.db")
            if os.path.exists(db_path):
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
    
    all_results.sort(key=lambda x: x.get('submission_time', ''), reverse=True)
    return all_results

def get_schedule_and_history_for_ai(start_date_str, days=3):
    """
    Fetches the multi-day schedule and recent performance history
    required for the AI schedule adjustment task.
    """
    multi_day_data = {}
    start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()

    for i in range(days):
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

    # Fetch recent historical performance
    historical_performance = get_recent_history(days=7)

    combined_data = {
        "multi_day_plan": multi_day_data,
        "historical_performance": historical_performance
    }
    return combined_data
