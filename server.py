import logging
import os
import io
import asyncio
import sys
from typing import List, Dict, Any, Optional
from datetime import datetime
from google.auth.transport.requests import Request
from google.oauth2 import service_account
from fastmcp import FastMCP
import requests
from google.cloud import storage
from PyPDF2 import PdfReader
from dotenv import load_dotenv
import google.oauth2.id_token
from storage_url import parse_storage_url
from pdf_utils import extract_pdf_text
import content_reader
import json

load_dotenv()

logger = logging.getLogger("classroom_quiz_mcp")
logging.basicConfig(level=logging.INFO)

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

BACKEND_BASE_URL = os.getenv("BACKEND_BASE_URL", "http://localhost:3000")
BACKEND_AUDIENCE = BACKEND_BASE_URL  # Audience for OIDC token

AGENT_INSTRUCTIONS = """
Context & Authentication
- sessionId arrives via <prompt_context>; never ask for it and always pass it to tools as session_id.
- group_id, module IDs, selected_modules, educator_name, group_name, and timezone/locale also come from context. If anything critical is missing, send the standard error message and stop instead of requesting IDs.

Primary Flow
1. Material sync: call get_the_required_materials_in_a_module for every module, then read_content_file_from_URL for each material. Warn and skip gracefully if a module/material is unavailable.
2. Requirement discovery: confirm title, numberOfQuestions, difficulty mix, scoring policy (fixed or dynamic), totalMarks, durationMinutes, startAt/endAt (educator timezone).
3. Generation: once requirements are gathered, call generate_quiz with structured parameters (title, total_marks, duration_minutes, start_at, end_at, questions array, module_ids). The function validates internally. Summarize what was published and ask whether the educator wants updates or an announcement.
4. Revision loop: when the educator requests changes, call apply_quiz_revisions with all parameters (quiz_id and updated values). The function handles validation. Repeat until they are satisfied.
5. Announcement (optional): only when the educator explicitly asks to notify students call add_group_announcement. Otherwise, the quiz remains live without an announcement.

UX Guardrails
- Never expose IDs, prompt_context, backend URLs, tokens, or raw errors.
- Translate backend failures into short, actionable explanations (e.g., date conflicts, permission issues, temporary outages).
- Keep tone professional and concise with occasional celebratory emojis for milestones.

Edge Cases
- If the educator pauses, send a single gentle reminder and wait.
- If they switch modules mid-flow, summarize progress, confirm cancellation, and restart from material collection.
- If they ask for non-quiz features, explain the limitation and guide them back to quiz creation.

Success Checklist
- sessionId used everywhere (never surfaced).
- Materials accessed or skipped with explanations.
- Requirements locked before drafting.
- generate_quiz called with all required parameters (title, total_marks, duration_minutes, start_at, end_at, questions, module_ids).
- Questions array properly structured with text, options, correctOptionIndex, and point for each question.
- apply_quiz_revisions used for any post-generation edit with all parameters.
- Announcements only sent after explicit educator request.
- Final summary provided once the educator is done.

"""

def get_service_token():
    """Get a fresh Google-signed OIDC Identity Token, preferring explicit service account file locally."""
    auth_request = Request()
    audience = BACKEND_AUDIENCE

    sa_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or os.getenv("SERVICE_ACCOUNT_FILE")
    if sa_path and os.path.exists(sa_path):
        creds = service_account.IDTokenCredentials.from_service_account_file(
            sa_path,
            target_audience=audience,
        )
        creds.refresh(auth_request)
        return creds.token

    # Fallback to metadata server / default credentials (Cloud Run, etc.)
    return google.oauth2.id_token.fetch_id_token(auth_request, audience=audience)

