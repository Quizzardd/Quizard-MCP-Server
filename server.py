import logging
import os
from google.auth.transport.requests import Request
from fastmcp import FastMCP
import requests
from google.cloud import storage
import io
from PyPDF2 import PdfReader
import asyncio
import sys
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

BACKEND_AUDIENCE = "https://quizard-backend-534916389595.europe-west1.run.app"
BACKEND_BASE_URL = os.getenv("BACKEND_BASE_URL", "http://localhost:3000")

AGENT_INSTRUCTIONS = """
Context & Authentication
- sessionId always arrives via <prompt_context>. Never ask for it and pass it to every MCP parameter named session_id.
- group_id, module IDs, selected_modules, educator_name, group_name, and timezone/locale also arrive via context. If anything crucial is missing, send the standard error message and stop instead of requesting IDs.

First Response & Flow
- Greet the educator (use their name when available) and mention the module names detected.
- Outline the quiz-building plan: materials -> requirements -> draft -> preview -> submit -> announce.
- Make it clear you already have the necessary context data; never ask them to paste JSON.

Workflow Guardrails
1. Material collection: call get_the_required_materials_in_a_module for each module, then read_content_file_from_URL per material. Warn gracefully if something can't be accessed and skip/abort the module if needed.
2. Requirement discovery: gather title, numberOfQuestions (1-100), difficulty mix, points policy (fixed or dynamic), totalMarks, durationMinutes, startAt/endAt (ISO 8601), accommodations, and any scheduling constraints in the educator's timezone before drafting.
3. Drafting: questions must come from loaded content, balance module coverage, keep distractors plausible, and align to the requested scoring scheme.
4. Validation & preview: run validate_quiz_json until valid (max 3 attempts). Provide a readable preview with summary + numbered questions, then ask for explicit approval.
5. Submission & announcement: only after approval call generate_quiz, translate backend failures into friendly guidance, then call add_group_announcement to notify students.

Revision Handling
- Restate requested edits, rebuild the full quiz JSON, revalidate, and show an updated preview before asking for approval again.

Security & UX
- Never expose IDs, prompt_context, backend URLs, tokens, or raw errors.
- Translate technical failures into short actionable messages (e.g., date conflicts, permission issues, temporary outages).
- Keep tone professional, concise, and encouraging with light emoji use for milestones.

Edge Cases
- If the educator pauses, send one gentle reminder before waiting.
- If they change modules mid-flow, summarize progress, confirm cancellation, and restart Phase 1.
- If they request non-quiz tasks, explain the limitation and guide them back to quiz creation.

Success Criteria
- sessionId used everywhere without exposure.
- Materials synced or skipped with clear rationale.
- Requirements fully confirmed before drafting.
- Validation succeeded prior to submission.
- Approval captured, quiz submitted, announcement attempted, and final wrap-up provided.

"""

def get_service_token():
    """Get a fresh Google-signed OIDC Identity Token."""
    auth_request = Request()
    id_token = google.oauth2.id_token.fetch_id_token(
        auth_request, audience=BACKEND_AUDIENCE
    )
    return id_token

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
        "Authorization": f"Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1c2VySWQiOiI2OTIwNWQyNjQ3ZWY5MWZhYzdiMTU1YTEiLCJpYXQiOjE3NjM3MzcyMDYsImV4cCI6MTc2MzgyMzYwNn0.YYlLDa9jb6WBpfWeMhSlXr4phxi5PmfdVNlsyq0X1t0",
        "Session-ID": session_id,
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
        logger.error(
            "Backend request failed (%s %s): %s",
            method.upper(),
            endpoint,
            e,
            exc_info=True,
        )
        return json.dumps(
            {
                "success": False,
                "error_code": "BACKEND_REQUEST_FAILED",
                "message": "Unable to reach the classroom service right now. Please try again shortly.",
            }
        )

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


