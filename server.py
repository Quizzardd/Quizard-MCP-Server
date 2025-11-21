import logging
import os
import io
import asyncio
import sys
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
2. Requirement discovery: confirm title, numberOfQuestions, difficulty mix, scoring policy (fixed or dynamic), totalMarks, durationMinutes, startAt/endAt (educator timezone), and accommodations before drafting.
3. Validation + generation: once validate_quiz_json passes, immediately call generate_quiz so the UI shows the draft. Summarize what was published and ask whether the educator wants updates or an announcement.
4. Revision loop: when the educator requests changes, rebuild the entire quiz JSON, revalidate, and call apply_quiz_revisions to push the update. Repeat until they are satisfied.
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
- Validation succeeded before generate_quiz was invoked.
- apply_quiz_revisions used for any post-generation edit.
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
def generate_quiz(quiz_details: str, session_id: str) -> str:
    """
Persist the validated quiz so it appears in the educator UI.

Flow expectations:
    1. Finish requirement gathering and run validate_quiz_json.
    2. As soon as validation returns valid: true, call generate_quiz so the
       initial draft is stored and rendered. No extra educator approval prompt
       is required at this moment.
    3. Capture quiz_id, modules_linked, available_from, and available_until for
       future revision/announcement steps (never expose IDs to the educator).

When to use:
    - Immediately after validation passes for a brand-new quiz configuration.
    - Never for incremental edits (use apply_quiz_revisions in that case).

Input structure:
    "{
      "quiz_details": {
        "title": "...",
        "description": "...",
        "totalMarks": 100,
        "durationMinutes": 90,
        "startAt": "2024-12-15T09:00:00Z",
        "endAt": "2024-12-15T12:00:00Z",
        "module_ids": ["mod_abc", "mod_xyz"],
        "questions": [
          {"text": "...", "options": ["A","B","C","D"], "correctOptionIndex": 1, "point": 5},
          {"text": "...", "options": ["A","B","C","D"], "correctOptionIndex": 2, "point": 5}
        ]
      },
      "session_id": "<sessionId from prompt context>"
    }"

Response format:
    {
      "success": true,
      "quiz_id": "quiz_123",
      "modules_linked": ["Object-Oriented Programming"],
      "available_from": "Dec 15, 2024 at 9:00 AM",
      "available_until": "Dec 15, 2024 at 12:00 PM",
      "message": "Quiz created successfully"
    }

Next steps when success=true:
    - Relay human-friendly details (title, duration, marks, availability).
    - Store quiz_id privately for apply_quiz_revisions.
    - Ask the educator whether they want more updates or prefer to notify students.

Error handling:
    - On success=false, explain the high-level cause (date conflict, permissions,
      backend timeout, etc.) and offer to retry after adjustments.
    - No quiz is created when the call fails, so it is safe to call again once
      the issue is resolved.
"""
    endpoint = "/api/v1/quizzes/from-details"
    return make_authenticated_request(endpoint, "POST", session_id, json.loads(quiz_details))


@mcp.tool()
def apply_quiz_revisions(quiz_id: str, updated_quiz_details: str, session_id: str) -> str:
    """
    Apply educator-requested updates to an existing quiz draft.

    Usage:
        1. Gather the requested changes and rebuild the full quiz JSON.
        2. Run validate_quiz_json again and ensure it is still valid.
        3. Call apply_quiz_revisions so the backend/UI reflect the edits.

    Args:
        quiz_id: Identifier returned from generate_quiz (keep private).
        updated_quiz_details: Stringified JSON with structure:
            "{
              "quiz_details": { ...same schema as generate_quiz... },
              "session_id": "<sessionId from prompt context>"
            }"
        session_id: Session identifier from prompt context for auth.

    Behavior:
        - Issues a PUT to /api/v1/quizzes/{quiz_id} with the new payload.
        - Returns { "success": bool, "message": "...", "quiz": {...} } on success.
        - On failure, includes error/details strings to translate for the educator.
    """

    endpoint = f"/api/v1/quizzes/from-details/{quiz_id}"
    return make_authenticated_request(endpoint, "PUT", session_id, json.loads(updated_quiz_details))


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
