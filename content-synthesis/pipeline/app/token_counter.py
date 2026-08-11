"""
Async-safe token counter for aggregating Bedrock usage across parallel tasks.
"""
import asyncio
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )


class TokenCounter:
    def __init__(self):
        self._lock = asyncio.Lock()
        self._totals = TokenUsage()
        self._per_task: dict[str, TokenUsage] = {}

    async def record(self, task_name: str, usage: dict) -> None:
        entry = TokenUsage(
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
        )
        async with self._lock:
            self._per_task[task_name] = self._per_task.get(task_name, TokenUsage()) + entry
            self._totals = self._totals + entry

    def log_summary(self) -> None:
        logger.info("=" * 50)
        logger.info("TOKEN USAGE SUMMARY")
        logger.info("=" * 50)
        for name, u in sorted(self._per_task.items()):
            logger.info("  %-40s  in: %6d  out: %6d  total: %7d",
                        name, u.input_tokens, u.output_tokens, u.total_tokens)
        logger.info("-" * 50)
        logger.info("  %-40s  in: %6d  out: %6d  total: %7d",
                     "TOTAL", self._totals.input_tokens,
                     self._totals.output_tokens, self._totals.total_tokens)
        logger.info("=" * 50)

    def to_dict(self) -> dict:
        return {
            "total": {"input_tokens": self._totals.input_tokens,
                      "output_tokens": self._totals.output_tokens,
                      "total_tokens": self._totals.total_tokens},
            "per_task": {n: {"input_tokens": u.input_tokens,
                             "output_tokens": u.output_tokens,
                             "total_tokens": u.total_tokens}
                         for n, u in self._per_task.items()},
        }