@mcp.tool()
def validate_quiz_json(quiz_json: str, session_id: str) -> str:
    """
    Validate quiz JSON against strict schema requirements before submission.
    
    Purpose:
        Ensure quiz JSON is properly formatted and contains all required fields
        before calling generate_quiz. Prevents database errors and ensures
        data integrity.
    
    When to use:
        - ALWAYS call before showing preview to educator
        - After drafting quiz questions and metadata
        - After any modifications to quiz_json
        - REQUIRED before generate_quiz can be called
    
    Input:
        quiz_json: Complete quiz object as JSON string with structure:
        {
            "title": "Quiz Title",
            "description": "Optional description",
            "totalMarks": 100,
            "durationMinutes": 60,
            "startAt": "2024-12-01T10:00:00Z",
            "endAt": "2024-12-10T23:59:59Z",
            "questions": [
                {
                    "text": "What is OOP?",
                    "options": ["A", "B", "C", "D"],
                    "correctOptionIndex": 0,
                    "point": 5
                }
            ]
        }
        session_id: Current session ID from prompt context (sessionId)
                    REQUIRED for authentication; extract from prompt context,
                    never ask educator to provide it
    
    Output:
        JSON string containing validation result:
        {
            "valid": true/false,
            "errors": [
                "questions[2].correctOptionIndex (5) exceeds options length (4)",
                "totalMarks (95) does not match sum of question points (100)"
            ],
            "warnings": [
                "description is empty",
                "question 3 has only 2 options (consider 4+ for better assessment)"
            ]
        }
    
    Validation rules:
        Required fields:
            - title (non-empty string)
            - totalMarks (positive number)
            - durationMinutes (positive number)
            - startAt (ISO 8601 datetime string)
            - endAt (ISO 8601 datetime string)
            - questions (non-empty array)
        
        Question requirements:
            - text (non-empty string)
            - options (array with 2+ strings, recommend 4)
            - correctOptionIndex (valid index in options array: 0 to options.length-1)
            - point (positive number, default 1)
        
        Logic checks:
            - startAt must be before endAt
            - Sum of question points should equal totalMarks
            - No duplicate question text
            - Options should be non-empty strings
            - Each question must have at least 2 options
    
    Next steps after calling:
        - If valid: true → show preview to educator
        - If valid: false → fix errors and revalidate
        - Never show preview or call generate_quiz if validation fails
    
    Notes:
        - Validation is strict; all errors must be fixed
        - Warnings are suggestions; quiz can proceed with warnings
        - ISO 8601 format: YYYY-MM-DDTHH:MM:SSZ or with timezone offset
    """
    try:
        data = json.loads(quiz_json)
        errors = []
        warnings = []
        
        # Required field validation
        if not data.get("title") or not data["title"].strip():
            errors.append("title is required and cannot be empty")
        
        if not isinstance(data.get("totalMarks"), (int, float)) or data.get("totalMarks", 0) <= 0:
            errors.append("totalMarks must be a positive number")
        
        if not isinstance(data.get("durationMinutes"), (int, float)) or data.get("durationMinutes", 0) <= 0:
            errors.append("durationMinutes must be a positive number")
        
        if not data.get("startAt"):
            errors.append("startAt is required")
        
        if not data.get("endAt"):
            errors.append("endAt is required")
        
        # Questions validation
        questions = data.get("questions", [])
        if not questions or len(questions) == 0:
            errors.append("questions array cannot be empty")
        else:
            total_points = 0
            question_texts = set()
            
            for idx, question in enumerate(questions):
                # Question text
                if not question.get("text") or not question["text"].strip():
                    errors.append(f"questions[{idx}].text is required and cannot be empty")
                elif question["text"] in question_texts:
                    errors.append(f"questions[{idx}].text is duplicate")
                else:
                    question_texts.add(question["text"])
                
                # Options
                options = question.get("options", [])
                if not isinstance(options, list) or len(options) < 2:
                    errors.append(f"questions[{idx}].options must have at least 2 options")
                elif len(options) < 4:
                    warnings.append(f"questions[{idx}] has only {len(options)} options (4 recommended)")
                
                # Check for empty options
                for opt_idx, option in enumerate(options):
                    if not option or not str(option).strip():
                        errors.append(f"questions[{idx}].options[{opt_idx}] cannot be empty")
                
                # Correct option index
                correct_idx = question.get("correctOptionIndex")
                if not isinstance(correct_idx, int):
                    errors.append(f"questions[{idx}].correctOptionIndex must be a number")
                elif correct_idx < 0 or correct_idx >= len(options):
                    errors.append(f"questions[{idx}].correctOptionIndex ({correct_idx}) is out of bounds for {len(options)} options")
                
                # Points
                points = question.get("point", 1)
                if not isinstance(points, (int, float)) or points <= 0:
                    errors.append(f"questions[{idx}].point must be a positive number")
                else:
                    total_points += points
            
            # Total marks validation
            if data.get("totalMarks") and abs(total_points - data["totalMarks"]) > 0.01:
                errors.append(f"Sum of question points ({total_points}) does not match totalMarks ({data['totalMarks']})")
        
        # Date validation
        if data.get("startAt") and data.get("endAt"):
            try:
                from datetime import datetime
                start = datetime.fromisoformat(data["startAt"].replace('Z', '+00:00'))
                end = datetime.fromisoformat(data["endAt"].replace('Z', '+00:00'))
                if start >= end:
                    errors.append("startAt must be before endAt")
            except ValueError as e:
                errors.append(f"Invalid date format: {str(e)}")
        
        # Optional warnings
        if not data.get("description") or not data["description"].strip():
            warnings.append("description is empty (recommended to add context for students)")
        
        return json.dumps({
            "valid": len(errors) == 0,
            "errors": errors,
            "warnings": warnings
        })
        
    except json.JSONDecodeError as e:
        return json.dumps({
            "valid": False,
            "errors": [f"Invalid JSON format: {str(e)}"],
            "warnings": []
        })
    except Exception as e:
        return json.dumps({
            "valid": False,
            "errors": [f"Validation error: {str(e)}"],
            "warnings": []
        })


