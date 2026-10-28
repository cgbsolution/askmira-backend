"""Measure Ask Mira's AI planner on the question bank.

The rule-based planner is tested on every question in tests/companion (pytest).
The AI planner can only be measured with a model, so this script sends a sample
of the same questions to the configured model, exactly as a turn would (the same
rules, tool list and answer schema), and counts how often it picks the tool and
inputs the bank expects. Nothing about any person is sent: only the question
text and the tool descriptions.

    cd backend
    .venv/Scripts/python.exe -m scripts.eval_companion --sample 300
    .venv/Scripts/python.exe -m scripts.eval_companion --sample 300 --employee

Needs ANTHROPIC_API_KEY (it costs one small model call per question). Prints a
score per tool and the questions it got wrong.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
from collections import Counter, defaultdict
from datetime import date

from app.core.config import settings
from app.services.ai import client
from app.services.companion import service
from app.services.companion.config import CompanionConfig
from app.services.companion.tools import TOOLS
from app.services.grounding import injection
from tests.companion.fresh_questions import FRESH, FRESH_2
from tests.companion.question_bank import BANK, HELP, NEEDS_AI, REFUSED, TODAY, Case

#: What the AI may use: in AI mode the policy and report tools are there too.
AI_TOOLS = list(TOOLS.values())


def _tools(case: Case):
    return [
        tool
        for tool in AI_TOOLS
        if tool.group not in case.without and (case.hr or not tool.hr_only)
    ]


def _right(case: Case, plan: service.Plan) -> bool:
    if case.expect == REFUSED:
        # Nothing about anybody else is offered; their own record is not the answer either.
        return plan.tool == "none"
    if case.expect == NEEDS_AI:
        return plan.tool == "policy_question"
    if case.expect in (HELP, None):
        return plan.tool == "none"
    if plan.tool != case.expect:
        return False
    inputs = plan.inputs()
    return all(str(inputs.get(k, "")) == str(v) for k, v in case.inputs.items())


async def _one(case: Case, today: date) -> tuple[Case, service.Plan | None]:
    tools = _tools(case)
    system = service._system(CompanionConfig(), "Acme Technologies", tools)
    # The same shape as a real turn's message (service._context), dated to the bank's day.
    prompt = "\n\n".join(
        [
            f"Today is {today:%A} {today.isoformat()} (India).",
            "The person's new message:",
            injection.wrap(case.text, label="person"),
            injection.system_reinforcement(),
        ]
    )
    try:
        plan, _ = await client.structured(
            system=system,
            user=prompt,
            output=service.Plan,
            schema=service._schema(tools),
            max_tokens=1500,
            effort="low",
        )
    except (client.AiUnavailableError, client.AiDeclinedError):
        return case, None
    return case, plan


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", type=int, default=200)
    parser.add_argument("--employee", action="store_true", help="only employees' questions")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    if not settings.ANTHROPIC_API_KEY:
        print("Set ANTHROPIC_API_KEY to measure the AI planner.", file=sys.stderr)
        return 2

    pool = [c for c in [*BANK, *FRESH, *FRESH_2] if c.hr != args.employee]
    random.Random(args.seed).shuffle(pool)
    sample = pool[: args.sample]

    results = []
    for start in range(0, len(sample), 8):
        results += await asyncio.gather(*(_one(c, TODAY) for c in sample[start : start + 8]))

    score: dict[str, Counter] = defaultdict(Counter)
    wrong = []
    for case, plan in results:
        ok = plan is not None and _right(case, plan)
        score[str(case.expect)]["right" if ok else "wrong"] += 1
        if not ok:
            wrong.append((case, plan))
    total = sum(s["right"] for s in score.values())
    print(f"AI planner: {total}/{len(results)} right ({100 * total / max(len(results), 1):.1f}%)")
    for key, counts in sorted(score.items()):
        print(f"  {key:22} {counts['right']:4} right  {counts['wrong']:4} wrong")
    for case, plan in wrong[:60]:
        got = "no answer" if plan is None else f"{plan.tool} {plan.inputs()}"
        print(f"- {case.text!r}: wanted {case.expect} {case.inputs}, got {got}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
