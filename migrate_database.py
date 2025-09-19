from sqlalchemy import create_engine, inspect
from sqlalchemy.sql import text
from models import CentralBase
import os

# Get the absolute path to the instance folder to ensure correct DB location
instance_folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'instance')
os.makedirs(instance_folder, exist_ok=True)
db_path = os.path.join(instance_folder, 'app_data.db')

DATABASE_URI = f'sqlite:///{db_path}'

def migrate():
    """Creates or updates the database tables based on the CentralBase metadata."""
    print(f"Connecting to database at: {DATABASE_URI}")
    engine = create_engine(DATABASE_URI)
    
    try:
        print("Applying migrations: Creating/updating tables...")
        # Create all tables defined in CentralBase if they don't exist
        CentralBase.metadata.create_all(engine)
        
        # --- Manual Migration for last_run column ---
        # SQLAlchemy's create_all won't add columns to existing tables on SQLite
        inspector = inspect(engine)
        columns = [col['name'] for col in inspector.get_columns('automation_settings')]
        
        if 'last_run' not in columns:
            print("Column 'last_run' not found in 'automation_settings' table. Adding it...")
            with engine.connect() as connection:
                # Use text() for literal SQL
                connection.execute(text('ALTER TABLE automation_settings ADD COLUMN last_run JSON'))
                # Commit is implicit with engine.connect() context manager
            print("'last_run' column added successfully.")
        else:
            print("'last_run' column already exists.")

        print("Database migration completed successfully.")
    except Exception as e:
        print(f"An error occurred during migration: {e}")

if __name__ == '__main__':
    migrate()
