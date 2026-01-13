import os
import sqlite3
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Path, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


def _utc_now_iso() -> str:
    """Return current UTC time in ISO-8601 format with 'Z' suffix."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# PUBLIC_INTERFACE
def get_db_path() -> str:
    """Get the configured SQLite database path.

    Environment variables:
      - TODO_SQLITE_DB_PATH: preferred explicit path to SQLite db file
      - SQLITE_DB_PATH: alternative name (useful with generic tooling)

    Returns:
        Absolute or relative path to SQLite database file.
    """
    return os.getenv("TODO_SQLITE_DB_PATH") or os.getenv("SQLITE_DB_PATH") or "todo.db"


def _connect() -> sqlite3.Connection:
    """Create a SQLite connection with row_factory for dict-like access."""
    conn = sqlite3.connect(get_db_path(), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db() -> None:
    """Initialize the database schema if it does not exist.

    Schema must align with the work item requirement:
      id INTEGER PK
      title TEXT NOT NULL
      completed INTEGER DEFAULT 0
      created_at TEXT
      updated_at TEXT
    """
    conn = _connect()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS todos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                completed INTEGER NOT NULL DEFAULT 0,
                created_at TEXT,
                updated_at TEXT
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


class TodoBase(BaseModel):
    """Shared Todo fields."""
    title: str = Field(..., min_length=1, description="Short title/description of the todo item.")


class TodoCreate(TodoBase):
    """Request model for creating a todo."""
    completed: Optional[bool] = Field(
        default=False,
        description="Optional initial completion state. Defaults to false.",
    )


class TodoUpdate(BaseModel):
    """Request model for updating a todo (full update semantics)."""
    title: str = Field(..., min_length=1, description="Updated title for the todo item.")
    completed: bool = Field(..., description="Updated completion state.")


class Todo(BaseModel):
    """Response model for a todo item."""
    id: int = Field(..., description="Unique identifier of the todo item.")
    title: str = Field(..., description="Title/description of the todo item.")
    completed: bool = Field(..., description="Whether the todo is completed.")
    created_at: Optional[str] = Field(None, description="ISO-8601 UTC timestamp when created.")
    updated_at: Optional[str] = Field(None, description="ISO-8601 UTC timestamp when last updated.")


class TodoList(BaseModel):
    """Response model for a list of todos."""
    items: List[Todo] = Field(..., description="List of todo items.")
    total: int = Field(..., description="Total number of todo items returned.")


def _row_to_todo(row: sqlite3.Row) -> Todo:
    """Convert a SQLite row into a Todo model."""
    return Todo(
        id=int(row["id"]),
        title=str(row["title"]),
        completed=bool(int(row["completed"])),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


openapi_tags = [
    {"name": "Health", "description": "Service health and diagnostics."},
    {"name": "Todos", "description": "CRUD operations for todo items."},
]

app = FastAPI(
    title="Todo Backend API",
    description=(
        "SQLite-backed Todo API providing CRUD endpoints and a toggle-complete action.\n\n"
        "DB configuration:\n"
        "- Set TODO_SQLITE_DB_PATH (or SQLITE_DB_PATH) to point to the SQLite .db file.\n"
        "- If unset, defaults to ./todo.db (relative to the server working directory)."
    ),
    version="1.0.0",
    openapi_tags=openapi_tags,
)

# CORS: allow local React dev server on port 3000 (plus localhost variants).
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    """Initialize database schema on application startup."""
    _init_db()


@app.get(
    "/",
    tags=["Health"],
    summary="Health check",
    description="Basic liveness endpoint.",
    operation_id="healthCheck",
)
# PUBLIC_INTERFACE
def health_check():
    """Health check endpoint.

    Returns:
        A small JSON object indicating the service is up.
    """
    return {"message": "Healthy"}


@app.post(
    "/todos",
    response_model=Todo,
    status_code=201,
    tags=["Todos"],
    summary="Create a todo",
    description="Create a new todo item.",
    operation_id="createTodo",
)
# PUBLIC_INTERFACE
def create_todo(payload: TodoCreate) -> Todo:
    """Create a new todo item.

    Args:
        payload: TodoCreate model containing title and optional completed state.

    Returns:
        The created Todo item.
    """
    now = _utc_now_iso()
    completed_int = 1 if payload.completed else 0

    conn = _connect()
    try:
        cur = conn.execute(
            """
            INSERT INTO todos (title, completed, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (payload.title, completed_int, now, now),
        )
        conn.commit()
        todo_id = cur.lastrowid
        row = conn.execute("SELECT * FROM todos WHERE id = ?", (todo_id,)).fetchone()
        return _row_to_todo(row)
    finally:
        conn.close()