def make_authenticated_request(endpoint: str, method: str, session_id: str, data: dict = None):
    """
    Make authenticated request to backend with service account token and session ID.
    
    Args:
        endpoint: API endpoint path (e.g., "/api/v1/materials/module/mod_123")
        method: HTTP method (GET, POST, PUT, DELETE)
        session_id: Session ID for authentication (extracted from prompt context)
        data: Optional request body for POST/PUT requests
    
    Returns:
        Response text or raises exception on error
    """
    token = get_service_token()
    url = f"{BACKEND_BASE_URL}{endpoint}"
    headers = {
        "authentication-service": f"Bearer {token}",
        "session-id": session_id,
        "Content-Type": "application/json"
    }
    
    try:
        if method.upper() == "GET":
            response = requests.get(url, headers=headers)
        elif method.upper() == "POST":
            response = requests.post(url, headers=headers, json=data)
        elif method.upper() == "PUT":
            response = requests.put(url, headers=headers, json=data)
        elif method.upper() == "DELETE":
            response = requests.delete(url, headers=headers)
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")
        
        response.raise_for_status()
        return response.text
    except requests.exceptions.RequestException as e:
        status = getattr(e.response, "status_code", None)
        body = getattr(e.response, "text", "")
        logger.error(
            "Backend request failed (%s %s): status=%s, error=%s, body=%s",
            method.upper(),
            endpoint,
            status,
            e,
            body,
            exc_info=True,
        )
        return json.dumps({
            "success": False,
            "error_code": "BACKEND_REQUEST_FAILED",
            "status": status,
            "message": "Request to classroom service failed.",
            "details": body[:1000] if body else str(e),
        })

def validate_quiz_parameters(
    title: str,
    total_marks: float,
    duration_minutes: int,
    start_at: str,
    end_at: str,
    questions: List[Dict[str, Any]],
    module_ids: List[str]
) -> Dict[str, Any]:
    """Validate quiz parameters before API submission."""
    errors = []
    
    # Title validation
    if not title or not title.strip():
        errors.append("Title cannot be empty")
    
    # Marks validation
    if total_marks <= 0:
        errors.append("Total marks must be positive")
    
    # Duration validation
    if duration_minutes <= 0:
        errors.append("Duration must be positive")
    
    # Date validation
    try:
        start = datetime.fromisoformat(start_at.replace('Z', '+00:00'))
        end = datetime.fromisoformat(end_at.replace('Z', '+00:00'))
        if start >= end:
            errors.append("Start time must be before end time")
    except ValueError as e:
        errors.append(f"Invalid date format: {str(e)}")
    
    # Questions validation
    if not questions:
        errors.append("At least one question is required")
    else:
        total_points = 0
        for idx, q in enumerate(questions):
            if not q.get("text", "").strip():
                errors.append(f"Question {idx+1}: text is required")
            
            options = q.get("options", [])
            if len(options) < 2:
                errors.append(f"Question {idx+1}: at least 2 options required")
            
            correct_idx = q.get("correctOptionIndex")
            if not isinstance(correct_idx, int) or correct_idx < 0 or correct_idx >= len(options):
                errors.append(f"Question {idx+1}: invalid correctOptionIndex")
            
            point = q.get("point", 1)
            if point <= 0:
                errors.append(f"Question {idx+1}: point must be positive")
            total_points += point
        
        if abs(total_points - total_marks) > 0.01:
            errors.append(f"Sum of question points ({total_points}) doesn't match totalMarks ({total_marks})")
    
    # Module IDs validation
    if not module_ids:
        errors.append("At least one module_id is required")
    
    return {
        "valid": len(errors) == 0,
        "errors": errors
    }

# Create MCP server with explicit agent instructions
mcp = FastMCP("Classroom Quiz Generator MCP Server", instructions=AGENT_INSTRUCTIONS)


@mcp.tool()
def get_the_required_materials_in_a_module(module_id: str, session_id: str) -> str:
    """
    Fetch all required learning materials for a specific module.
    
    Purpose:
        Retrieve teaching materials (PDFs, documents) associated with one module
        to build focused, content-aligned quiz questions.
    
    When to use:
        - Educator wants quiz based on specific module(s)
        - Call once per module included in quiz scope
        - Always call this before reading material content
    
    Input:
        module_id: Internal module identifier from prompt context
                   (NEVER ask educator for this; it's provided automatically)
        session_id: Current session ID from prompt context (sessionId)
                    REQUIRED for authentication; extract from prompt context,
                    never ask educator to provide it
    
    Output:
        JSON string containing:
        {
            "success": true,
            "module_name": "Object-Oriented Programming",
            "materials": [
                {
                    "material_id": "mat_abc123",
                    "title": "OOP Fundamentals",
                    "url": "gs://bucket/path/to/file.pdf",
                    "type": "pdf",
                    "file_size": "2.5 MB"
                }
            ]
        }
        
        OR on error:
        {
            "success": false,
            "error": "Module not found or no access"
        }
    
    Next steps after calling:
        1. Check success field; if false, inform educator of the error
        2. Extract module_name to show educator which module is being processed
        3. For EACH material URL, call read_content_file_from_URL to get content
        4. DO NOT expose material_id or module_id to educator
    
    Notes:
        - Returns only "required" materials for the module
        - Materials are filtered by educator's module selection
        - Empty materials array means no content uploaded yet
        - Authentication is handled automatically via service account when
          session_id is provided
        - userId is no longer provided in prompt context; always pass the sessionId value as session_id
    """
    endpoint = f"/api/v1/materials/module/{module_id}"
    return make_authenticated_request(endpoint, "GET", session_id)


