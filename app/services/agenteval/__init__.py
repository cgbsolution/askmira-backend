"""Agent evaluation: the control that catches the defect we already shipped.

The Fit Score returned near-identical values across candidates whose underlying
round scores differed by nineteen points, and it reached a benchmark before
anybody noticed. Nothing about that was subtle in hindsight — a scoring agent
that does not discriminate is visible in five candidates and one query. It got
through because no test asked whether the outputs differed.

So the assertion that matters most here is `DISCRIMINATION`, and every scoring
agent gets one. The rest of this package exists to make running it cheap enough
that it happens on every prompt change.

Everything except `service.py` is pure.
"""
