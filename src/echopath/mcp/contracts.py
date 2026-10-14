from __future__ import annotations

from typing import Any, Dict


JSON = Dict[str, Any]
SERVER_NAME = "echopath"
SERVER_TITLE = "EchoPath MCP Server"
DESKTOP_SKILL_NAME = "pyautogui-desktop"

RESOURCE_CONFIG = "echopath://config"
RESOURCE_SKILLS = "echopath://skills"
RESOURCE_SESSIONS = "echopath://sessions"
RESOURCE_MEMORY_INDEX = "echopath://memory/consolidated/index"
RESOURCE_SESSION_TEMPLATE = "echopath://sessions/{session_id}"
RESOURCE_SESSION_DEBUG_TEMPLATE = "echopath://sessions/{session_id}/debug"

TOOL_DEFINITIONS: list[JSON] = [
    {
        'name': 'echopath_list_runs',
        'description': 'List ongoing, paused and recent runs across local workers.',
        'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
    },
    {
        'name': 'echopath_control_run',
        'description': 'Request pause, resume or stop; wait for paused before using the desktop.',
        'inputSchema': {
            'type': 'object',
            'properties': {'run_id': {'type': 'string'}, 'action': {'type': 'string', 'enum': ['pause', 'resume', 'stop']}},
            'required': ['run_id', 'action'], 'additionalProperties': False,
        },
    },
    {
        'name': 'echopath_start_task',
        'description': 'Start a tracked task on the local desktop or a connected VM and return immediately with its task ID.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'task': {'type': 'string'},
                'target': {'type': 'string', 'enum': ['local', 'vm'], 'default': 'vm'},
                'allow_desktop_control': {'type': 'boolean', 'default': False},
                'vm_endpoint': {'type': 'string'},
                'allow_native_handoff': {
                    'type': 'boolean', 'default': False,
                    'description': 'Host can execute native segments on the same desktop and call echopath_resume_task.',
                },
            },
            'required': ['task'], 'additionalProperties': False,
        },
    },
    {
        'name': 'echopath_resume_task',
        'description': 'Answer a tracked task that is awaiting_native with its declared outputs, or cancel it.',
        'inputSchema': {
            'type': 'object', 'additionalProperties': False,
            'properties': {
                'run_id': {'type': 'string'},
                'outputs': {'type': 'object', 'additionalProperties': {'type': 'string'}},
                'cancel': {'type': 'boolean', 'default': False},
            },
            'required': ['run_id'],
        },
    },
    {
        'name': 'echopath_get_task',
        'description': 'Get a tracked task by task ID, including its activity and final result.',
        'inputSchema': {
            'type': 'object', 'properties': {'run_id': {'type': 'string'}},
            'required': ['run_id'], 'additionalProperties': False,
        },
    },
    {
        "name": "echopath_create_session",
        "description": "Create an EchoPath chat session and return its session_id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Optional human-readable session title.",
                }
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "echopath_run_task",
        "description": (
            "Send one task to EchoPath. Live desktop control is disabled unless "
            "allow_desktop_control is true."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "User task text to send to EchoPath.",
                },
                "session_id": {
                    "type": "string",
                    "description": "Existing EchoPath session_id. Omit to create one.",
                },
                "allow_desktop_control": {
                    "type": "boolean",
                    "default": False,
                    "description": "Enable real desktop GUI observation and actions.",
                },
                "allow_native_handoff": {
                    "type": "boolean", "default": False,
                    "description": "Host can execute native computer-use segments on the same desktop and call echopath_resume.",
                },
            },
            "required": ["message"],
            "additionalProperties": False,
        },
    },
    {
        "name": "echopath_resume",
        "description": "Resume a paused replay after native computer use, or cancel it. Live return state is verified.",
        "inputSchema": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "session_id": {"type": "string"},
                "continuation_token": {"type": "string"},
                "outputs": {"type": "object", "additionalProperties": {"type": "string"}},
                "cancel": {"type": "boolean", "default": False},
            },
            "required": ["session_id", "continuation_token"],
        },
    },
    {
        "name": "echopath_get_session",
        "description": "Return an EchoPath session with messages and debug state.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "EchoPath session_id."}
            },
            "required": ["session_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "echopath_list_sessions",
        "description": "List in-memory EchoPath sessions for this MCP server process.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "echopath_get_status",
        "description": "Return the current debug status for an EchoPath session.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "EchoPath session_id."}
            },
            "required": ["session_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "echopath_smoke_check",
        "description": (
            "Run safe EchoPath health checks. The model bridge is only called when "
            "include_model_bridge is true."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "include_model_bridge": {
                    "type": "boolean",
                    "default": False,
                    "description": "Call the configured model bridge with a trivial prompt.",
                }
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "echopath_consolidate",
        "description": (
            "Consolidate a recorded GUI run into the memory library, or "
            "deprecate a learned memory path."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "string",
                    "description": (
                        "gui_runs run id. Omit to use the most recent "
                        "completed run."
                    ),
                },
                "task_intent": {
                    "type": "string",
                    "description": "Override the task intent used for consolidation.",
                },
                "deprecate_path_id": {
                    "type": "string",
                    "description": (
                        "Instead of consolidating, deprecate this memory path id."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": "Reason recorded when deprecating.",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "echopath_list_memories",
        "description": (
            "List learned memories from both stores: memory-repo paths and "
            "consolidated replay records."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "include_candidates": {
                    "type": "boolean",
                    "default": False,
                    "description": "Also list unpromoted candidate paths.",
                },
                "limit": {
                    "type": "integer",
                    "default": 50,
                    "description": "Maximum entries per store.",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "echopath_clear_history",
        "description": "Clear in-memory sessions and local EchoPath history artifacts.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "confirm": {
                    "type": "boolean",
                    "default": False,
                    "description": "Must be true to perform the clear operation.",
                }
            },
            "required": ["confirm"],
            "additionalProperties": False,
        },
    },
]

RESOURCE_DEFINITIONS: list[JSON] = [
    {
        "uri": RESOURCE_CONFIG,
        "name": "EchoPath config",
        "description": "Credential-free normalized EchoPath configuration.",
        "mimeType": "application/json",
    },
    {
        "uri": RESOURCE_SKILLS,
        "name": "EchoPath skills",
        "description": "Configured EchoPath skill names and markdown paths.",
        "mimeType": "application/json",
    },
    {
        "uri": RESOURCE_SESSIONS,
        "name": "EchoPath sessions",
        "description": "In-memory EchoPath sessions for this MCP process.",
        "mimeType": "application/json",
    },
    {
        "uri": RESOURCE_MEMORY_INDEX,
        "name": "EchoPath consolidated memory index",
        "description": "Local consolidated replay memory index, when present.",
        "mimeType": "application/json",
    },
]

RESOURCE_TEMPLATE_DEFINITIONS: list[JSON] = [
    {
        "uriTemplate": RESOURCE_SESSION_TEMPLATE,
        "name": "EchoPath session",
        "description": "Session messages and debug metadata.",
        "mimeType": "application/json",
    },
    {
        "uriTemplate": RESOURCE_SESSION_DEBUG_TEMPLATE,
        "name": "EchoPath session debug state",
        "description": "Current projected run status for a session.",
        "mimeType": "application/json",
    },
]

PROMPT_DEFINITIONS: list[JSON] = [
    {
        "name": "echopath_plan_task",
        "description": "Ask EchoPath to produce a plan without executing actions.",
        "arguments": [
            {
                "name": "task",
                "description": "Task to plan.",
                "required": True,
            }
        ],
    },
    {
        "name": "echopath_visible_desktop_task",
        "description": "Template for explicitly authorized visible desktop work.",
        "arguments": [
            {
                "name": "task",
                "description": "Desktop task to perform.",
                "required": True,
            }
        ],
    },
    {
        "name": "echopath_replay_diagnostics",
        "description": "Inspect replay and session diagnostics for a task.",
        "arguments": [
            {
                "name": "session_id",
                "description": "EchoPath session_id.",
                "required": True,
            }
        ],
    },
]