@mcp.tool()
def read_content_file_from_URL(file_url: str, session_id: str) -> str:
    """
    Read and extract text content from a file stored in Google Cloud Storage.
    
    Purpose:
        Extract full text content from material files (primarily PDFs) to analyze
        for quiz question generation.
    
    When to use:
        - After get_the_required_materials_in_a_module returns material URLs
        - Call once per material URL to get its text content
        - Use before generating quiz questions
    
    Input:
        file_url: Full URL to file. Supported formats:
            - gs://bucket/path/to/object
            - https://storage.googleapis.com/bucket/path/to/object
            - https://storage.cloud.google.com/bucket/path/to/object
            - https://firebasestorage.googleapis.com/v0/b/<bucket>/o/<object>
        session_id: Current session ID from prompt context (sessionId)
                    REQUIRED for authentication; extract from prompt context,
                    never ask educator to provide it
    
    Output:
        Extracted text content of the file as plain string.
        For PDFs: all text extracted from all pages.
        For text files: raw content.
        For unreadable files: error message explaining the issue.
    
    Next steps after calling:
        1. Analyze content to identify key topics, concepts, definitions
        2. Use content to craft quiz questions aligned to material
        3. Ensure questions reference actual content from materials
    
    Notes:
        - Automatically handles authentication via service account
        - Best effort UTF-8 decoding for binary files
        - DO NOT expose file URLs or storage paths to educator
        - Returns empty string if file doesn't exist or is inaccessible
        - session_id is used for access control and audit logging
        - userId is not available in prompt context; always pass sessionId as session_id
    """
    # Note: This function uses direct GCS access, not backend API
    # but session_id can be used for audit logging if needed
    try:
        return content_reader.read_content_file_from_URL(file_url)
    except Exception as e:
        return f"Error reading file: {str(e)}"


# @mcp.tool()
# def validate_quiz_json(quiz_json: str, session_id: str) -> str:
#     """
#     Validate quiz JSON against strict schema requirements before submission.
    
#     Purpose:
#         Ensure quiz JSON is properly formatted and contains all required fields
#         before calling generate_quiz. Prevents database errors and ensures
#         data integrity.
    
#     When to use:
#         - ALWAYS call before showing preview to educator
#         - After drafting quiz questions and metadata
#         - After any modifications to quiz_json
#         - REQUIRED before generate_quiz can be called
    
#     Input:
#     {
#         quiz_json: Complete payload as JSON string with structure:
#         {
#             "quiz_details": {
#                 "title": "Quiz Title",
#                 "description": "Optional description",
#                 "totalMarks": 100,
#                 "durationMinutes": 60,
#                 "startAt": "2024-12-01T10:00:00Z",
#                 "endAt": "2024-12-10T23:59:59Z",
#                 "questions": [
#                     {
#                         "text": "What is OOP?",
#                         "options": ["A", "B", "C", "D"],
#                         "correctOptionIndex": 0,
#                         "point": 5
#                     }
#                 ],
#                 "module_ids": ["..."]
#             },
#             "session_id": "<sessionId from prompt context>"
#         }
#     }
    
#     Output:
#         JSON string containing validation result:
#         {
#             "valid": true/false,
#             "errors": [
#                 "questions[2].correctOptionIndex (5) exceeds options length (4)",
#                 "totalMarks (95) does not match sum of question points (100)"
#             ],
#             "warnings": [
#                 "description is empty",
#                 "question 3 has only 2 options (consider 4+ for better assessment)"
#             ]
#         }
    
#     Validation rules:
#         Required fields:
#             - title (non-empty string)
#             - totalMarks (positive number)
#             - durationMinutes (positive number)
#             - startAt (ISO 8601 datetime string)
#             - endAt (ISO 8601 datetime string)
#             - questions (non-empty array)
        
#         Question requirements:
#             - text (non-empty string)
#             - options (array with 2+ strings, recommend 4)
#             - correctOptionIndex (valid index in options array: 0 to options.length-1)
#             - point (positive number, default 1)
        
