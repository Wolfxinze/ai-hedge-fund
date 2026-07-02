"""Monitoring: run preset analysis flows over watchlists into research reports.

Research-only. Every report passes through ``serialize_report`` and carries a
disclaimer. The default analyzing flow is the ai-hedge-fund committee built from
``monitor.selected_analysts`` (#51, ``committee_flow.py``); TradingAgents' debate
graph remains available as an injectable adapter via a process seam.
"""
