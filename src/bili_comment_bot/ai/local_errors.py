"""Allowlisted diagnostics shared across the local subprocess boundary."""

LOCAL_FAILURES = frozenset(
    {
        "local_context_budget",
        "local_media_invalid",
        "local_completion_incomplete",
        "local_dependencies_missing",
        "local_models_not_prepared",
        "local_memory_budget",
        "local_response_budget",
        "local_inference_unavailable",
        "local_inference_failed",
        "local_inference_timeout",
        "local_requires_apple_silicon",
    }
)


class LocalWorkerError(ValueError):
    def __init__(self, reason):
        self.reason = reason if reason in LOCAL_FAILURES else "local_inference_failed"
        super().__init__(self.reason)
