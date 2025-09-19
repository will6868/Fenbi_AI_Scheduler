import os
from datetime import datetime
from main import app, db, PracticeSession, QuestionDetail
from image_processor import extract_data_from_image
from pdf_processor import extract_data_from_pdf

def run_test():
    """
    Tests the data processing and database storage logic using files
    from the 'uploads' directory.
    """
    with app.app_context():
        # Ensure the database and tables are created
        db.create_all()
        
        print("--- Starting Test ---")
        
        # 1. Define file paths
        base_dir = os.path.abspath(os.path.dirname(__file__))
        upload_dir = os.path.join(base_dir, 'uploads')
        
        image_files = [os.path.join(upload_dir, f) for f in os.listdir(upload_dir) if f.lower().endswith(('.png', '.jpg', '.jpeg'))]
        pdf_files = [os.path.join(upload_dir, f) for f in os.listdir(upload_dir) if f.lower().endswith('.pdf')]

        if not image_files or not pdf_files:
            print("Error: Missing image or PDF files in 'uploads' directory.")
            return

        print(f"Found images: {image_files}")
        print(f"Found PDFs: {pdf_files}")

        try:
            # 2. Extract data
            print("\n--- Extracting Data ---")
            summary_data = extract_data_from_image(image_files[0])
            print(f"Extracted from image: {summary_data}")
            
            all_question_details = []
            for pdf_path in pdf_files:
                details = extract_data_from_pdf(pdf_path)
                all_question_details.extend(details)
            print(f"Extracted from PDF: {len(all_question_details)} questions")

            # 3. Process and associate data
            print("\n--- Processing Data ---")
            correct_count = 0
            for detail in all_question_details:
                if str(detail.get("your_answer")) == str(detail.get("correct_answer")):
                    detail["status"] = "correct"
                    correct_count += 1
                else:
                    detail["status"] = "incorrect"
            
            total_questions = len(all_question_details)
            incorrect_count = total_questions - correct_count
            accuracy = round((correct_count / total_questions) * 100, 2) if total_questions > 0 else 0
            print(f"Calculated Metrics: Total={total_questions}, Correct={correct_count}, Accuracy={accuracy}%")

            # 4. Store in database
            print("\n--- Storing in Database ---")
            submission_dt = datetime.strptime(summary_data['submission_time'], '%Y.%m.%d %H:%M') if summary_data.get('submission_time') else datetime.utcnow()

            new_session = PracticeSession(
                practice_type=summary_data.get('practice_type', 'N/A'),
                total_questions=total_questions,
                correct_answers=correct_count,
                incorrect_answers=incorrect_count,
                unanswered=0, # Placeholder, as PDF data doesn't specify this
                accuracy=accuracy,
                total_time=f"{summary_data.get('total_time_minutes', 0)}分钟",
                submission_time=submission_dt
            )
            db.session.add(new_session)
            db.session.flush()

            for detail in all_question_details:
                new_question = QuestionDetail(
                    question_number=detail['question_number'],
                    status=detail['status'],
                    session_id=new_session.id
                )
                db.session.add(new_question)
            
            db.session.commit()
            print(f"Data for session {new_session.id} committed to the database.")

            # 5. Verify from database
            print("\n--- Verifying from Database ---")
            session = PracticeSession.query.get(new_session.id)
            if session:
                print(f"Successfully retrieved session {session.id}")
                print(f"  Type: {session.practice_type}")
                print(f"  Accuracy: {session.accuracy}%")
                print(f"  Questions in DB: {len(session.questions)}")
                print("Test PASSED!")
            else:
                print("Test FAILED: Could not retrieve session from database.")

        except Exception as e:
            db.session.rollback()
            print(f"\n--- An error occurred ---")
            print(f"Error: {e}")
            print("Test FAILED.")

if __name__ == '__main__':
    run_test()
