"""The company, its people and their logins.

HR adds each employee, an admin gives them a login, and each person signs in
with the temporary password and sets their own — the first-day path every real
employee takes. Then employees fill in their own profile sections, HR records
the statutory and bank details, and a few employees ask for changes HR has to
approve.
"""

from __future__ import annotations

from seed_demo import generate_password

from .api import Api, Demo, SeedError
from .cast import (
    BASE_KEYS,
    DEPARTMENTS,
    aadhaar_for,
    account_for,
    bank_for,
    esi_for,
    pan_for,
    uan_for,
)

FLOW = "people"


async def _org(demo: Demo) -> None:
    ananya = demo["ananya"]
    entities = await ananya.get("/org/legal-entities")
    entity = next(e for e in entities if e["code"] == "DEMO")
    demo.ids["entity"] = entity["id"]
    await ananya.patch(
        f"/org/legal-entities/{entity['id']}",
        {
            "pan": "AAQCA7421K",
            "tan": "BLRA07421C",
            "pf_registration_no": "KN/BNG/0074210/000",
            "esi_registration_no": "53000742100011001",
            "registered_address": (
                "4th Floor, Prestige Tech Park, Outer Ring Road, Marathahalli, "
                "Bengaluru, Karnataka 560103"
            ),
            "pt_registrations": {"KA": "PTKA074210", "MH": "27740742100P"},
        },
    )

    locations = {loc["code"]: loc for loc in await ananya.get("/org/locations")}
    await ananya.patch(
        f"/org/locations/{locations['BLR']['id']}",
        {"name": "Bengaluru HQ", "city": "Bengaluru", "pincode": "560103"},
    )
    if "MUM" not in locations:
        locations["MUM"] = await ananya.post(
            "/org/locations",
            {
                "legal_entity_id": entity["id"],
                "name": "Mumbai office",
                "code": "MUM",
                "state_code": "MH",
                "city": "Mumbai",
                "pincode": "400051",
            },
        )
    demo.ids["location"] = {code: loc["id"] for code, loc in locations.items()}

    departments = {d["code"]: d for d in await ananya.get("/org/departments")}
    for code, name, _ in DEPARTMENTS:
        if code not in departments:
            departments[code] = await ananya.post(
                "/org/departments", {"name": name, "code": code}
            )
        elif departments[code]["name"] != name:
            await ananya.patch(f"/org/departments/{departments[code]['id']}", {"name": name})
    demo.ids["department"] = {code: d["id"] for code, d in departments.items()}
    demo.note(FLOW, "legal entity registrations, 2 locations, 7 departments")


def _record(demo: Demo, key: str) -> dict:
    person = demo.people[key]
    return {
        "first_name": person.first_name,
        "last_name": person.last_name,
        "work_email": person.email,
        "phone": person.phone,
        "date_of_birth": person.date_of_birth,
        "gender": person.gender,
        "legal_entity_id": demo.ids["entity"],
        "location_id": demo.ids["location"][person.location],
        "department_id": demo.ids["department"][person.department],
        "manager_id": demo.emp(person.manager) if person.manager else None,
        "designation": person.designation,
        "grade": person.grade,
        "date_of_joining": person.date_of_joining,
        "notice_period_days": 90 if (person.grade or "").startswith(("M", "E")) else 60,
    }


async def _employees(demo: Demo) -> None:
    priya = demo["priya"]
    # The five records seed_demo created carry only a name and a role; HR
    # completes them the way they would after a data migration. Nobody in HR
    # edits their own record, so Karthik completes Priya's.
    for key in BASE_KEYS:
        editor = demo["karthik"] if key == "priya" else priya
        await editor.patch(f"/employees/{demo.emp(key)}", _record(demo, key))

    existing = {
        e["employee_code"]: e
        for e in (await priya.get("/employees", page_size=200))["items"]
    }
    added = 0
    for key, person in demo.people.items():
        if key in BASE_KEYS:
            continue
        if person.code in existing:
            person.employee_id = existing[person.code]["id"]
            continue
        body = _record(demo, key) | {"employee_code": person.code}
        if key == "fatima":
            body["probation_end_date"] = "2027-03-20"
        created = await priya.post("/employees", body)
        person.employee_id = created["id"]
        added += 1

    for code, _, head in DEPARTMENTS:
        await priya.patch(
            f"/org/departments/{demo.ids['department'][code]}",
            {"head_employee_id": demo.emp(head)},
        )
    demo.note(FLOW, f"{added} employees added by HR; 5 migrated records completed")


