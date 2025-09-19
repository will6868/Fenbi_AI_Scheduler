import os

# Fix for macOS fork issue.
# This MUST be set before any other imports that might initialize Objective-C runtime.
os.environ['OBJC_DISABLE_INITIALIZE_FORK_SAFETY'] = 'YES'

import redis
from rq import Worker, Queue
import logging
import time
import threading
from jobs import schedule_automated_tasks

# Setup logging
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s',
                    handlers=[
                        logging.FileHandler("worker.log"),
                        logging.StreamHandler()
                    ])

listen = ['default']

redis_url = os.getenv('REDIS_URL', 'redis://localhost:6379')

conn = redis.from_url(redis_url)

def run_scheduler():
    """Periodically calls the task scheduler function."""
    logging.info("Starting automation scheduler loop...")
    while True:
        try:
            schedule_automated_tasks()
        except Exception as e:
            logging.error(f"An error occurred in the scheduler loop: {e}", exc_info=True)
        time.sleep(60) # Wait for 60 seconds before the next run

if __name__ == '__main__':
    # Start the scheduler in a separate thread
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()

    # Start the RQ worker in the main thread
    logging.info(f"Starting RQ worker for queues: {listen}")
    worker = Worker(listen, connection=conn)
    worker.work(logging_level='INFO')
