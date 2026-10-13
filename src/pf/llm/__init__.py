"""LLM steps, used only where judgement is needed (reading a requirement document, turning English into CEL,
explaining compiled logic back in English). Every call:

  * runs a named skill from the pipeline-factory plugin through the Claude Agent SDK,
  * must return JSON matching a pydantic model (Agent SDK structured output), re-validated here,
  * is logged with model, skill hash, prompt hash and cost for the evidence trail,
  * can be replayed from recorded fixtures, so tests and CI are deterministic and offline.
"""
from .base import LLMMeta, run_structured

__all__ = ["LLMMeta", "run_structured"]
