import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from werkzeug.utils import secure_filename
from models import Base, CentralBase, VALUE_TO_FOLDER

# Define instance path relative to the project root
INSTANCE_PATH = os.path.join(os.path.dirname(__file__), 'instance')
os.makedirs(INSTANCE_PATH, exist_ok=True)

# --- Central Database for Plans and Schedules ---
central_db_path = os.path.join(INSTANCE_PATH, 'app_data.db')
central_engine = create_engine(f"sqlite:///{central_db_path}")
CentralBase.metadata.create_all(central_engine)
CentralSession = sessionmaker(bind=central_engine)

# --- Dynamic DB Session Management ---
def get_db_session(category_str, date_str):
    """
    Creates a database session for a specific category string and date.
    Example date_str: '2023-10-27'
    """
    if not category_str or not isinstance(category_str, str):
        raise ValueError("A valid practice category string must be provided.")

    # Look up the standardized folder name from the mapping.
    # If the category_str is not a standard one (e.g., a custom goal),
    # sanitize the string itself as a fallback folder name.
    folder_name = VALUE_TO_FOLDER.get(category_str, secure_filename(category_str.replace(" ", "_")))

    # Create the directory for the category if it doesn't exist
    db_dir = os.path.join('database', folder_name)
    os.makedirs(db_dir, exist_ok=True)

    # Create the full path for the database file
    db_path = os.path.join(db_dir, f"{date_str}.db")

    # Create an engine and ensure the table exists
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine) # This is idempotent

    # Return a new session
    Session = sessionmaker(bind=engine)
    return Session()
