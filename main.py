"""
EGroupware REST API — OpenAPI Tool Server for Open WebUI
=========================================================
Exposes EGroupware's CalDAV/CardDAV REST API as a standard OpenAPI server
that Open WebUI (and any OpenAPI-compatible LLM agent) can use as tools.

Base URL of EGroupware:  set via env  EGW_BASE_URL   (e.g. https://my.egroupware.org/egroupware)
Authentication:          set via env  EGW_USERNAME / EGW_PASSWORD  (Basic auth)
                         OR           EGW_TOKEN  (Application token)

Run:
    pip install -r requirements.txt
    uvicorn main:app --host 0.0.0.0 --port 8000 --reload

Then point Open WebUI's "Add Tool Server" to:  http://localhost:8000
"""

import os
import base64
import json
import httpx
from contextvars import ContextVar
from typing import Optional, List
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Configuration (from environment variables)
# ---------------------------------------------------------------------------

EGW_BASE_URL = os.getenv("EGW_BASE_URL", "https://example.org/egroupware").rstrip("/")
EGW_USERNAME = os.getenv("EGW_USERNAME", "")
EGW_PASSWORD = os.getenv("EGW_PASSWORD", "")
EGW_TOKEN    = os.getenv("EGW_TOKEN", "")          # App token (preferred over password)

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="EGroupware Tools",
    description=(
        "OpenAPI Tool Server that exposes EGroupware's Calendar, Contacts, "
        "Infolog (Tasks), Timesheet, and Mail REST API as tools for Open WebUI."
    ),
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Per-request auth context (populated by middleware from incoming headers)
# ---------------------------------------------------------------------------

_request_auth: ContextVar[Optional[str]] = ContextVar("_request_auth", default=None)
_request_base_url: ContextVar[Optional[str]] = ContextVar("_request_base_url", default=None)


@app.middleware("http")
async def extract_auth_middleware(request: Request, call_next):
    """
    Capture the Authorization header (Bearer token) and optional
    X-EGW-Base-URL header sent by Open WebUI so downstream helpers
    can use them without touching every endpoint signature.
    """
    auth_token = _request_auth.set(request.headers.get("authorization"))
    base_url_token = _request_base_url.set(request.headers.get("x-egw-base-url"))
    try:
        return await call_next(request)
    finally:
        _request_auth.reset(auth_token)
        _request_base_url.reset(base_url_token)


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def _auth():
    """
    Return (auth_tuple, extra_headers) for EGroupware.
    Priority:
      1. Bearer token forwarded by Open WebUI (Authorization header on the incoming request)
      2. EGW_TOKEN env var
      3. EGW_USERNAME / EGW_PASSWORD env vars
    """
    incoming_auth = _request_auth.get()
    if incoming_auth and incoming_auth.lower().startswith("bearer "):
        token = incoming_auth[7:].strip()
        return None, {"Authorization": f"Bearer {token}"}
    if EGW_TOKEN:
        return None, {"Authorization": f"Bearer {EGW_TOKEN}"}
    if EGW_USERNAME and EGW_PASSWORD:
        return (EGW_USERNAME, EGW_PASSWORD), {}
    raise HTTPException(
        status_code=401,
        detail="No EGroupware credentials. Provide an EGroupware app-token as the Bearer token in Open WebUI, or set EGW_USERNAME/EGW_PASSWORD env vars.",
    )


def _base_url() -> str:
    """Return the EGroupware base URL, honouring an optional per-request override."""
    return (_request_base_url.get() or EGW_BASE_URL).rstrip("/")


def _client() -> httpx.Client:
    auth_tuple, extra_headers = _auth()
    headers = {"Accept": "application/json", **extra_headers}
    if auth_tuple:
        return httpx.Client(auth=auth_tuple, headers=headers, timeout=30)
    return httpx.Client(headers=headers, timeout=30)


def _get(path: str, params: dict = None):
    dav_base = f"{_base_url()}/groupdav.php"
    with _client() as client:
        r = client.get(f"{dav_base}{path}", params=params)
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    return r.json()


