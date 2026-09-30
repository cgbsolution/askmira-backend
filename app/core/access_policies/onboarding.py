"""Onboarding: the joiner's own plan, and tasks assigned to the caller."""

ENDPOINTS: dict[str, str] = {
    "GET /onboarding/me": "own: the caller's own onboarding plan, by their employee id.",
    "GET /onboarding/me/summary": (
        "own: the caller's own plan's progress and the tasks assigned to their login."
    ),
    "GET /onboarding/my-tasks": "own: tasks on other people's plans assigned to the caller.",
    "GET /onboarding/plans/{plan_id}": (
        "scoped:_visible_plan: HR, the joiner, their buddy, their reporting line, or "
        "somebody holding a task on the plan; anyone else is told it does not exist."
    ),
    "POST /onboarding/tasks/{task_id}/complete": (
        "scoped:_refuse_completion: the task's assignee, the joiner for their own tasks, "
        "or HR — never an acknowledgement on somebody else's behalf."
    ),
    "POST /onboarding/tasks/{task_id}/document": (
        "scoped:_refuse_completion: the joiner for their own document task, or HR."
    ),
    "GET /onboarding/tasks/{task_id}/document": (
        "scoped:_task_and_plan: somebody who can see the plan; the vault then allows only "
        "the joiner or HR to download."
    ),
}
