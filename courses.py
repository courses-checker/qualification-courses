# --- Course Management Functions ---
from flask import session
from datetime import datetime, timedelta
from pymongo import MongoClient
from bson import ObjectId
import os
from dotenv import load_dotenv
import logging
import os
import json
import traceback

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

# Database connection setup
MONGODB_URI = os.getenv('MONGODB_URI')
database_connected = False
user_courses_collection = None
client = None

def initialize_database():
    """Initialize database connection"""
    global database_connected, user_courses_collection, client
    
    try:
        client = MongoClient(
            MONGODB_URI,
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000,
            socketTimeoutMS=5000,
            retryWrites=True,
            retryReads=True
        )
        # Test connection
        client.admin.command('ping')
        
        db_user_data = client['user_data']
        user_courses_collection = db_user_data['user_courses']
        
        # Create indexes if they don't exist
        user_courses_collection.create_index([
            ("email", 1),
            ("index_number", 1),
            ("level", 1)
        ], unique=True)
        
        database_connected = True
        logger.info("✅ Database connection established successfully")
        return True
    except Exception as e:
        logger.error(f"❌ Error connecting to database: {str(e)}")
        database_connected = False
        return False

def verify_courses_consistency(email, index_number, level):
    """Verify course data consistency between session and database"""
    logger.info(f"Verifying course consistency for {email}, {index_number}, {level}")
    
    session_key = f'{level}_courses_{index_number}'
    session_data = session.get(session_key)
    
    if not database_connected:
        logger.warning("Database not connected, cannot verify consistency")
        return False
    
    try:
        db_data = user_courses_collection.find_one({
            'email': email,
            'index_number': index_number,
            'level': level
        })
        
        if not db_data or 'courses' not in db_data:
            logger.warning("No courses found in database")
            return False
        
        db_courses = db_data['courses']
        db_count = len(db_courses)
        
        # Update session with metadata only
        session[session_key] = {
            'courses_count': db_count,
            'last_db_fetch': datetime.now().isoformat(),
            'from_db': True,
            'has_courses': True
        }
        logger.info("✅ Session metadata updated from database")
        return True
        
    except Exception as e:
        logger.error(f"❌ Error verifying course consistency: {str(e)}", exc_info=True)
        return False
def cleanup_database():
    """Cleanup database connections"""
    global client
    if client:
        client.close()

# Initialize database connection
initialize_database()

# Register cleanup on program exit
import atexit
atexit.register(cleanup_database)

def get_user_courses(email, index_number, level, force_refresh=False):
    """Get user courses with strict database preference"""
    logger.info(f"Getting courses for {email}, {index_number}, {level}")
    
    session_key = f'{level}_courses_{index_number}'
    
    # Always try database first if connected
    if database_connected:
        try:
            # Ensure fresh database connection
            if not client.admin.command('ping'):
                logger.warning("Database connection lost, reconnecting...")
                initialize_database()
            
            db_data = user_courses_collection.find_one({
                'email': email,
                'index_number': index_number,
                'level': level
            })
            
            if db_data and 'courses' in db_data:
                # Validate and convert courses
                valid_courses = []
                original_count = len(db_data['courses'])
                
                for course in db_data['courses']:
                    if course and isinstance(course, dict):
                        course_copy = dict(course)
                        if '_id' in course_copy:
                            if isinstance(course_copy['_id'], ObjectId):
                                course_copy['_id'] = str(course_copy['_id'])
                        # Ensure all required fields are present
                        if ('programme_name' in course_copy or 'course_name' in course_copy):
                            valid_courses.append(course_copy)
                
                logger.info(f"✅ Loaded {len(valid_courses)} courses from database for {level}")
                
                # Update session with metadata only
                session[session_key] = {
                    'courses_count': len(valid_courses),
                    'last_db_fetch': datetime.now().isoformat(),
                    'from_db': True,
                    'has_courses': True
                }
                
                return valid_courses
                
        except Exception as e:
            logger.error(f"❌ Error getting courses from database: {str(e)}", exc_info=True)
    
    # Only use session data as a LAST RESORT if database is unavailable
    if not force_refresh:
        session_data = session.get(session_key)
        if session_data and session_data.get('has_courses'):
            # We can't return actual courses from session anymore
            # So we need to indicate that courses exist but need to be fetched from DB
            logger.warning(f"⚠️ Session indicates courses exist but database unavailable")
            return []  # Return empty, UI should show "database unavailable" message
    
    logger.warning("No courses found in database")
    return []

