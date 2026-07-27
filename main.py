# -*- coding: utf-8 -*-

"""Compatibility exports for integrations that still import ``main``."""

from expertsearch.main import get_dynamic_recommendations, run_agent_task

__all__ = ["get_dynamic_recommendations", "run_agent_task"]