def _post(path: str, body: dict):
    dav_base = f"{_base_url()}/groupdav.php"
    with _client() as client:
        r = client.post(f"{dav_base}{path}", json=body,
                        headers={"Content-Type": "application/json"})
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    return r.json()


def _put(path: str, body: dict):
    dav_base = f"{_base_url()}/groupdav.php"
    with _client() as client:
        r = client.put(f"{dav_base}{path}", json=body,
                       headers={"Content-Type": "application/json"})
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    return r.json()


def _delete(path: str):
    dav_base = f"{_base_url()}/groupdav.php"
    with _client() as client:
        r = client.delete(f"{dav_base}{path}")
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    return {"status": r.status_code, "message": "Deleted successfully"}


def _username() -> str:
    """
    Return the username for URL path construction.
    Priority:
      1. 'sub' claim from the incoming OAuth JWT Bearer token (EGroupware OpenID)
      2. EGW_USERNAME env var
    """
    incoming_auth = _request_auth.get()
    if incoming_auth and incoming_auth.lower().startswith("bearer "):
        token = incoming_auth[7:].strip()
        try:
            # JWT payload is the second segment, base64url-encoded (no signature check needed
            # here — EGroupware will reject the token itself if it's invalid)
            payload_b64 = token.split(".")[1]
            # Pad to a multiple of 4 for standard base64 decoding
            payload_b64 += "=" * (-len(payload_b64) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_b64))
            sub = payload.get("sub") or payload.get("account_lid")
            if sub:
                return sub
        except Exception:
            pass  # fall through to env-var fallback
    if EGW_USERNAME:
        return EGW_USERNAME
    raise HTTPException(
        status_code=401,
        detail="Cannot determine EGroupware username. Authenticate via Open WebUI OAuth or set EGW_USERNAME.",
    )


# ===========================================================================
# CALENDAR endpoints
# ===========================================================================

class CalendarEvent(BaseModel):
    title: str = Field(..., description="Event title / summary")
    start: str = Field(..., description="Start datetime in ISO 8601, e.g. 2024-06-01T10:00:00")
    timeZone: str = Field("Europe/Berlin", description="IANA timezone, e.g. Europe/Berlin")
    duration: str = Field("PT1H", description="Duration in ISO 8601 duration format, e.g. PT1H (1 hour)")
    description: Optional[str] = Field(None, description="Event description / notes")
    location: Optional[str] = Field(None, description="Physical or virtual location")
    status: Optional[str] = Field("confirmed", description="Event status: confirmed | tentative | cancelled")
    priority: Optional[int] = Field(5, description="Priority 1 (highest) – 9 (lowest), 5 = normal")
    privacy: Optional[str] = Field("public", description="public | private | confidential")


@app.get("/calendar/list", summary="List calendar events", tags=["Calendar"],
         description="Returns all calendar events for the authenticated user.")
def list_calendar_events(
    search: Optional[str] = None,
    start_after: Optional[str] = None,
):
    """
    List calendar events. Optionally filter by a search term or earliest start date.
    - **search**: free-text search
    - **start_after**: only return events starting after this ISO date, e.g. 2024-01-01
    """
    username = _username()
    params = {}
    if search:
        params["filters[search]"] = search
    if start_after:
        params["filters[start]"] = start_after
    return _get(f"/{username}/calendar/", params or None)


@app.get("/calendar/get/{event_id}", summary="Get a calendar event", tags=["Calendar"],
         description="Retrieve a single calendar event by its numeric ID.")
def get_calendar_event(event_id: int):
    username = _username()
    return _get(f"/{username}/calendar/{event_id}")


@app.post("/calendar/create", summary="Create a calendar event", tags=["Calendar"],
          description="Create a new calendar event for the authenticated user.")
def create_calendar_event(event: CalendarEvent):
    username = _username()
    body = {
        "@type": "Event",
        "title": event.title,
        "start": event.start,
        "timeZone": event.timeZone,
        "duration": event.duration,
        "status": event.status,
        "priority": event.priority,
        "privacy": event.privacy,
    }
    if event.description:
        body["description"] = event.description
    if event.location:
        body["locations"] = {"1": {"name": event.location}}
    return _post(f"/{username}/calendar/", body)


