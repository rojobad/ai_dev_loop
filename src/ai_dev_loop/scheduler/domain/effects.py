"""Scheduler effect kinds for tick exercises and attempt executor boundaries."""

from __future__ import annotations

from ai_dev_loop.scheduler.domain.cursor_contract import (
    CREATE_CHAT_EFFECT_KIND,
    INGEST_CURSOR_RESULT_EFFECT_KIND,
    NORMALIZE_STAGING_EFFECT_KIND,
    PREFLIGHT_EFFECT_KIND,
    RUN_CURSOR_TURN_EFFECT_KIND,
)

SYNTHETIC_SELF_TEST_EFFECT_KIND = "synthetic.self_test"
SYNTHETIC_SELF_TEST_EFFECT_ID = "synthetic-self-test"

FAKE_AGENT_SELF_TEST_EFFECT_KIND = "fake_agent.self_test"
FAKE_AGENT_SELF_TEST_EFFECT_ID = "fake-agent-self-test"

CURSOR_ATTEMPT_EFFECT_KINDS = frozenset(
    {
        CREATE_CHAT_EFFECT_KIND,
        RUN_CURSOR_TURN_EFFECT_KIND,
    }
)

LOCAL_CURSOR_EFFECT_KINDS = frozenset(
    {
        PREFLIGHT_EFFECT_KIND,
        INGEST_CURSOR_RESULT_EFFECT_KIND,
        NORMALIZE_STAGING_EFFECT_KIND,
    }
)
