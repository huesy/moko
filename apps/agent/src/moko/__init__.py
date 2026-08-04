"""Moko — a warm, curious personal AI subject.

See MISSION.md for what Moko is, and SCHEMA.md for how it remembers.
This is the package root; the milestone build lives in the submodules.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("moko")  # baked into the wheel by hatch-vcs at build time
except PackageNotFoundError:  # running from a source tree that isn't installed
    __version__ = "0.0.0+unknown"
