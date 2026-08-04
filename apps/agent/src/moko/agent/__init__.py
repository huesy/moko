"""Moko's brain — the perceive → respond → remember loop, plus curiosity."""

from .brain import Brain
from .curiosity import detect_gaps, detect_gaps_for
from .dream import dream, should_dream

__all__ = ["Brain", "detect_gaps", "detect_gaps_for", "dream", "should_dream"]