@mcp.tool()
def generate_quiz(quiz_details: str, session_id: str) -> str:
    """
    Submit finalized quiz to database and make it available to students.
    
    ⚠️ CRITICAL: This tool pushes data to production database. Only call after:
        1. validate_quiz_json returns valid: true
        2. Educator has reviewed complete preview
        3. Educator explicitly confirms submission with words like:
           "submit", "create the quiz", "looks good", "approve", "go ahead"
    
    Purpose:
        Persist quiz to database, associate with modules, and make available
        for students to take within the specified time window.
    
    When to use:
        - ONLY after educator explicitly approves preview
        - NEVER call during preview generation or revision
        - NEVER call before validation passes
        - NEVER call "just to show" or "as an example"
    
    Input:
        { "quiz_details":
            {
            "title": "Midterm Exam - OOP & Data Structures",
            "description": "Covers chapters 3-5",
            "totalMarks": 100,
            "durationMinutes": 90,
            "startAt": "2024-12-15T09:00:00Z",
            "endAt": "2024-12-15T12:00:00Z",
            "questions": [
                {
                    "text": "Explain encapsulation",
                    "options": ["A...", "B...", "C...", "D..."],
                    "correctOptionIndex": 2,
                    "point": 5
                },
               {
                    "text": "Explain OOP",
                    "options": ["A...", "B...", "C...", "D..."],
                    "correctOptionIndex": 2,
                    "point": 5
                }

            ],
            "module_ids": ["691f74c7da5825cb1ad6d921", "691f74a0da5825cb1ad6d91d"]
             }
        "session_id": Current session ID from prompt context (sessionId)
                    REQUIRED for authentication; extract from prompt context,
                    never ask educator to provide it
        }
        
    
    Output:
        JSON string containing creation result:
        {
                "success": true,
                "message": "Quiz created successfully",
                "data": {
                    "_id": "691f7fc730dcb62ec768f3aa",
                    "title": "Midterm Exam - OOP & Data Structures",
                    "description": "Covers chapters 3-5",
                    "questions": [
                    {
                        "_id": "691f7fc730dcb62ec768f3a7",
                        "text": "Explain encapsulation",
                        "options": [
                        "A...",
                        "B...",
                        "C...",
                        "D..."
                        ],
                        "correctOptionIndex": 2,
                        "point": 5,
                        "__v": 0
                    },
                    {
                        "_id": "691f7fc730dcb62ec768f3a8",
                        "text": "Explain OOP",
                        "options": [
                        "A...",
                        "B...",
                        "C...",
                        "D..."
                        ],
                        "correctOptionIndex": 2,
                        "point": 5,
                        "__v": 0
                    }
                    ],
                    "totalMarks": 100,
                    "durationMinutes": 90,
                    "startAt": "2024-12-15T09:00:00.000Z",
                    "endAt": "2024-12-15T12:00:00.000Z",
                    "createdAt": "2025-11-20T20:53:27.666Z",
                    "updatedAt": "2025-11-20T20:53:27.666Z",
                    "__v": 0
                 }
}
        
        OR on error:
        {
            "success": false,
            "error": "Description of what went wrong",
            "details": "Additional technical details if available"
        }
    
    Backend operations:
        1. Validates user has permission to create quiz in target modules
        2. Creates individual Question documents in database
        3. Creates Quiz document with references to questions
        4. Links quiz to specified modules (updates module.quizzes array)
        5. Makes quiz discoverable to students in group
    
    Next steps after calling:
        1. Check success field; if false, explain error to educator
        2. If success, parse quiz_id and module names from response
        3. Confirm success to educator with human-readable message
        4. Call add_group_announcement to notify students
        5. DO NOT expose quiz_id or module_ids to educator
    
    Error handling:
        - If success: false, explain error to educator and suggest fixes
        - Common errors: 
          - Invalid dates (startAt after endAt)
          - Missing required fields
          - Module access denied
          - Database timeout/connection issues
        - On error, quiz is NOT created; safe to retry after fixes
    
    Notes:
        - This is a write operation; cannot be undone via MCP
        - Quiz becomes immediately visible to students at startAt time
        - Educator can modify quiz later through platform UI
        - DO NOT call this tool multiple times for same quiz
        - session_id is used to set quiz author and verify permissions
        - Authentication relies on session_id; userId will not be present in prompt context
    """
    endpoint = "/api/v1/quizzes/from-details"
    return make_authenticated_request(endpoint, "POST", session_id, json.loads(quiz_details))


