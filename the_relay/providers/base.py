"""Provider-neutral interface implemented by Groq and the stand-in.

WHY AN INTERFACE?
-----------------
The orchestrator must not know whether a reply came from a real hosted model or
a local simulator. If it did, the stand-in would not be testing the real code
path. Both providers accept the same :class:`ModelRequest` and return the same
:class:`ModelResponse`; only the ``source`` field differs ("real" vs
"stand-in"), and that field is what keeps the measurements honest.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Awaitable, Callable

from the_relay.types import ModelRequest, ModelResponse

# Optional callback invoked for every streamed piece of text. The arguments are
# (text_piece, elapsed_ms_since_call_started). The pipeline uses the first call
# to record time-to-first-token.
TokenCallback = Callable[[str, float], Awaitable[None] | None]


class ModelProvider(ABC):
    """Common interface for a model service."""

    source: str
    model_id: str

    @abstractmethod
    async def generate(
        self,
        request: ModelRequest,
        on_token: TokenCallback | None = None,
    ) -> ModelResponse:
        """Produce one completion, optionally streaming pieces to ``on_token``."""

    async def aclose(self) -> None:
        """Release network resources. The stand-in has nothing to release."""
        return None