@app.put("/calendar/update/{event_id}", summary="Update a calendar event", tags=["Calendar"],
         description="Update an existing calendar event by ID.")
def update_calendar_event(event_id: int, event: CalendarEvent):
    username = _username()
    body = {
        "@type": "Event",
        "title": event.title,
        "start": event.start,
        "timeZone": event.timeZone,
        "duration": event.duration,
        "status": event.status,
        "priority": event.priority,
        "privacy": event.privacy,
    }
    if event.description:
        body["description"] = event.description
    if event.location:
        body["locations"] = {"1": {"name": event.location}}
    return _put(f"/{username}/calendar/{event_id}", body)


@app.delete("/calendar/delete/{event_id}", summary="Delete a calendar event", tags=["Calendar"],
            description="Permanently delete a calendar event by ID.")
def delete_calendar_event(event_id: int):
    username = _username()
    return _delete(f"/{username}/calendar/{event_id}")


# ===========================================================================
# CONTACTS / ADDRESSBOOK endpoints
# ===========================================================================

class Contact(BaseModel):
    fullName: str = Field(..., description="Full display name of the contact")
    first_name: Optional[str] = Field(None, description="First / given name", alias="name/personal")
    last_name: Optional[str] = Field(None, description="Last / family name", alias="name/surname")
    email_work: Optional[str] = Field(None, description="Work email address", alias="emails/work")
    email_home: Optional[str] = Field(None, description="Home / personal email", alias="emails/home")
    phone_work: Optional[str] = Field(None, description="Work phone number", alias="phones/tel_work")
    phone_mobile: Optional[str] = Field(None, description="Mobile phone number", alias="phones/tel_cell")
    org: Optional[str] = Field(None, description="Organisation / company name")
    title: Optional[str] = Field(None, description="Job title")
    note: Optional[str] = Field(None, description="Notes about the contact")

    class Config:
        populate_by_name = True


@app.get("/contacts/list", summary="List contacts", tags=["Contacts"],
         description="Returns contacts from the user's addressbook. Use search to filter.")
def list_contacts(search: Optional[str] = None):
    username = _username()
    params = {"filters[search]": search} if search else None
    return _get(f"/{username}/addressbook/", params)


@app.get("/contacts/get/{contact_id}", summary="Get a contact", tags=["Contacts"],
         description="Retrieve a single contact by its numeric ID.")
def get_contact(contact_id: int):
    username = _username()
    return _get(f"/{username}/addressbook/{contact_id}")


@app.post("/contacts/create", summary="Create a contact", tags=["Contacts"],
          description="Create a new contact in the user's personal addressbook.")
def create_contact(contact: Contact):
    username = _username()
    body = {"fullName": contact.fullName}
    if contact.first_name:
        body["name/personal"] = contact.first_name
    if contact.last_name:
        body["name/surname"] = contact.last_name
    if contact.email_work:
        body["emails/work"] = contact.email_work
    if contact.email_home:
        body["emails/home"] = contact.email_home
    if contact.phone_work:
        body["phones/tel_work"] = contact.phone_work
    if contact.phone_mobile:
        body["phones/tel_cell"] = contact.phone_mobile
    if contact.org:
        body["org"] = contact.org
    if contact.title:
        body["title"] = contact.title
    if contact.note:
        body["note"] = contact.note
    return _post(f"/{username}/addressbook/", body)


@app.delete("/contacts/delete/{contact_id}", summary="Delete a contact", tags=["Contacts"],
            description="Permanently delete a contact by ID.")
def delete_contact(contact_id: int):
    username = _username()
    return _delete(f"/{username}/addressbook/{contact_id}")


# ===========================================================================
# INFOLOG / TASKS endpoints
# ===========================================================================

