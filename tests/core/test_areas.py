"""Which area a request is in, and which right it needs."""

from __future__ import annotations

from app.core.areas import AREAS, AreaRights, area_for_path, required_right


def test_paths_map_to_their_area() -> None:
    assert area_for_path("/api/v1/payroll/runs") == "payroll"
    assert area_for_path("/api/v1/disbursement/batches/1/release") == "payroll"
    assert area_for_path("/api/v1/employees/abc") == "people"
    assert area_for_path("/api/v1/leave/requests") == "time"
    assert area_for_path("/api/v1/hiring/candidates") == "hiring"
    assert area_for_path("/api/v1/reports/analytics/attrition") == "analytics"


def test_account_settings_and_public_pages_are_in_no_area() -> None:
    for path in ("/api/v1/auth/me", "/api/v1/admin/users", "/api/v1/opsec/step-up",
                 "/api/v1/public/careers/acme", "/api/v1/dpdp/requests"):
        assert area_for_path(path) is None, path


def test_every_mapped_area_is_described() -> None:
    from app.core import areas

    assert set(areas._SEGMENT_AREA.values()) <= set(AREAS)


def test_the_right_follows_the_method_and_the_path() -> None:
    assert required_right("GET", "/api/v1/payroll/runs") == "can_view"
    assert required_right("POST", "/api/v1/payroll/runs") == "can_create"
    assert required_right("PATCH", "/api/v1/employees/1") == "can_edit"
    assert required_right("DELETE", "/api/v1/documents/1") == "can_delete"
    assert required_right("POST", "/api/v1/payroll/runs/1/approve") == "can_approve"
    assert required_right("POST", "/api/v1/leave/requests/1/decide") == "can_approve"
    assert required_right("POST", "/api/v1/disbursement/batches/1/release") == "can_approve"
    assert required_right("GET", "/api/v1/payroll/runs/1/register.csv") == "can_export"
    assert required_right("GET", "/api/v1/disbursement/batches/1/file") == "can_export"
    assert required_right("GET", "/api/v1/filings/pf-ecr/1/download") == "can_export"


def test_rights_default_to_everything() -> None:
    assert all(AreaRights().allows(r) for r in ("can_view", "can_approve", "can_export"))
    assert not AreaRights(can_approve=False).allows("can_approve")


def test_moving_people_to_another_company_is_an_edit() -> None:
    """It changes existing records, so a sub-admin needs Edit on People, not Create."""
    path = "/api/v1/employees/bulk/legal-entity"
    assert area_for_path(path) == "people"
    assert required_right("PATCH", path) == "can_edit"