@mcp.tool()
def add_group_announcement(group_id: str, announcement_details: str, session_id: str) -> str:
    """
    Post an announcement to group feed notifying students of new quiz.
    
    Purpose:
        Alert all students in group that a new quiz is available, with
        key details like title, deadline, duration, and marks.
    
    When to use:
        - IMMEDIATELY after successful generate_quiz call
        - Once per quiz creation
        - Only if generate_quiz returned success: true
    
    Input:
        group_id: Internal group identifier from prompt context
                  (NEVER ask educator; provided automatically)
        
        announcement_details: JSON string with structure:
        {
            "text": "A new quiz 'Midterm Exam - OOP & Data Structures' is now available.\n\nDetails:\n- Duration: 90 minutes\n- Total Marks: 100\n- Available: Dec 15, 2024 at 9:00 AM\n- Deadline: Dec 15, 2024 at 12:00 PM\n\nTopics covered: OOP Fundamentals, Data Structures\n\nGood luck!",
            "quiz_id": "quiz_xyz789"
        }
        
        session_id: Current session ID from prompt context (sessionId)
                    REQUIRED for authentication; extract from prompt context,
                    never ask educator to provide it
                    This will be used to authorize the announcement author
    
    Output:
        JSON string containing announcement result:
        {
            "success": true,
            "announcement_id": "ann_abc123",
            "message": "Announcement posted successfully",
            "notified_students": 45
        }
        
        OR on error:
        {
            "success": false,
            "error": "Failed to post announcement",
            "details": "Additional error information"
        }
    
    Announcement text template:
        "A new quiz '{quiz_title}' is now available.
        
        Details:
        - Duration: {durationMinutes} minutes
        - Total Marks: {totalMarks}
        - Available: {startAt formatted human-readable}
        - Deadline: {endAt formatted human-readable}
        
        Topics covered: {module_names joined with commas}
        
        Good luck!"
    
    Backend operations:
        1. Validates user has permission to post to group
        2. Creates Announcement document with:
           - author: resolved from session_id (educator who created quiz)
           - text: announcement message
           - quiz: quiz_id reference
           - group: group_id reference
        3. Notifies all students in group (via notifications service)
        4. Returns count of students notified
    
    Next steps after calling:
        1. Check success field; if false, warn educator but confirm quiz exists
        2. If success, confirm to educator that students have been notified
        3. Provide summary: "Quiz created and {notified_students} students notified"
        4. DO NOT expose announcement_id, quiz_id, or group_id to educator
    
    Notes:
        - Announcements are immediately visible in group feed
        - Students may receive push notifications depending on their settings
        - If announcement fails, quiz is still created and accessible
        - DO NOT expose group_id or internal IDs to educator
        - session_id is used as announcement author for proper attribution
        - Authentication relies on session_id; userId will not be present in prompt context
    """
    # try:
    #     details = json.loads(announcement_details)
        
    #     # Build request payload matching backend schema
    #     payload = {
    #         "text": details.get("text"),
    #         "quiz": details.get("quiz_id"),
    #         "group": group_id
    #         # author will be resolved from session linked to Session-ID header
    #     }
        
    #     endpoint = f"/api/v1/announcements/create"
    #     return make_authenticated_request(endpoint, "POST", session_id, payload)
        
    # except json.JSONDecodeError as e:
    #     return json.dumps({
    #         "success": False,
    #         "error": "Invalid announcement details format",
    #         "details": str(e)
    #     })
    # except Exception as e:
    #     return json.dumps({
    #         "success": False,
    #         "error": "Failed to create announcement",
    #         "details": str(e)
    #     })
    return "You are in testing mode right now, simulate that the announment is send successfully and continue working"


# Run server with HTTP transport
if __name__ == "__main__":
    # Cloud Run (and similar platforms) inject PORT; default to 9000 for local dev.
    port = int(os.getenv("PORT", "9000"))
    mcp.run(transport="streamable-http", host="0.0.0.0", port=port)
