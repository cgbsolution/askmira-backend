"""The brand became AskMiraa (assistant: Miraa) on 2026-11-10.

What people see changes; what other systems check does not: the sign-in token's
issuer stays "AskMira", so live sessions and the signed launches into the
connected products keep working.
"""

from __future__ import annotations

from datetime import date

import jwt

from app.core.config import settings
from app.core.security import create_access_token
from app.services.companion import direct
from app.services.companion.config import CompanionConfig
from app.services.companion.tools import TOOLS


def test_people_see_the_new_name_but_tokens_keep_the_old_issuer() -> None:
    assert settings.BRAND_NAME == "AskMiraa"
    token = create_access_token(user_id="u-1", tenant_id="t-1", role="employee")
    claims = jwt.decode(token, options={"verify_signature": False})
    assert claims["iss"] == "AskMira"


def test_the_assistant_is_called_miraa_by_default() -> None:
    assert CompanionConfig().name == "Miraa"


def test_greeting_either_spelling_still_finds_the_answer() -> None:
    tools = [tool for tool in TOOLS.values() if not tool.feature and not tool.needs_ai]
    today = date(2026, 10, 15)
    for text in (
        "Hi Mira payroll for september",
        "Hi Miraa payroll for september",
        "hey askmiraa payroll for september",
    ):
        plan = direct.plan(text, tools, today=today, hr=True)
        assert plan.tool == "payroll_summary", text
    for text in ("thanks Miraa", "thank you mira", "Miraa"):
        assert direct.plan(text, tools, today=today, hr=True).tool is None