#         Logic checks:
#             - startAt must be before endAt
#             - Sum of question points should equal totalMarks
#             - No duplicate question text
#             - Options should be non-empty strings
#             - Each question must have at least 2 options
    
#     Next steps after calling:
#         - If valid: true → show preview to educator
#         - If valid: false → fix errors and revalidate
#         - Never show preview or call generate_quiz if validation fails
    
#     Notes:
#         - Validation is strict; all errors must be fixed
#         - Warnings are suggestions; quiz can proceed with warnings
#         - ISO 8601 format: YYYY-MM-DDTHH:MM:SSZ or with timezone offset
#     """
#     try:
#         payload_in = json.loads(quiz_json)
#         errors = []
#         warnings = []

#         if not isinstance(payload_in, dict):
#             return json.dumps({
#                 "valid": False,
#                 "errors": ["Payload must be a JSON object containing quiz_details"],
#                 "warnings": []
#             })

#         if "quiz_details" not in payload_in:
#             return json.dumps({
#                 "valid": False,
#                 "errors": ["Missing quiz_details object in payload"],
#                 "warnings": []
#             })

#         data = payload_in.get("quiz_details")
#         if not isinstance(data, dict):
#             return json.dumps({
#                 "valid": False,
#                 "errors": ["quiz_details must be an object"],
#                 "warnings": []
#             })
        
#         # Required field validation
#         if not data.get("title") or not data["title"].strip():
#             errors.append("title is required and cannot be empty")
        
#         if not isinstance(data.get("totalMarks"), (int, float)) or data.get("totalMarks", 0) <= 0:
#             errors.append("totalMarks must be a positive number")
        
#         if not isinstance(data.get("durationMinutes"), (int, float)) or data.get("durationMinutes", 0) <= 0:
#             errors.append("durationMinutes must be a positive number")
        
#         if not data.get("startAt"):
#             errors.append("startAt is required")
        
#         if not data.get("endAt"):
#             errors.append("endAt is required")
        
#         # Questions validation
#         questions = data.get("questions", [])
#         if not questions or len(questions) == 0:
#             errors.append("questions array cannot be empty")
#         else:
#             total_points = 0
#             question_texts = set()
            
#             for idx, question in enumerate(questions):
#                 # Question text
#                 if not question.get("text") or not question["text"].strip():
#                     errors.append(f"questions[{idx}].text is required and cannot be empty")
#                 elif question["text"] in question_texts:
#                     errors.append(f"questions[{idx}].text is duplicate")
#                 else:
#                     question_texts.add(question["text"])
                
#                 # Options
#                 options = question.get("options", [])
#                 if not isinstance(options, list) or len(options) < 2:
#                     errors.append(f"questions[{idx}].options must have at least 2 options")
#                 elif len(options) < 4:
#                     warnings.append(f"questions[{idx}] has only {len(options)} options (4 recommended)")
                
#                 # Check for empty options
#                 for opt_idx, option in enumerate(options):
#                     if not option or not str(option).strip():
#                         errors.append(f"questions[{idx}].options[{opt_idx}] cannot be empty")
                
#                 # Correct option index
#                 correct_idx = question.get("correctOptionIndex")
#                 if not isinstance(correct_idx, int):
#                     errors.append(f"questions[{idx}].correctOptionIndex must be a number")
#                 elif correct_idx < 0 or correct_idx >= len(options):
#                     errors.append(f"questions[{idx}].correctOptionIndex ({correct_idx}) is out of bounds for {len(options)} options")
                
#                 # Points
#                 points = question.get("point", 1)
#                 if not isinstance(points, (int, float)) or points <= 0:
#                     errors.append(f"questions[{idx}].point must be a positive number")
#                 else:
#                     total_points += points
            
#             # Total marks validation
#             if data.get("totalMarks") and abs(total_points - data["totalMarks"]) > 0.01:
#                 errors.append(f"Sum of question points ({total_points}) does not match totalMarks ({data['totalMarks']})")
        
#         # Date validation
#         if data.get("startAt") and data.get("endAt"):
#             try:
#                 from datetime import datetime
#                 start = datetime.fromisoformat(data["startAt"].replace('Z', '+00:00'))
#                 end = datetime.fromisoformat(data["endAt"].replace('Z', '+00:00'))
#                 if start >= end:
#                     errors.append("startAt must be before endAt")
#             except ValueError as e:
#                 errors.append(f"Invalid date format: {str(e)}")
        
#         # Optional warnings
#         if not data.get("description") or not data["description"].strip():
#             warnings.append("description is empty (recommended to add context for students)")
        