async def _logins(demo: Demo) -> None:
    """An admin creates each login; the person signs in and sets a password."""
    ananya = demo["ananya"]
    users = {u["email"]: u for u in await ananya.get("/admin/users")}
    created = 0
    for key, person in demo.people.items():
        if person.code is None or key in BASE_KEYS:
            continue
        if person.email in users:
            person.user_id = users[person.email]["id"]
        else:
            result = await ananya.post(
                "/admin/users",
                {
                    "email": person.email,
                    "full_name": person.name,
                    "role": person.role,
                    "employee_id": demo.emp(key),
                },
            )
            person.user_id = result["user"]["id"]
            temporary = result["temporary_password"]

            # First sign-in, through the login endpoint, with the temporary password.
            response = await demo.http.post(
                "/api/v1/auth/login", json={"email": person.email, "password": temporary}
            )
            if response.status_code != 200:
                raise SeedError(f"{person.email} could not sign in: {response.text}")
            if not response.json()["user"].get("must_change_password"):
                raise SeedError(f"{person.email} was not asked to change the temporary password")
            person.password = generate_password()
            session = Api(demo.http, person.user_id, demo.tenant_id, person.role, person.name)
            await session.post(
                "/auth/change-password",
                {"current_password": temporary, "new_password": person.password},
            )
            created += 1
        person.api = Api(demo.http, person.user_id, demo.tenant_id, person.role, person.name)
    demo.note(FLOW, f"{created} logins created, signed in and passwords set")


async def _profiles(demo: Demo) -> None:
    """What employees fill in themselves, and what HR records for them."""
    karthik = demo["karthik"]
    for key, person in demo.people.items():
        if person.code is None:
            continue
        me = demo[key]
        married = person.date_of_birth is not None and person.date_of_birth < "1993-01-01"
        await me.put(
            "/employees/me/profile",
            {
                "change": {
                    "section": "personal",
                    "data": {
                        "father_name": f"{_father(person.last_name)} {person.last_name}",
                        "marital_status": "married" if married else "single",
                        "blood_group": ("B+", "O+", "A+", "AB+", "O-")[len(person.first_name) % 5],
                        "nationality": "Indian",
                    },
                }
            },
        )
        await me.put(
            "/employees/me/profile",
            {
                "change": {
                    "section": "emergency_contacts",
                    "data": [
                        {
                            "name": f"{_father(person.last_name)} {person.last_name}",
                            "relationship": "father",
                            "phone": "+91 99000 10" + person.code[-3:],
                            "is_primary": True,
                        }
                    ],
                }
            },
        )
        await me.put(
            "/employees/me/profile",
            {
                "change": {
                    "section": "education",
                    "data": [
                        {
                            "level": "graduate",
                            "qualification": "B.Tech" if person.department == "ENG" else "B.Com"
                            if person.department == "FIN" else "BBA",
                            "specialisation": "Computer Science"
                            if person.department == "ENG" else None,
                            "institution": _college(person.code),
                            "start_year": int(person.date_of_birth[:4]) + 18,
                            "end_year": int(person.date_of_birth[:4]) + 22,
                        }
                    ],
                }
            },
        )

        # Statutory identifiers, the salary account and the address are
        # sensitive: HR records them from the joining documents.
        bank_name, ifsc = bank_for(person)
        employee_id = demo.emp(key)
        hr = demo["priya"] if key == "karthik" else karthik
        await hr.put(
            f"/employees/{employee_id}/profile",
            {
                "change": {
                    "section": "statutory",
                    "data": {
                        "pan": pan_for(person),
                        "aadhaar": aadhaar_for(person),
                        "uan": uan_for(person),
                        # ESI covers monthly wages up to 21,000: an insurance
                        # number from the ESIC enrolment at joining.
                        **({"esi_number": esi_for(person)}
                           if person.ctc and person.ctc <= 252_000 else {}),
                    },
                }
            },
        )
        await hr.put(
            f"/employees/{employee_id}/profile",
            {
                "change": {
                    "section": "bank_account",
                    "data": {
                        "account_number": account_for(person),
                        "ifsc": ifsc,
                        "account_holder_name": person.name,
                        "bank_name": bank_name,
                    },
                }
            },
        )
        await hr.put(
            f"/employees/{employee_id}/profile",
            {
                "change": {
                    "section": "addresses",
                    "data": {
                        "current": _address(person),
                        "permanent_same_as_current": True,
                    },
                }
            },
        )
    demo.note(FLOW, "personal, emergency, education, statutory, bank and address on every profile")

    # Changes that need HR's approval.
    meera = demo["meera"]
    moved = await meera.post(
        "/employees/me/change-requests",
        {
            "change": {
                "section": "addresses",
                "data": {
                    "current": {
                        "line1": "Flat 1204, Brigade Lakefront",
                        "line2": "Whitefield",
                        "city": "Bengaluru",
                        "state_code": "KA",
                        "pincode": "560066",
                    },
                    "permanent_same_as_current": True,
                },
            },
            "reason": "Moved house in September",
        },
    )
    await karthik.post(f"/employees/change-requests/{moved['id']}/approve",
                       {"note": "Rent agreement seen"})

    await demo["rohan"].post(
        "/employees/me/change-requests",
        {
            "change": {
                "section": "bank_account",
                "data": {
                    "account_number": "917020041234567",
                    "ifsc": "UTIB0000009",
                    "account_holder_name": "Rohan Das",
                    "bank_name": "Axis Bank",
                },
            },
            "reason": "Salary account moved to Axis Bank",
        },
    )
    rejected = await demo["pooja"].post(
        "/employees/me/change-requests",
        {
            "change": {
                "section": "identity",
                "data": {"first_name": "Pooja", "last_name": "Desai-Shah",
                         "date_of_birth": "1996-03-08"},
            },
            "reason": "Surname after marriage",
        },
    )
    await karthik.post(
        f"/employees/change-requests/{rejected['id']}/reject",
        {"note": "Please attach the marriage certificate or gazette notification and resubmit."},
    )
    demo.note(FLOW, "change requests: address approved, bank pending, name change rejected")


