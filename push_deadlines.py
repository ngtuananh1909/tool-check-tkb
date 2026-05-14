#!/usr/bin/env python3
"""Push eLearning deadlines from subject_deadlines.json to Supabase."""
import json
import os
import sys
import hashlib

# Add current directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

from supabase import create_client, Client


def _elearning_deadline_signature(student_id, course_id, activity_name, due_date):
    """Generate a unique signature for elearning deadline."""
    data = f"{student_id}|{course_id}|{activity_name}|{due_date}"
    return hashlib.sha256(data.encode("utf-8")).hexdigest()[:32]


def push_deadlines(deadlines):
    """Push deadlines to Supabase."""
    student_id = os.environ.get("STUDENT_ID")
    if not student_id:
        raise ValueError("STUDENT_ID is required")
    
    url = os.environ.get("SUPABASE_URL", "https://cnmvukglrzbumhcwpfxj.supabase.co/rest/v1/").rstrip("/rest/v1")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    
    client = create_client(url, key)
    
    payload_rows = []
    for row in deadlines:
        course_name = str(row.get("course_name") or "").strip()
        activity_name = str(row.get("activity_name") or "").strip()
        due_date = str(row.get("due_date") or "").strip()
        
        if not course_name or not activity_name or not due_date:
            continue
        
        course_id = str(row.get("course_id") or "").strip()
        if not course_id:
            course_id = hashlib.sha256(course_name.lower().encode("utf-8")).hexdigest()[:16]
        
        source_signature = _elearning_deadline_signature(student_id, course_id, activity_name, due_date)
        
        payload_rows.append({
            "student_id": student_id,
            "course_id": course_id,
            "course_name": course_name,
            "activity_name": activity_name,
            "due_date": due_date,
            "activity_url": str(row.get("activity_url") or "").strip() or None,
            "completion_status": "incomplete",
            "source_signature": source_signature,
        })
    
    if not payload_rows:
        print("No valid deadlines to push")
        return 0
    
    try:
        # Clear existing deadlines for this student first
        client.table("elearning_deadlines").delete().eq("student_id", student_id).execute()
        
        # Insert new deadlines
        response = client.table("elearning_deadlines").insert(payload_rows).execute()
        print(f"Successfully pushed {len(payload_rows)} deadline(s) to Supabase")
        return len(payload_rows)
    except Exception as e:
        print(f"Error pushing deadlines: {e}")
        raise


def main():
    # Load deadlines from JSON file
    json_path = os.path.join(os.path.dirname(__file__), "subject_deadlines.json")
    
    with open(json_path, "r", encoding="utf-8") as f:
        deadlines = json.load(f)
    
    print(f"Loaded {len(deadlines)} deadlines from {json_path}")
    
    # Push to Supabase
    try:
        count = push_deadlines(deadlines)
        print(f"Successfully upserted {count} deadline(s) to Supabase")
    except Exception as e:
        print(f"Error pushing deadlines: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