#         return json.dumps({
#             "valid": len(errors) == 0,
#             "errors": errors,
#             "warnings": warnings
#         })
        
#     except json.JSONDecodeError as e:
#         return json.dumps({
#             "valid": False,
#             "errors": [f"Invalid JSON format: {str(e)}"],
#             "warnings": []
#         })
#     except Exception as e:
#         return json.dumps({
#             "valid": False,
#             "errors": [f"Validation error: {str(e)}"],
#             "warnings": []
#         })


@mcp.tool()
def generate_quiz(
    title: str,
    total_marks: float,
    duration_minutes: int,
    start_at: str,
    end_at: str,
    questions: List[Dict[str, Any]],
    module_ids: List[str],
    session_id: str,
    description: str = ""
) -> str:
    """
    Create and publish a new quiz to the educator's classroom.
    
    Purpose:
        Persist a validated quiz so it appears immediately in the educator UI.
        Students can access it once the start_at time arrives.
    
    When to use:
        - After gathering all quiz requirements from the educator
        - Once you've collected content from module materials
        - When questions have been crafted and validated
        - For brand-new quizzes only (use apply_quiz_revisions for edits)
    
    Parameters:
        title: Quiz name shown to students (e.g., "Midterm Exam - OOP")
        total_marks: Maximum possible score (must equal sum of all question points)
        duration_minutes: Time limit in minutes (e.g., 60 for 1 hour)
        start_at: When quiz becomes available (ISO 8601 format: "2024-12-15T09:00:00Z")
        end_at: When quiz closes (ISO 8601 format: "2024-12-15T18:00:00Z")
        questions: Array of question objects, each with:
            - text: Question prompt (string)
            - options: Answer choices (array of strings, minimum 2)
            - correctOptionIndex: Index of correct answer (0-based integer)
            - point: Points awarded for correct answer (positive number)
        module_ids: Module identifiers from prompt context (array of strings)
        session_id: Session ID from prompt context for authentication
        description: Optional context/instructions for students
    
    Example questions parameter:
        [
            {
                "text": "What is encapsulation in OOP?",
                "options": ["Hiding data", "Inheritance", "Polymorphism", "Abstraction"],
                "correctOptionIndex": 0,
                "point": 5
            },
            {
                "text": "Which keyword is used for inheritance in Python?",
                "options": ["extends", "inherits", "class", "implements"],
                "correctOptionIndex": 2,
                "point": 5
            }
        ]
    
    Returns:
        JSON string with:
        {
            "success": true,
            "quiz_id": "quiz_abc123",
            "modules_linked": ["Object-Oriented Programming"],
            "available_from": "Dec 15, 2024 at 9:00 AM",
            "available_until": "Dec 15, 2024 at 6:00 PM",
            "message": "Quiz created successfully"
        }
    
    Next steps:
        1. Store quiz_id internally (never show to educator)
        2. Summarize: title, duration, marks, availability window, covered topics
        3. Ask if educator wants to make changes or notify students
    
    Validation:
        - Automatically validates all parameters before API call
        - Returns validation errors if any constraint is violated
        - Ensures total_marks equals sum of question points
        - Checks start_at is before end_at
    
    Notes:
        - Quiz is immediately live; no separate publish step needed
        - Students see it in their UI once start_at time arrives
        - Announcement is separate (use add_group_announcement if requested)
    """
    # Validate parameters
    validation = validate_quiz_parameters(
        title, total_marks, duration_minutes, start_at, end_at, questions, module_ids
    )
    
    if not validation["valid"]:
        return json.dumps({
            "success": False,
            "error": "Validation failed",
            "details": validation["errors"]
        })
    
    # Build API payload
    payload = {
        "quiz_details": {
            "title": title.strip(),
            "description": description.strip(),
            "totalMarks": total_marks,
            "durationMinutes": duration_minutes,
            "startAt": start_at,
            "endAt": end_at,
            "questions": questions,
            "module_ids": module_ids
        }
    }
    
    endpoint = "/api/v1/quizzes/from-details"
    return make_authenticated_request(endpoint, "POST", session_id, payload)