def _father(last: str) -> str:
    return {"Rao": "Srinivas", "Ramesh": "Ramesh", "Iyer": "Subramaniam", "Shah": "Mahesh",
            "Nair": "Gopalan", "Mehta": "Rajesh", "Kulkarni": "Anil", "Das": "Tapan",
            "Sheikh": "Javed", "Verma": "Sunil", "Menon": "Krishnan", "Joshi": "Prakash",
            "Kapoor": "Ashok", "Desai": "Hemant", "Khan": "Salim", "Gupta": "Vinod",
            "Pillai": "Raghavan", "Reddy": "Venkat", "Kumar": "Ramesh",
            "Narayan": "Narayan", "Singh": "Harpreet", "Teja": "Prasad"}.get(last, "Raj")


def _college(code: str) -> str:
    colleges = (
        "RV College of Engineering", "PES University", "NIT Surathkal",
        "Christ University", "St. Joseph's College of Commerce", "BITS Pilani",
        "University of Mumbai", "Symbiosis International University",
    )
    return colleges[int(code[-2:]) % len(colleges)]


def _address(person) -> dict:
    if person.location == "MUM":
        return {"line1": f"{int(person.code[-2:]) + 10}, Sea Breeze CHS", "line2": "Bandra East",
                "city": "Mumbai", "state_code": "MH", "pincode": "400051"}
    areas = (("Koramangala", "560034"), ("HSR Layout", "560102"), ("Indiranagar", "560038"),
             ("Whitefield", "560066"), ("Jayanagar", "560041"), ("Marathahalli", "560037"))
    area, pin = areas[int(person.code[-2:]) % len(areas)]
    return {"line1": f"#{int(person.code[-2:]) * 7}, 2nd Cross", "line2": area,
            "city": "Bengaluru", "state_code": "KA", "pincode": pin}


async def run(demo: Demo) -> None:
    await _org(demo)
    await _employees(demo)
    await _logins(demo)
    await _profiles(demo)