class InfologTask(BaseModel):
    title: str = Field(..., description="Task subject / title")
    description: Optional[str] = Field(None, description="Detailed description of the task")
    type: Optional[str] = Field("task", description="Type: task | note | phone | email")
    status: Optional[str] = Field("not-started", description="Status: not-started | in-process | done | cancelled")
    priority: Optional[int] = Field(2, description="Priority: 0=low, 1=normal, 2=high")
    percent: Optional[int] = Field(0, description="Completion percentage 0–100")
    due: Optional[str] = Field(None, description="Due date in ISO 8601, e.g. 2024-06-15T17:00:00Z")
    start: Optional[str] = Field(None, description="Start date in ISO 8601")
    responsible: Optional[str] = Field(None, description="Username or email of responsible person")


@app.get("/tasks/list", summary="List tasks / infolog entries", tags=["Tasks (Infolog)"],
         description="Returns infolog entries (tasks, notes, etc.) for the authenticated user.")
def list_tasks(
    search: Optional[str] = None,
    type: Optional[str] = None,
    status: Optional[str] = None,
):
    """
    List tasks/infolog. Filters:
    - **search**: free-text search in title/description
    - **type**: task | note | phone | email
    - **status**: not-started | in-process | done | cancelled
    """
    username = _username()
    params = {}
    if search:
        params["filters[search]"] = search
    if type:
        params["filters[info_type]"] = type
    if status:
        params["filters[info_status]"] = status
    return _get(f"/{username}/infolog/", params or None)


@app.get("/tasks/get/{task_id}", summary="Get a task", tags=["Tasks (Infolog)"],
         description="Retrieve a single infolog entry / task by its numeric ID.")
def get_task(task_id: int):
    username = _username()
    return _get(f"/{username}/infolog/{task_id}")


@app.post("/tasks/create", summary="Create a task", tags=["Tasks (Infolog)"],
          description="Create a new task or infolog entry.")
def create_task(task: InfologTask):
    username = _username()
    body = {
        "@type": "Task",
        "title": task.title,
        "info_type": task.type,
        "status": task.status,
        "priority": task.priority,
        "percentComplete": task.percent,
    }
    if task.description:
        body["description"] = task.description
    if task.due:
        body["due"] = task.due
    if task.start:
        body["start"] = task.start
    if task.responsible:
        body["participants"] = {task.responsible: {"@type": "Participant", "participationStatus": "accepted"}}
    return _post(f"/{username}/infolog/", body)


@app.put("/tasks/update/{task_id}", summary="Update a task", tags=["Tasks (Infolog)"],
         description="Update an existing task / infolog entry by ID.")
def update_task(task_id: int, task: InfologTask):
    username = _username()
    body = {
        "@type": "Task",
        "title": task.title,
        "info_type": task.type,
        "status": task.status,
        "priority": task.priority,
        "percentComplete": task.percent,
    }
    if task.description:
        body["description"] = task.description
    if task.due:
        body["due"] = task.due
    if task.start:
        body["start"] = task.start
    return _put(f"/{username}/infolog/{task_id}", body)


@app.delete("/tasks/delete/{task_id}", summary="Delete a task", tags=["Tasks (Infolog)"],
            description="Permanently delete a task / infolog entry by ID.")
def delete_task(task_id: int):
    username = _username()
    return _delete(f"/{username}/infolog/{task_id}")


# ===========================================================================
# TIMESHEET endpoints
# ===========================================================================

class TimesheetEntry(BaseModel):
    title: str = Field(..., description="Title / description of the timesheet entry")
    start: str = Field(..., description="Start datetime UTC, e.g. 2024-06-01T08:00:00Z")
    duration: int = Field(..., description="Duration in minutes")
    project: Optional[str] = Field(None, description="Project name or code")
    description: Optional[str] = Field(None, description="Detailed notes")
    quantity: Optional[float] = Field(None, description="Quantity (defaults to duration in hours)")
    unitprice: Optional[float] = Field(None, description="Unit price for billing")
    status: Optional[str] = Field(None, description="Status string (e.g. open, billed)")


@app.get("/timesheet/list", summary="List timesheet entries", tags=["Timesheet"],
         description="Returns timesheet entries for the authenticated user.")
