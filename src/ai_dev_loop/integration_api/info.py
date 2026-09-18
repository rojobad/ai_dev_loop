"""Integration info response builder."""

from __future__ import annotations

from ai_dev_loop import __version__
from ai_dev_loop.integration_api.models import IntegrationInfoData


def build_integration_info_data() -> IntegrationInfoData:
    return IntegrationInfoData.model_validate(
        {
            "aiDevLoopVersion": __version__,
            "capabilities": {
                "runs": False,
                "sequences": False,
                "reviewInspection": False,
                "processOutput": False,
                "codexCapacity": False,
            },
        }
    )