@app.get(
    "/todos",
    response_model=TodoList,
    tags=["Todos"],
    summary="List todos",
    description="Fetch all todo items ordered by id descending (newest first).",
    operation_id="listTodos",
)
# PUBLIC_INTERFACE
def list_todos() -> TodoList:
    """List all todo items.

    Returns:
        TodoList including items and total count.
    """
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM todos ORDER BY id DESC").fetchall()
        items = [_row_to_todo(r) for r in rows]
        return TodoList(items=items, total=len(items))
    finally:
        conn.close()


@app.get(
    "/todos/{id}",
    response_model=Todo,
    tags=["Todos"],
    summary="Get todo by id",
    description="Fetch a single todo item by its id.",
    operation_id="getTodo",
)
# PUBLIC_INTERFACE
def get_todo(
    id: int = Path(..., ge=1, description="ID of the todo item."),
) -> Todo:
    """Get a todo by id.

    Args:
        id: Todo ID.

    Returns:
        The Todo item.

    Raises:
        HTTPException: 404 if not found.
    """
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM todos WHERE id = ?", (id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Todo not found")
        return _row_to_todo(row)
    finally:
        conn.close()


@app.put(
    "/todos/{id}",
    response_model=Todo,
    tags=["Todos"],
    summary="Update todo",
    description="Replace title and completed state for a todo item.",
    operation_id="updateTodo",
)
# PUBLIC_INTERFACE
def update_todo(
    payload: TodoUpdate,
    id: int = Path(..., ge=1, description="ID of the todo item."),
) -> Todo:
    """Update an existing todo item.

    Args:
        payload: TodoUpdate model (title and completed required).
        id: Todo ID.

    Returns:
        Updated Todo.

    Raises:
        HTTPException: 404 if not found.
    """
    conn = _connect()
    try:
        existing = conn.execute("SELECT * FROM todos WHERE id = ?", (id,)).fetchone()
        if existing is None:
            raise HTTPException(status_code=404, detail="Todo not found")

        now = _utc_now_iso()
        completed_int = 1 if payload.completed else 0

        conn.execute(
            """
            UPDATE todos
            SET title = ?, completed = ?, updated_at = ?
            WHERE id = ?
            """,
            (payload.title, completed_int, now, id),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM todos WHERE id = ?", (id,)).fetchone()
        return _row_to_todo(row)
    finally:
        conn.close()


@app.delete(
    "/todos/{id}",
    status_code=204,
    tags=["Todos"],
    summary="Delete todo",
    description="Delete a todo item by id.",
    operation_id="deleteTodo",
)
# PUBLIC_INTERFACE
def delete_todo(
    id: int = Path(..., ge=1, description="ID of the todo item."),
) -> Response:
    """Delete a todo item.

    Args:
        id: Todo ID.

    Returns:
        Empty 204 response.

    Raises:
        HTTPException: 404 if not found.
    """
    conn = _connect()
    try:
        cur = conn.execute("DELETE FROM todos WHERE id = ?", (id,))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Todo not found")
        return Response(status_code=204)
    finally:
        conn.close()


@app.patch(
    "/todos/{id}/toggle",
    response_model=Todo,
    tags=["Todos"],
    summary="Toggle completion",
    description="Flip the completed state for a todo item.",
    operation_id="toggleTodo",
)
# PUBLIC_INTERFACE
def toggle_todo(
    id: int = Path(..., ge=1, description="ID of the todo item."),
) -> Todo:
    """Toggle the completed state of a todo.

    Args:
        id: Todo ID.

    Returns:
        Updated Todo with completion flipped.

    Raises:
        HTTPException: 404 if not found.
    """
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM todos WHERE id = ?", (id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Todo not found")

        current_completed = int(row["completed"])
        new_completed = 0 if current_completed == 1 else 1
        now = _utc_now_iso()

        conn.execute(
            """
            UPDATE todos
            SET completed = ?, updated_at = ?
            WHERE id = ?
            """,
            (new_completed, now, id),
        )
        conn.commit()

        updated = conn.execute("SELECT * FROM todos WHERE id = ?", (id,)).fetchone()
        return _row_to_todo(updated)
    finally:
        conn.close()
