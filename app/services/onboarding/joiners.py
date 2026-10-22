"""Adding a new joiner: the record, the checklist and the login in one go.

HR used to do this in three places — People → Add employee, Settings → Users
for a login, then Onboarding → Start onboarding, picking the person they had
just created out of the whole company. Here it is one transaction, so a
checklist that cannot start (an inactive one, say) leaves no half-set-up
record behind, and a login is never created for a record that was refused.

The order inside matters: the checklist starts before the login is created,
so the welcome email can already tell the joiner what is waiting for them.
"""

from __future__ import annotations

from fastapi import HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import permissions
from app.models.user import User, UserRole
from app.schemas.onboarding import JoinerCreate, JoinerCreated, PlanCreate
from app.schemas.platform_admin import ManagedUserCreate
from app.services import employee as employee_service
from app.services.identity import users as user_admin
from app.services.onboarding import rules
from app.services.onboarding import service as onboarding

__all__ = ["add_joiner"]


async def add_joiner(
    session: AsyncSession,
    user: User,
    payload: JoinerCreate,
    *,
    request: Request | None = None,
) -> JoinerCreated:
    """Create the joiner's record, start their checklist and give them a login.

    Does not commit: the router does, once, for all three.
    """
    permissions.require_hr(user, detail="Only HR adds new joiners.")
    if payload.create_login and not permissions.is_admin(user):
        raise permissions.forbidden(
            "Only an admin gives people a login. Add the joiner without one; an admin "
            "can create it later from their profile."
        )
    joining = payload.employee.date_of_joining
    if joining is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A new joiner needs a date of joining.",
        )

    record = payload.employee.model_copy(
        update={"status": rules.joining_status(joining, rules.india_today())}
    )
    employee = await employee_service.create_employee(
        session, user, record, request=request, commit=False
    )

    plan = None
    if payload.template_id is not None:
        plan = await onboarding.start_plan(
            session,
            user,
            PlanCreate(
                employee_id=employee.id,
                template_id=payload.template_id,
                buddy_employee_id=payload.buddy_employee_id,
            ),
            request=request,
        )

    login = None
    if payload.create_login:
        login = await user_admin.create_user(
            session,
            user,
            ManagedUserCreate(
                email=employee.work_email,
                full_name=employee.full_name,
                role=UserRole.EMPLOYEE,
                employee_id=employee.id,
                send_welcome_email=payload.send_welcome_email,
            ),
            request=request,
            commit=False,
        )

    return JoinerCreated(
        employee_id=employee.id,
        employee_code=employee.employee_code,
        employee_name=employee.full_name,
        status=employee.status,
        plan_id=plan.id if plan else None,
        checklist_name=plan.template_name if plan else None,
        login=login,
    )
