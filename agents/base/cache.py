import hashlib
import json
import time


from agents.base.model import (
    TaskRequest,
    TaskResponse,
)

import structlog


logger = structlog.get_logger()


class TaskCache:
    """Simple cache for task results"""

    def __init__(self, ttl_seconds: int = 3600):
        self._cache: dict[str, tuple[TaskResponse, float]] = {}
        self.ttl_seconds = ttl_seconds

    def _generate_key(self, request: TaskRequest) -> str:
        # generate cache key from task request
        # hash the essential parts of the request
        content = f"{request.task_type}:{request.objective}:{json.dumps(request.data, sort_keys=True)}"
        return hashlib.sha256(content.encode()).hexdigest()

    def get(self, request: TaskRequest) -> TaskResponse | None:
        # get cached response if exists and not expired
        key = self._generate_key(request)
        if key in self._cache:
            response, timestamp = self._cache[key]
            if time.time() - timestamp < self.ttl_seconds:
                logger.info("cache_hit", task_id=request.task_id, key=key)
                return response
            else:
                del self._cache[key]
        return None

    def set(self, request: TaskRequest, response: TaskResponse) -> None:
        # cache a response
        key = self._generate_key(request)
        self._cache[key] = (response, time.time())
        logger.info("cache_set", task_id=request.task_id, key=key)