def list_timesheet(search: Optional[str] = None):
    username = _username()
    params = {"filters[search]": search} if search else None
    return _get(f"/{username}/timesheet/", params)


@app.get("/timesheet/get/{entry_id}", summary="Get a timesheet entry", tags=["Timesheet"],
         description="Retrieve a single timesheet entry by its numeric ID.")
def get_timesheet_entry(entry_id: int):
    username = _username()
    return _get(f"/{username}/timesheet/{entry_id}")


@app.post("/timesheet/create", summary="Create a timesheet entry", tags=["Timesheet"],
          description="Log a new timesheet entry for work performed.")
def create_timesheet_entry(entry: TimesheetEntry):
    username = _username()
    body = {
        "@type": "timesheet",
        "title": entry.title,
        "start": entry.start,
        "duration": entry.duration,
    }
    if entry.project:
        body["project"] = entry.project
    if entry.description:
        body["description"] = entry.description
    if entry.quantity is not None:
        body["quantity"] = entry.quantity
    if entry.unitprice is not None:
        body["unitprice"] = entry.unitprice
    if entry.status:
        body["status"] = entry.status
    return _post(f"/{username}/timesheet/", body)


@app.delete("/timesheet/delete/{entry_id}", summary="Delete a timesheet entry", tags=["Timesheet"],
            description="Permanently delete a timesheet entry by ID.")
def delete_timesheet_entry(entry_id: int):
    username = _username()
    return _delete(f"/{username}/timesheet/{entry_id}")


# ===========================================================================
# MAIL endpoints
# ===========================================================================

class SendMailRequest(BaseModel):
    to: List[str] = Field(..., description='List of recipient email addresses, e.g. ["user@example.org"]')
    subject: str = Field(..., description="Email subject line")
    body: str = Field(..., description="Email body text (plain text)")
    cc: Optional[List[str]] = Field(None, description="CC recipients")
    bcc: Optional[List[str]] = Field(None, description="BCC recipients")
    identity_id: Optional[int] = Field(None, description="Sender identity ID (from /mail/identities). Uses default if omitted.")
    bodyType: Optional[str] = Field("text/plain", description="Content type: text/plain or text/html")


@app.get("/mail/identities", summary="List mail identities", tags=["Mail"],
         description="Returns available sender identities / email addresses for the authenticated user.")
def list_mail_identities():
    return _get("/mail")


@app.post("/mail/send", summary="Send an email", tags=["Mail"],
          description="Send an email on behalf of the authenticated user via EGroupware's SMTP.")
def send_mail(mail: SendMailRequest):
    body = {
        "to": mail.to,
        "subject": mail.subject,
        "body": mail.body,
        "bodyType": mail.bodyType,
    }
    if mail.cc:
        body["cc"] = mail.cc
    if mail.bcc:
        body["bcc"] = mail.bcc
    if mail.identity_id:
        body["identity_id"] = mail.identity_id
    return _post("/mail/", body)


# ===========================================================================
# Health / info endpoint
# ===========================================================================

@app.get("/", summary="Service info", tags=["Info"],
         description="Returns basic info about this tool server and the configured EGroupware instance.")
def root():
    return {
        "service": "EGroupware OpenAPI Tool Server",
        "version": "1.0.0",
        "egroupware_base": _base_url(),
        "auth_method": "bearer (from request)" if (_request_auth.get() or "").lower().startswith("bearer ") else ("token" if EGW_TOKEN else ("basic" if EGW_USERNAME else "none")),
        "note": "Pass your EGroupware app-token as a Bearer token in Open WebUI's tool-server config to authenticate per-user.",
        "tools": [
            "Calendar: list / get / create / update / delete events",
            "Contacts: list / get / create / delete contacts",
            "Tasks (Infolog): list / get / create / update / delete tasks",
            "Timesheet: list / get / create / delete entries",
            "Mail: list identities / send email",
        ],
        "openapi_docs": "/docs",
        "openapi_schema": "/openapi.json",
    }
