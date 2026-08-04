"""Moko's tools — capabilities it can invoke, gated by risk. See `base.py`."""

from .base import SAFE, SENSITIVE, AgentTool
from .consent import ALLOW, ASK, DENY, decide
from .extend import EXTEND_TOOLS
from .files import FILE_TOOLS
from .process import PROCESS_TOOLS
from .system import SYSTEM_TOOLS
from .web import WEB_TOOLS

# Everything Moko can reach for. Safe tools run; sensitive ones go through the
# consent flow. `execute_command` classifies each command (see tools/shell.py);
# `processes` manages the background ones it started; `secrets`/`author_skill`
# let Moko acquire credentials and teach itself new abilities.
ALL_TOOLS = [*WEB_TOOLS, *FILE_TOOLS, *SYSTEM_TOOLS, *PROCESS_TOOLS, *EXTEND_TOOLS]

__all__ = [
    "SAFE", "SENSITIVE", "AgentTool", "ALLOW", "ASK", "DENY", "decide",
    "WEB_TOOLS", "FILE_TOOLS", "SYSTEM_TOOLS", "PROCESS_TOOLS", "EXTEND_TOOLS", "ALL_TOOLS",
]
