"""Version one, platform-neutral backend boundary. No transport credentials."""
from dataclasses import dataclass, field
from typing import Mapping, Optional, Protocol, Tuple
import math
import re

VERSION = 1
RESULT_STATES = frozenset({"succeeded", "failed", "pending", "unknown", "cancelled", "unsupported"})


@dataclass(frozen=True)
class AttachmentRef:
    attachment_id: str
    media_type: str
    size_bytes: Optional[int]
    sha256: Optional[str]

    def __post_init__(self):
        if (not isinstance(self.attachment_id, str) or not 0 < len(self.attachment_id) <= 256
                or not isinstance(self.media_type, str) or not 0 < len(self.media_type) <= 128
                or self.size_bytes is not None and (type(self.size_bytes) is not int or self.size_bytes < 0)
                or self.sha256 is not None and (not isinstance(self.sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", self.sha256))):
            raise ValueError("invalid_attachment_reference")


@dataclass(frozen=True)
class BackendRequest:
    request_id: str
    message_id: str
    conversation_id: str
    text: str
    backend_session: Optional[str] = None
    model: Optional[str] = None
    config_revision: int = 1
    deadline_at: Optional[float] = None
    attachments: Tuple[AttachmentRef, ...] = ()
    history: Tuple[Tuple[str, str], ...] = ()
    version: int = VERSION

    def __post_init__(self):
        if self.version != VERSION or type(self.config_revision) is not int or self.config_revision < 1:
            raise ValueError("unsupported_request_version")
        if any(not isinstance(v, str) or not v or len(v) > 256 for v in
               (self.request_id, self.message_id, self.conversation_id)):
            raise ValueError("invalid_request_identity")
        if not isinstance(self.text, str) or len(self.text.encode("utf-8")) > 65536:
            raise ValueError("invalid_request_text")
        if self.deadline_at is not None and (type(self.deadline_at) not in (int, float) or not math.isfinite(self.deadline_at) or self.deadline_at <= 0):
            raise ValueError("invalid_request_deadline")
        if any(role not in ("user", "assistant") or not isinstance(text, str) for role, text in self.history):
            raise ValueError("invalid_history")


@dataclass(frozen=True)
class BackendCapabilities:
    text: bool = True
    images: bool = False
    files: bool = False
    audio: bool = False
    video: bool = False
    streaming: bool = False
    sessions: bool = False
    tools: bool = False
    approvals: bool = False
    cancellation: bool = False
    task_query: bool = False
    callbacks: bool = False
    usage: bool = False


@dataclass(frozen=True)
class BackendResult:
    request_id: str
    status: str
    text: Optional[str] = None
    task_id: Optional[str] = None
    backend_session: Optional[str] = None
    error_code: Optional[str] = None
    usage: Mapping[str, int] = field(default_factory=dict)
    version: int = VERSION

    def __post_init__(self):
        if self.version != VERSION or self.status not in RESULT_STATES:
            raise ValueError("invalid_result_status")
        if self.status != "succeeded" and self.text is not None:
            raise ValueError("non_success_text")
        if self.status == "succeeded" and not isinstance(self.text, str):
            raise ValueError("missing_result_text")


@dataclass(frozen=True)
class BackendEvent:
    request_id: str
    task_id: str
    event_id: str
    kind: str
    result: Optional[BackendResult] = None
    version: int = VERSION


class Backend(Protocol):
    backend_id: str
    capabilities: BackendCapabilities

    def health(self) -> Mapping[str, str]: ...
    def submit(self, request: BackendRequest) -> BackendResult: ...
    def status(self, task_id: str) -> BackendResult: ...
    def cancel(self, task_id: str) -> BackendResult: ...


class UnsupportedOperations:
    def status(self, task_id):
        return BackendResult(task_id, "unsupported", error_code="task_query_unsupported")

    def cancel(self, task_id):
        return BackendResult(task_id, "unsupported", error_code="cancellation_unsupported")
