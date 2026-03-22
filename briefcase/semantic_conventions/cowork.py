"""
Semantic conventions for Cowork OpenTelemetry events.

Defines attribute names for all Cowork event types received via OTLP
logs/events protocol. These conventions map 1:1 to the attributes
emitted by the Cowork agent.
"""

# ---------------------------------------------------------------------------
# Service resource attributes (on the OTLP Resource)
# ---------------------------------------------------------------------------
SERVICE_NAME = "service.name"  # always "cowork"
SERVICE_VERSION = "service.version"
HOST_ARCH = "host.arch"
OS_TYPE = "os.type"
OS_VERSION = "os.version"

# ---------------------------------------------------------------------------
# Standard attributes present on every Cowork event
# ---------------------------------------------------------------------------
SESSION_ID = "session.id"
ORGANIZATION_ID = "organization.id"
USER_ACCOUNT_UUID = "user.account_uuid"
USER_ID = "user.id"
USER_EMAIL = "user.email"
TERMINAL_TYPE = "terminal.type"
EVENT_TIMESTAMP = "event.timestamp"  # ISO 8601
EVENT_SEQUENCE = "event.sequence"  # monotonic counter

# ---------------------------------------------------------------------------
# Correlation attribute  shared by all events from a single user prompt
# ---------------------------------------------------------------------------
PROMPT_ID = "prompt.id"  # UUID v4

# ---------------------------------------------------------------------------
# Event type names
# ---------------------------------------------------------------------------
EVENT_USER_PROMPT = "user_prompt"
EVENT_TOOL_RESULT = "tool_result"
EVENT_API_REQUEST = "api_request"
EVENT_API_ERROR = "api_error"
EVENT_TOOL_DECISION = "tool_decision"

ALL_EVENT_TYPES = frozenset(
    {
        EVENT_USER_PROMPT,
        EVENT_TOOL_RESULT,
        EVENT_API_REQUEST,
        EVENT_API_ERROR,
        EVENT_TOOL_DECISION,
    }
)

# ---------------------------------------------------------------------------
# user_prompt attributes
# ---------------------------------------------------------------------------
PROMPT_TEXT = "prompt"
PROMPT_LENGTH = "prompt_length"

# ---------------------------------------------------------------------------
# tool_result attributes
# ---------------------------------------------------------------------------
TOOL_NAME = "tool_name"
TOOL_SUCCESS = "success"
TOOL_DURATION_MS = "duration_ms"
TOOL_ERROR = "error"
TOOL_DECISION_TYPE = "decision_type"
TOOL_DECISION_SOURCE = "decision_source"
TOOL_RESULT_SIZE_BYTES = "tool_result_size_bytes"
TOOL_MCP_SERVER_SCOPE = "mcp_server_scope"
TOOL_PARAMETERS = "tool_parameters"  # JSON string

# ---------------------------------------------------------------------------
# api_request attributes
# ---------------------------------------------------------------------------
API_MODEL = "model"
API_COST_USD = "cost_usd"
API_DURATION_MS = "duration_ms"
API_INPUT_TOKENS = "input_tokens"
API_OUTPUT_TOKENS = "output_tokens"
API_CACHE_READ_TOKENS = "cache_read_tokens"
API_CACHE_CREATION_TOKENS = "cache_creation_tokens"
API_SPEED = "speed"

# ---------------------------------------------------------------------------
# api_error attributes
# ---------------------------------------------------------------------------
API_ERROR_MODEL = "model"
API_ERROR_ERROR = "error"
API_ERROR_STATUS_CODE = "status_code"
API_ERROR_DURATION_MS = "duration_ms"
API_ERROR_ATTEMPT = "attempt"
API_ERROR_SPEED = "speed"

# ---------------------------------------------------------------------------
# tool_decision attributes
# ---------------------------------------------------------------------------
TOOL_DECISION_TOOL_NAME = "tool_name"
TOOL_DECISION_DECISION = "decision"
TOOL_DECISION_SOURCE_ATTR = "source"

# ---------------------------------------------------------------------------
# Per-event required attribute sets (for validation)
# ---------------------------------------------------------------------------
REQUIRED_ATTRS = {
    EVENT_USER_PROMPT: {PROMPT_TEXT, PROMPT_LENGTH},
    EVENT_TOOL_RESULT: {TOOL_NAME, TOOL_SUCCESS, TOOL_DURATION_MS},
    EVENT_API_REQUEST: {API_MODEL, API_COST_USD, API_DURATION_MS, API_INPUT_TOKENS, API_OUTPUT_TOKENS},
    EVENT_API_ERROR: {API_ERROR_MODEL, API_ERROR_ERROR, API_ERROR_STATUS_CODE},
    EVENT_TOOL_DECISION: {TOOL_DECISION_TOOL_NAME, TOOL_DECISION_DECISION, TOOL_DECISION_SOURCE_ATTR},
}

# Attributes that may contain sensitive/PII data and should be redacted
SENSITIVE_ATTRIBUTES = frozenset(
    {
        PROMPT_TEXT,
        TOOL_PARAMETERS,
        TOOL_ERROR,
        USER_EMAIL,
        API_ERROR_ERROR,
    }
)
