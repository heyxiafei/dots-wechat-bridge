"""Explicit offline echo, never a substitute for a connected assistant."""
from .contracts import BackendCapabilities, BackendResult, UnsupportedOperations


class EchoBackend(UnsupportedOperations):
    backend_id = "echo"
    capabilities = BackendCapabilities(text=True, sessions=True)

    def health(self):
        return {"status": "READY", "mode": "simulation", "reason": "offline_echo"}

    def submit(self, request):
        if request.attachments:
            return BackendResult(request.request_id, "unsupported", error_code="attachments_unsupported")
        return BackendResult(request.request_id, "succeeded", text="[模拟回显] " + request.text,
                             backend_session=request.backend_session)
