"""Local token estimation.

WHY ESTIMATE AT ALL?
--------------------
The quota manager must decide *before* a call whether the call is affordable.
The provider only reports exact usage *after* the call. So the system needs a
local, conservative estimate for admission, and then replaces it with the
provider's real usage when the response arrives.

WHY NOT A REAL TOKENIZER?
-------------------------
Groq's models do not all share one tokenizer, and the tokenizer is not part of
the documented API contract. Installing a third-party tokenizer would create a
new dependency and still not guarantee the provider's count. A character-based
estimate is honest, cheap, and documented as an estimate. The real number is
always recorded when the provider supplies it.

The estimate is deliberately biased slightly high: underestimating tokens
causes self-inflicted 429s, while overestimating only makes us a little more
conservative.
"""

from __future__ import annotations

import math
import re

# English prose averages ~4 characters per token; technical text with symbols
# and numbers is closer to ~3.5. We use 3.5 so the estimate is conservative.
_CHARS_PER_TOKEN = 3.5

# Chat messages carry a small per-message overhead in OpenAI-compatible APIs.
_MESSAGE_OVERHEAD_TOKENS = 4


def estimate_tokens(text: str) -> int:
    """Estimate the number of model tokens in ``text``.

    This is an estimate, not a measurement. It is used only for admission and
    for stand-in accounting; real calls overwrite it with provider usage.
    """
    if not text:
        return 0
    # Count characters excluding repeated whitespace, which tokenizers collapse.
    compact = re.sub(r"\s+", " ", text).strip()
    return max(1, math.ceil(len(compact) / _CHARS_PER_TOKEN))


def estimate_messages_tokens(messages: list[dict[str, str]]) -> int:
    """Estimate the input tokens of a chat-style message list.

    Each message has a role and content; the small per-message overhead
    accounts for the chat template the provider wraps around it.
    """
    total = 0
    for message in messages:
        total += _MESSAGE_OVERHEAD_TOKENS
        total += estimate_tokens(str(message.get("role", "")))
        total += estimate_tokens(str(message.get("content", "")))
    return total
