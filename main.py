from flask import Flask, render_template, request, redirect, url_for, jsonify, Response
import os
os.environ['no_proxy'] = '*'
import json
from werkzeug.utils import secure_filename
from ai_analyzer import save_ai_config, test_ai_connection_stream, adjust_schedule_from_text, get_comprehensive_analysis, analyze_image_with_ai
from pdf_processor import extract_text_from_pdf
from image_processor import extract_data_from_image
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timedelta

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads'
# Use an absolute path for the database to ensure consistency
db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'instance', 'analysis_data.db')
os.makedirs(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'instance'), exist_ok=True)
app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'pdf'}

# --- Database Models ---
class PracticeSession(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    practice_type = db.Column(db.String(200), nullable=False)
    total_questions = db.Column(db.Integer, nullable=False)
    correct_answers = db.Column(db.Integer, nullable=False)
    incorrect_answers = db.Column(db.Integer, nullable=False)
    unanswered = db.Column(db.Integer, nullable=False)
    accuracy = db.Column(db.Float, nullable=False)
    total_time = db.Column(db.String(50), nullable=True)
    submission_time = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    detailed_analysis_text = db.Column(db.Text, nullable=True) # New field for AI analysis
    questions = db.relationship('QuestionDetail', backref='session', lazy=True, cascade="all, delete-orphan")

    def __repr__(self):
        return f'<PracticeSession {self.id}>'

class QuestionDetail(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    question_number = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(20), nullable=False) # 'correct', 'incorrect', 'unanswered'
    question_text = db.Column(db.Text, nullable=True) # From PDF
    error_reason = db.Column(db.Text, nullable=True) # From PDF analysis
    session_id = db.Column(db.Integer, db.ForeignKey('practice_session.id'), nullable=False)

    def __repr__(self):
        return f'<QuestionDetail {self.question_number} - {self.status}>'

# This model seems to be used by the old /analyze and /get_history routes.
# We'll add it here to support the history chart.
class AnalysisResult(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    practice_type = db.Column(db.String(200), nullable=False)
    submission_time = db.Column(db.String(100), nullable=False)
    difficulty = db.Column(db.Float)
    accuracy_rate = db.Column(db.String(20))
    total_questions = db.Column(db.Integer)
    correct_answers = db.Column(db.Integer)
    incorrect_answers = db.Column(db.Integer)
    unanswered = db.Column(db.Integer)
    total_time_minutes = db.Column(db.Integer)
    average_time_per_answered_question = db.Column(db.Float)
    incorrect_question_numbers = db.Column(db.JSON)
    ability_analysis = db.Column(db.JSON)
    answer_card = db.Column(db.JSON)

    def to_dict(self):
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}

# Define the database model for study plans
class StudyPlan(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    plan_date = db.Column(db.String(50), nullable=False, unique=True) # YYYY-MM-DD
    goals = db.Column(db.JSON) # Store goals like [{"type": "资料分析", "target_questions": 20, "target_accuracy": 75, "target_time_minutes": 30}, ...]

    def to_dict(self):
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}

# Define the database model for daily schedules
class DailySchedule(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    schedule_date = db.Column(db.String(50), nullable=False, unique=True) # YYYY-MM-DD
    schedule_items = db.Column(db.JSON) # Store schedule like [{"time": "07:00", "activity": "起床"}, ...]

    def to_dict(self):
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/upload', methods=['POST'])
def upload_file():
    """Handles file uploads, saves them, and returns the list of filenames."""
    if 'files[]' not in request.files:
        return jsonify({"error": "No file part"}), 400
    
    files = request.files.getlist('files[]')
    uploaded_filenames = []

    for file in files:
        if file.filename == '':
            continue
        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(filepath)
            uploaded_filenames.append(filename)
        else:
            return jsonify({"error": f"Invalid file type: {file.filename}"}), 400
            
    return jsonify({"uploaded_files": uploaded_filenames})

@app.route('/analyze', methods=['POST'])
def analyze_files():
    """
    Receives filenames (currently handles the first one), gets AI analysis, 
    saves the data to the database, and returns a success response with the new session ID.
    """
    data = request.get_json()
    filenames = data.get('filenames')

    if not filenames:
        return jsonify({"error": "No filenames provided"}), 400

    # For now, we process only the first file as per the new logic.
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filenames[0])
    
    if not os.path.exists(filepath):
        return jsonify({"error": f"File not found: {filepath}"}), 404

    try:
        # Call the correct, non-streaming analysis function
        analysis_result = analyze_image_with_ai(filepath)

        if "error" in analysis_result:
            return jsonify(analysis_result), 500

        # --- Map the flat JSON to the database models ---
        
        # Safely get numeric values, defaulting to 0
        total_questions = analysis_result.get('total_questions', 0)
        correct_answers = analysis_result.get('correct_answers', 0)
        
        # Calculate accuracy
        accuracy = 0
        if total_questions > 0:
            accuracy = round((correct_answers / total_questions) * 100, 2)

        # Convert submission time string to datetime object
        try:
            submission_dt = datetime.strptime(analysis_result.get('submission_time'), '%Y.%m.%d %H:%M')
        except (ValueError, TypeError):
            submission_dt = datetime.utcnow() # Fallback

        # Structure the detailed analysis for storage
        detailed_analysis = {
            "ability_analysis": analysis_result.get("ability_analysis", {}),
            "difficulty": analysis_result.get("difficulty", "N/A")
        }
        analysis_text = json.dumps(detailed_analysis, ensure_ascii=False, indent=2)

        new_session = PracticeSession(
            practice_type=analysis_result.get('practice_type', '未知类型'),
            total_questions=total_questions,
            correct_answers=correct_answers,
            incorrect_answers=analysis_result.get('incorrect_answers', 0),
            unanswered=analysis_result.get('unanswered', 0),
            accuracy=accuracy,
            total_time=str(analysis_result.get('total_time_minutes', 'N/A')), # DB expects string
            submission_time=submission_dt,
            detailed_analysis_text=analysis_text
        )

        db.session.add(new_session)
        
        # --- Also save a record to AnalysisResult for history chart compatibility ---
        # This is a temporary measure to bridge the two data models.
        # Ideally, the frontend chart would be updated to use the new PracticeSession model.
        try:
            legacy_result = AnalysisResult(
                practice_type=analysis_result.get('practice_type', '未知类型'),
                submission_time=analysis_result.get('submission_time'),
                difficulty=analysis_result.get('difficulty'),
                accuracy_rate=f"{accuracy}%",
                total_questions=total_questions,
                correct_answers=correct_answers,
                incorrect_answers=analysis_result.get('incorrect_answers', 0),
                unanswered=analysis_result.get('unanswered', 0),
                total_time_minutes=analysis_result.get('total_time_minutes'),
                average_time_per_answered_question=analysis_result.get('average_time_per_answered_question'),
                incorrect_question_numbers=analysis_result.get('incorrect_question_numbers'),
                ability_analysis=analysis_result.get('ability_analysis'),
                answer_card=analysis_result.get('answer_card')
            )
            db.session.add(legacy_result)
        except Exception as e:
            print(f"Could not save legacy AnalysisResult: {e}")
            # Do not block the main operation if this fails
            pass

        db.session.flush() # Flush to get the new_session.id for the foreign key

        # Populate QuestionDetail from the answer_card
        answer_card = analysis_result.get('answer_card', {})
        for q_num, status in answer_card.items():
            new_question = QuestionDetail(
                question_number=int(q_num),
                status=status,
                session_id=new_session.id
            )
            db.session.add(new_question)

        db.session.commit()

        return jsonify({"status": "success", "session_id": new_session.id})

    except Exception as e:
        db.session.rollback()
        return jsonify({"error": "An unexpected error occurred during analysis or DB operation.", "details": str(e)}), 500

@app.route('/results/<int:session_id>')
def show_results(session_id):
    """Displays the analysis results for a given session."""
    session = PracticeSession.query.get_or_404(session_id)
    # The detailed analysis is stored as a JSON string, so we parse it back
    analysis_data = json.loads(session.detailed_analysis_text) if session.detailed_analysis_text else {}
    return render_template('results.html', session=session, analysis=analysis_data)

@app.route('/get_history', methods=['GET'])
def get_history():
    """Fetches historical analysis results for the chart."""
    # This route uses the AnalysisResult model.
    # Ensure that data is being saved to this table for the chart to work.
    results = AnalysisResult.query.order_by(AnalysisResult.submission_time.asc()).all()
    return jsonify([res.to_dict() for res in results])

@app.route('/save_settings', methods=['POST'])
def save_settings():
    data = request.get_json()
    save_ai_config(data)
    return jsonify({"status": "success"})

@app.route('/test_ai', methods=['POST'])
def test_ai():
    return Response(test_ai_connection_stream(), content_type='text/plain; charset=utf-8')

@app.route('/ping', methods=['GET'])
def ping():
    return "pong"

# --- Schedule and Planning Routes ---

@app.route('/schedule')
def schedule():
    return render_template('schedule.html')

@app.route('/get_daily_schedule', methods=['GET'])
def get_daily_schedule():
    schedule_date = request.args.get('date')
    print(f"\n--- Received request for /get_daily_schedule ---")
    print(f"Date parameter: {schedule_date}")

    if not schedule_date:
        print("Error: Date parameter is missing.")
        return jsonify({"error": "Date parameter is required"}), 400
    
    try:
        schedule = DailySchedule.query.filter_by(schedule_date=schedule_date).first()
        
        if schedule:
            print(f"Found schedule in DB for {schedule_date}.")
            print(f"Data: {schedule.to_dict()}")
            return jsonify(schedule.to_dict())
        else:
            print(f"No schedule found for {schedule_date}. Returning default schedule.")
            default_schedule = {
                "schedule_date": schedule_date,
                "schedule_items": [
                    { "time": "07:00", "activity": "起床和早餐" },
                    { "time": "09:00", "activity": "训练 - 资料分析 (基础技巧与速算训练) (60分钟)" },
                    { "time": "15:00", "activity": "训练 - 资料分析 (限时完成训练) (60分钟)" },
                    { "time": "22:30", "activity": "睡觉" }
                ]
            }
            print(f"Default data: {default_schedule}")
            return jsonify(default_schedule)
            
    except Exception as e:
        print(f"!!! An exception occurred: {e}")
        # Log the full traceback for detailed debugging
        import traceback
        traceback.print_exc()
        return jsonify({"error": "An internal server error occurred."}), 500

@app.route('/save_daily_schedule', methods=['POST'])
def save_daily_schedule():
    data = request.json
    schedule_date = data.get('schedule_date')
    schedule_items = data.get('schedule_items')

    if not schedule_date or not schedule_items:
        return jsonify({"error": "Date and schedule items are required"}), 400

    schedule = DailySchedule.query.filter_by(schedule_date=schedule_date).first()
    if schedule:
        schedule.schedule_items = schedule_items
    else:
        schedule = DailySchedule(schedule_date=schedule_date, schedule_items=schedule_items)
        db.session.add(schedule)
    
    db.session.commit()
    return jsonify({"status": "success", "schedule": schedule.to_dict()})

@app.route('/adjust_schedule_with_ai', methods=['POST'])
def adjust_schedule_with_ai():
    data = request.json
    start_date_str = data.get('date')
    user_request = data.get('request')
    days_to_plan = 7 # Look ahead 7 days

    if not start_date_str or not user_request:
        return jsonify({"error": "Date and user request are required"}), 400

    # 1. Fetch historical performance data
    try:
        history_sessions = PracticeSession.query.order_by(PracticeSession.submission_time.desc()).limit(10).all()
        history_data = [{
            "practice_type": s.practice_type,
            "accuracy": s.accuracy,
            "total_time": s.total_time,
            "submission_time": s.submission_time.strftime('%Y-%m-%d %H:%M'),
            "analysis": json.loads(s.detailed_analysis_text) if s.detailed_analysis_text else {}
        } for s in history_sessions]
    except Exception as e:
        print(f"Error fetching history data: {e}")
        history_data = []

    # 2. Fetch schedule and plan data for the next N days
    multi_day_data = {}
    start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
    for i in range(days_to_plan):
        current_date = start_date + timedelta(days=i)
        current_date_str = current_date.strftime('%Y-%m-%d')
        
        plan = StudyPlan.query.filter_by(plan_date=current_date_str).first()
        schedule = DailySchedule.query.filter_by(schedule_date=current_date_str).first()
        
        multi_day_data[current_date_str] = {
            "study_goals": plan.goals if plan else [],
            "detailed_schedule": schedule.schedule_items if schedule else []
        }

    # 3. Call the AI function with the combined data
    # We will modify the `adjust_schedule_from_text` function in ai_analyzer.py to handle this new data structure
    combined_data_for_ai = {
        "multi_day_plan": multi_day_data,
        "historical_performance": history_data
    }
    
    ai_result = adjust_schedule_from_text(combined_data_for_ai, user_request)

    if "error" in ai_result:
        return jsonify(ai_result), 500

    # 4. Process the multi-day response and update the database
    updated_schedules = ai_result.get('updated_schedules', {})
    for date_str, new_items in updated_schedules.items():
        schedule = DailySchedule.query.filter_by(schedule_date=date_str).first()
        if schedule:
            schedule.schedule_items = new_items
        else:
            new_schedule = DailySchedule(schedule_date=date_str, schedule_items=new_items)
            db.session.add(new_schedule)
    
    db.session.commit()

    response_data = {
        "suggestion": ai_result.get('suggestion'),
        "updated_schedule": updated_schedules.get(start_date_str)
    }

    return jsonify(response_data)

if __name__ == '__main__':
    # The db_path is now defined above, so we can reuse it.
    # We will also remove the logic that deletes the DB on every restart,
    # as it was for debugging and is not ideal for regular use.
    if not os.path.exists(app.config['UPLOAD_FOLDER']):
        os.makedirs(app.config['UPLOAD_FOLDER'])
        
    with app.app_context():
        db.create_all()
        
    app.run(debug=True, port=5001)
