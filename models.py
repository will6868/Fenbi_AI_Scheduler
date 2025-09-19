import enum
import datetime
from sqlalchemy import Column, Integer, String, Float, JSON
from sqlalchemy.orm import declarative_base

Base = declarative_base()
CentralBase = declarative_base()

class PracticeCategory(enum.Enum):
    VERBAL_COMPREHENSION = "言语理解与表达"
    QUANTITATIVE_RELATIONS = "数量关系"
    JUDGEMENT_REASONING = "判断推理"
    DATA_ANALYSIS = "资料分析"
    COMMON_SENSE = "常识判断"
    GRAPHICAL_REASONING = "图形推理"
    SPECIAL_INTELLIGENT_PRACTICE = "专项智能练习"
    MOCK_EXAM = "行测全真模拟考试 (摸底测试)"

CATEGORY_TO_FOLDER = {
    PracticeCategory.VERBAL_COMPREHENSION: "verbal_comprehension",
    PracticeCategory.QUANTITATIVE_RELATIONS: "quantitative_relations",
    PracticeCategory.JUDGEMENT_REASONING: "judgement_reasoning",
    PracticeCategory.DATA_ANALYSIS: "data_analysis",
    PracticeCategory.COMMON_SENSE: "common_sense",
    PracticeCategory.GRAPHICAL_REASONING: "graphical_reasoning",
    PracticeCategory.SPECIAL_INTELLIGENT_PRACTICE: "special_intelligent_practice",
    PracticeCategory.MOCK_EXAM: "mock_exam",
}

VALUE_TO_FOLDER = {member.value: folder for member, folder in CATEGORY_TO_FOLDER.items()}

class AnalysisResult(Base):
    __tablename__ = 'analysis_result'
    id = Column(Integer, primary_key=True)
    practice_type = Column(String(100), nullable=False)
    submission_time = Column(String(100), nullable=False)
    difficulty = Column(Float)
    total_questions = Column(Integer)
    questions_answered = Column(Integer)
    correct_answers = Column(Integer)
    incorrect_answers = Column(Integer)
    unanswered_questions = Column(Integer)
    total_time_minutes = Column(Integer)
    accuracy_rate_overall = Column(Float)
    accuracy_rate_answered = Column(Float)
    completion_score = Column(Integer)
    incorrect_question_numbers = Column(JSON)
    answer_card = Column(JSON)
    ability_analysis = Column(JSON)

    def to_dict(self):
        result = {}
        for c in self.__table__.columns:
            value = getattr(self, c.name)
            # Convert datetime objects to ISO 8601 strings
            if isinstance(value, (datetime.datetime, datetime.date)):
                result[c.name] = value.isoformat()
            else:
                result[c.name] = value
        return result

class AutomationSettings(CentralBase):
    __tablename__ = 'automation_settings'
    id = Column(Integer, primary_key=True)
    task_name = Column(String(100), nullable=False, unique=True)
    enabled = Column(JSON, default=lambda: {"comprehensive_analysis": False, "data_analysis": False, "daily_plan": False})
    execution_time = Column(JSON, default=lambda: {"comprehensive_analysis": "22:00", "data_analysis": "22:00", "daily_plan": "23:00"})
    last_run = Column(JSON, default=lambda: {})

    def to_dict(self):
        return {
            "id": self.id,
            "task_name": self.task_name,
            "enabled": self.enabled,
            "execution_time": self.execution_time,
            "last_run": self.last_run
        }

class StudyPlan(CentralBase):
    __tablename__ = 'study_plan'
    id = Column(Integer, primary_key=True)
    plan_date = Column(String(50), nullable=False, unique=True)
    goals = Column(JSON)
    def to_dict(self):
        result = {}
        for c in self.__table__.columns:
            value = getattr(self, c.name)
            # Convert datetime objects to ISO 8601 strings
            if isinstance(value, (datetime.datetime, datetime.date)):
                result[c.name] = value.isoformat()
            else:
                result[c.name] = value
        return result

class DailySchedule(CentralBase):
    __tablename__ = 'daily_schedule'
    id = Column(Integer, primary_key=True)
    schedule_date = Column(String(50), nullable=False, unique=True)
    schedule_items = Column(JSON)
    def to_dict(self):
        result = {}
        for c in self.__table__.columns:
            value = getattr(self, c.name)
            # Convert datetime objects to ISO 8601 strings
            if isinstance(value, (datetime.datetime, datetime.date)):
                result[c.name] = value.isoformat()
            else:
                result[c.name] = value
        return result
