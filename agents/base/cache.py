import hashlib
import json
import time
import sys
from collections import OrderedDict

from agents.base.model import (
    TaskRequest,
    TaskResponse,
)

import structlog


logger = structlog.get_logger()


class TaskCache:
    """Cache with LRU eviction and size limits"""

    def __init__(
        self,
        ttl_seconds: int = 3600,
        max_items: int = 1000,
        max_memory_mb: int = 100
    ):
        self._cache = OrderedDict()
        self.ttl_seconds = ttl_seconds
        self.max_items = max_items
        self.max_memory_bytes = max_memory_mb * 1024 * 1024

    def _evict_if_needed(self):
        # evict oldest items if cache is too large

        # Check item count
        while len(self._cache) > self.max_items:
            self._cache.popitem(last=False)

        # Check memory usage
        cache_size = sys.getsizeof(self._cache)
        while cache_size > self.max_memory_bytes and self._cache:
            self._cache.popitem(last=False)
            cache_size = sys.getsizeof(self._cache)

    def _generate_key(self, request: TaskRequest) -> str:
        # generate cache key from task request
        # hash the essential parts of the request
        content = f"{request.task_type}:{request.objective}:{json.dumps(request.data, sort_keys=True)}"
        return hashlib.sha256(content.encode()).hexdigest()

    def get(self, request: TaskRequest) -> TaskResponse | None:
        key = self._generate_key(request)
        if key in self._cache:
            response, timestamp = self._cache.pop(key)
            if time.time() - timestamp < self.ttl_seconds:
                # Move to end (most recently used)
                self._cache[key] = (response, timestamp)
                return response
            # Expired, don't re-add
        return None

    def set(self, request: TaskRequest, response: TaskResponse) -> None:
        key = self._generate_key(request)
        self._cache[key] = (response, time.time())
        self._evict_if_needed()