@mcp.tool()
def apply_quiz_revisions(
    quiz_id: str,
    title: str,
    total_marks: float,
    duration_minutes: int,
    start_at: str,
    end_at: str,
    questions: List[Dict[str, Any]],
    module_ids: List[str],
    session_id: str,
    description: str = ""
) -> str:
    """
    Update an existing quiz with educator-requested changes.
    
    Purpose:
        Apply modifications to a previously created quiz (title changes,
        question edits, timing adjustments, etc.)
    
    When to use:
        - After generate_quiz has been called and quiz_id was returned
        - When educator requests changes to existing quiz
        - For iterative refinement of quiz content
    
    Parameters:
        quiz_id: Identifier from generate_quiz response (stored internally, never shown)
        title: Updated quiz name
        total_marks: Updated maximum score
        duration_minutes: Updated time limit
        start_at: Updated availability start (ISO 8601)
        end_at: Updated availability end (ISO 8601)
        questions: Updated complete questions array (same format as generate_quiz)
        module_ids: Updated module identifiers
        session_id: Session ID from prompt context
        description: Updated optional description
    
    Returns:
        JSON string with:
        {
            "success": true,
            "message": "Quiz updated successfully",
            "quiz": { ...updated quiz object... }
        }
    
    Next steps:
        1. Confirm changes to educator
        2. Ask if further revisions needed
        3. Offer to send announcement if quiz details changed significantly
    
    Notes:
        - Complete replacement: provide ALL fields even if only one changed
        - Validates parameters same as generate_quiz
        - Students see updates immediately if quiz is already live
        - Use same question array structure as generate_quiz
    """
    # Validate parameters
    validation = validate_quiz_parameters(
        title, total_marks, duration_minutes, start_at, end_at, questions, module_ids
    )
    
    if not validation["valid"]:
        return json.dumps({
            "success": False,
            "error": "Validation failed",
            "details": validation["errors"]
        })
    
    # Build API payload
    payload = {
        "quiz_details": {
            "title": title.strip(),
            "description": description.strip(),
            "totalMarks": total_marks,
            "durationMinutes": duration_minutes,
            "startAt": start_at,
            "endAt": end_at,
            "questions": questions,
            "module_ids": module_ids
        }
    }
    
    endpoint = f"/api/v1/quizzes/from-details/{quiz_id}"
    return make_authenticated_request(endpoint, "PUT", session_id, payload)


@mcp.tool()
def add_group_announcement(group_id: str, announcement_details: str, session_id: str) -> str:
    """
    Post an announcement to the group feed when the educator explicitly asks to notify students.

Purpose:
    Share quiz availability details (duration, marks, window, covered modules) with all students.

When to use:
    - Only after generate_quiz/apply_quiz_revisions succeeded and the educator says to announce.
    - Once per quiz launch (retry only if a prior attempt failed).

Input:
    group_id: Provided via prompt context (never ask the educator).
    announcement_details: JSON string such as:
        {
          "text": "A new quiz 'Midterm Exam' is now available...",
          "quiz_id": "quiz_123"
        }
    session_id: Prompt-context session for authentication.

Output:
    {
      "success": true,
      "announcement_id": "ann_456",
      "notified_students": 42
    }
    or
    {
      "success": false,
      "error": "...",
      "details": "..."
    }

    Notes:
        - The quiz is already live once generate_quiz/apply_quiz_revisions returns success; this call only handles notifications.
        - If it fails, reassure the educator that the quiz remains accessible and offer manual announcement options.
        - Announcement schema: author (ObjectId, from session), text (required), quiz (optional ObjectId), group (required ObjectId).
    """
    try:
        details = json.loads(announcement_details)
    except json.JSONDecodeError as e:
        return json.dumps({
            "success": False,
            "error": "Invalid announcement details format",
            "details": str(e)
        })

    text = details.get("text")
    if not text or not str(text).strip():
        return json.dumps({
            "success": False,
            "error": "Announcement text is required"
        })

    payload = {
        "text": text,
        "group": group_id  # author is resolved from the session linked to Session-ID header
    }

    if details.get("quiz_id"):
        payload["quiz"] = details["quiz_id"]

    try:
        endpoint = "/api/v1/announcements"
        return make_authenticated_request(endpoint, "POST", session_id, payload)
    except Exception as e:
        return json.dumps({
            "success": False,
            "error": "Failed to create announcement",
            "details": str(e)
        })


# Run server with HTTP transport
if __name__ == "__main__":
    # Cloud Run (and similar platforms) inject PORT; default to 9000 for local dev.
    port = int(os.getenv("PORT", "9000"))
    mcp.run(transport="streamable-http", host="0.0.0.0", port=port)
    #mcp.run(transport="streamable-http", host="127.0.0.1", port=port)