def save_user_courses(email, index_number, level, courses, update_session=True):
    """Save user courses with strict validation and database priority"""
    logger.info(f"Saving courses for {email}, {index_number}, {level}")
    
    if not courses:
        logger.warning("No courses to save")
        return False
    
    # Validate and clean courses
    valid_courses = []
    original_count = len(courses)
    
    for course in courses:
        if not isinstance(course, dict):
            continue
            
        # Ensure course has required fields
        if not (course.get('programme_name') or course.get('course_name')):
            continue
            
        course_copy = dict(course)
        
        # Clean ObjectId fields
        if '_id' in course_copy:
            if isinstance(course_copy['_id'], ObjectId):
                course_copy['_id'] = str(course_copy['_id'])
            elif not isinstance(course_copy['_id'], str):
                continue
        
        # Remove any session-specific fields
        course_copy.pop('from_db', None)
        course_copy.pop('last_update', None)
        
        valid_courses.append(course_copy)
    
    if not valid_courses:
        logger.error("No valid courses after validation")
        return False
    
    if len(valid_courses) != original_count:
        logger.warning(f"⚠️ Course count changed during validation: {original_count} -> {len(valid_courses)}")
    
    # Always try to save to database first
    if database_connected:
        try:
            # Ensure fresh database connection
            if not client.admin.command('ping'):
                logger.warning("Database connection lost, reconnecting...")
                initialize_database()
            
            # Prepare the record
            record = {
                'email': email,
                'index_number': index_number,
                'level': level,
                'courses': valid_courses,
                'courses_count': len(valid_courses),
                'updated_at': datetime.now(),
                'last_validated': datetime.now()
            }
            
            logger.info(f"🛠️ About to update DB for {email}/{index_number}/{level}: saving {len(valid_courses)} courses")

            result = user_courses_collection.update_one(
                {
                    'email': email,
                    'index_number': index_number,
                    'level': level
                },
                {'$set': record},
                upsert=True
            )
            
            logger.info(f"✅ Saved {len(valid_courses)} courses to database for {level}")
            
            # Verify the save
            saved_data = user_courses_collection.find_one({
                'email': email,
                'index_number': index_number,
                'level': level
            })
            
            if saved_data and len(saved_data.get('courses', [])) == len(valid_courses):
                logger.info("✅ Database save verified")
                
                if update_session:
                    # 🔥 FIX: Store ONLY metadata in session, NOT the full courses
                    # This prevents the cookie from overflowing
                    session[f'{level}_courses_{index_number}'] = {
                        'courses_count': len(valid_courses),
                        'last_db_fetch': datetime.now().isoformat(),
                        'from_db': True,
                        'has_courses': True
                    }
                    logger.info(f"✅ Session updated with metadata only (not the full {len(valid_courses)} courses)")
                
                return True
            else:
                logger.error("❌ Database save verification failed")
                raise Exception("Save verification failed")
            
        except Exception as e:
            logger.error(f"❌ Error saving courses to database: {str(e)}", exc_info=True)
            
            # Only fallback to session if database save completely fails
            if update_session:
                # NOTE: Error fallback - this should be rare
                session[f'{level}_courses_{index_number}'] = {
                    'courses_count': len(valid_courses),
                    'last_update': datetime.now().isoformat(),
                    'from_db': False,
                    'error': str(e),
                    'has_courses': True
                }
                logger.warning(f"⚠️ Stored course metadata in session due to DB error")
            return False
    
    # If no database connection, save minimal data to session
    if update_session:
        session[f'{level}_courses_{index_number}'] = {
            'courses_count': len(valid_courses),
            'last_update': datetime.now().isoformat(),
            'from_db': False,
            'has_courses': True
        }
        logger.warning(f"⚠️ Stored course metadata in session (database unavailable)")
    
    return True