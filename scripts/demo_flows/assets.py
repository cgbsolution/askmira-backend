"""Assets: the register Manoj keeps, who holds what, and requests in flight."""

from __future__ import annotations

from datetime import date, timedelta

from .api import Demo

FLOW = "assets"

CATEGORIES = (
    # code, name, depreciation months, needs acknowledgement
    ("LAP", "Laptop", 36, True),
    ("MON", "Monitor", 60, True),
    ("PHN", "Mobile phone", 24, True),
    ("HDS", "Headset", 24, False),
    ("ACC", "Access card", None, False),
)


async def setup(demo: Demo) -> None:
    """Categories, the register, and the kit every employee was issued."""
    priya = demo["priya"]
    manoj_user = str(demo.people["manoj"].user_id)
    listed = await priya.get("/assets/categories", include_inactive=True)
    categories = {c["code"]: c for c in listed}
    for code, name, months, ack in CATEGORIES:
        if code not in categories:
            categories[code] = await priya.post(
                "/assets/categories",
                {"code": code, "name": name, "custodian_user_id": manoj_user,
                 "depreciation_months": months, "requires_acknowledgement": ack},
            )
    demo.ids["asset_category"] = {code: c["id"] for code, c in categories.items()}

    existing = {a["asset_tag"]: a for a in (await priya.get("/assets", page_size=200))["items"]}
    tags: dict[str, str] = {}

    async def asset(tag: str, category: str, name: str, **extra) -> str:
        if tag not in existing:
            existing[tag] = await priya.post(
                "/assets",
                {"category_id": demo.ids["asset_category"][category], "asset_tag": tag,
                 "name": name, **extra},
            )
        tags[tag] = existing[tag]["id"]
        return existing[tag]["id"]

    holders = [k for k, p in demo.people.items() if p.code]
    for index, key in enumerate(holders, start=1):
        person = demo.people[key]
        engineer = person.department in ("ENG", "PRD")
        make, model, cost = (
            ("Apple", "MacBook Pro 14 M3", "189900") if engineer
            else ("Lenovo", "ThinkPad E14 Gen 5", "72500")
        )
        tag = f"AM-LAP-{index:03d}"
        await asset(
            tag, "LAP", f"{make} {model}", make=make, model=model,
            serial_number=f"SN{int(person.code[-3:]) * 7919:08d}",
            purchase_date="2024-03-15" if index % 2 else "2025-06-02",
            purchase_cost=cost, vendor="Rashi Peripherals", invoice_number=f"RP/24/{4410 + index}",
            warranty_until="2027-03-14" if index % 2 else "2028-06-01",
            location_id=demo.ids["location"][person.location], condition="good",
        )
        await _hold(demo, tags[tag], key)
        if person.department == "ENG":
            mtag = f"AM-MON-{index:03d}"
            await asset(mtag, "MON", "Dell UltraSharp U2723QE 27\"", make="Dell",
                        model="U2723QE", purchase_cost="48500", purchase_date="2025-01-20",
                        location_id=demo.ids["location"]["BLR"], condition="good")
            await _hold(demo, tags[mtag], key)
        if person.department == "SAL":
            ptag = f"AM-PHN-{index:03d}"
            await asset(ptag, "PHN", "Samsung Galaxy A55", make="Samsung", model="Galaxy A55",
                        purchase_cost="39999", purchase_date="2025-08-11",
                        location_id=demo.ids["location"]["MUM"], condition="good")
            await _hold(demo, tags[ptag], key)

    # Spares in the store room, one in repair, one written off.
    for n in range(1, 4):
        await asset(f"AM-LAP-S{n:02d}", "LAP", "Lenovo ThinkPad E14 Gen 5", make="Lenovo",
                    model="ThinkPad E14 Gen 5", purchase_cost="72500",
                    purchase_date="2026-06-10", location_id=demo.ids["location"]["BLR"])
    for n in range(1, 3):
        await asset(f"AM-MON-S{n:02d}", "MON", "Dell P2422H 24\"", make="Dell", model="P2422H",
                    purchase_cost="16900", purchase_date="2026-06-10",
                    location_id=demo.ids["location"]["BLR"])
    await asset("AM-PHN-S01", "PHN", "Samsung Galaxy A35", make="Samsung", model="Galaxy A35",
                purchase_cost="30999", purchase_date="2026-05-02",
                location_id=demo.ids["location"]["MUM"])
    for n in range(1, 4):
        await asset(f"AM-HDS-{n:03d}", "HDS", "Jabra Evolve2 40", make="Jabra",
                    model="Evolve2 40", purchase_cost="8900", purchase_date="2025-11-05",
                    location_id=demo.ids["location"]["BLR"])
    repair = await asset("AM-LAP-R01", "LAP", "Dell Latitude 5440", make="Dell",
                         model="Latitude 5440", purchase_cost="81000",
                         purchase_date="2023-09-01", condition="fair",
                         location_id=demo.ids["location"]["BLR"])
    old = await asset("AM-LAP-X01", "LAP", "HP ProBook 440 G8", make="HP", model="ProBook 440 G8",
                      purchase_cost="64000", purchase_date="2021-07-12", condition="damaged",
                      location_id=demo.ids["location"]["BLR"])
    current = {a["id"]: a for a in (await priya.get("/assets", page_size=200))["items"]}
    if current[repair]["status"] == "in_stock":
        await priya.post(f"/assets/{repair}/status",
                         {"status": "in_repair",
                          "reason": "Battery swelling; sent to Dell service"})
    if current[old]["status"] == "in_stock":
        await priya.post(f"/assets/{old}/status",
                         {"status": "retired", "reason": "Past its five-year life; data wiped"})
    demo.ids["asset_tag"] = tags

    # Everyone who received kit acknowledges it, except two who have not yet.
    acknowledged = 0
    for key, person in demo.people.items():
        if not person.code or key in ("fatima", "imran"):
            continue
        mine = await demo[key].get("/assets/me")
        for item in _assignments(mine):
            if item.get("acknowledged_at") is None and item.get("requires_acknowledgement", True):
                await demo[key].post(f"/assets/assignments/{item['id']}/acknowledge")
                acknowledged += 1
    demo.note(FLOW, f"{len(existing)} assets in 5 categories; {acknowledged} receipts acknowledged")


def _assignments(mine) -> list[dict]:
    if isinstance(mine, dict):
        for key in ("current", "assignments", "items", "holding"):
            if isinstance(mine.get(key), list):
                return mine[key]
        return []
    return list(mine)


async def _hold(demo: Demo, asset_id: str, key: str) -> None:
    detail = await demo["priya"].get(f"/assets/{asset_id}")
    if detail["status"] == "assigned":
        return
    # Nobody in HR hands an asset to themselves.
    issuer = demo["karthik"] if key == "priya" else demo["priya"]
    await issuer.post(
        f"/assets/{asset_id}/assign",
        {"employee_id": demo.emp(key), "condition_out": "good",
         "notes": "Issued at joining" if key != "fatima" else "Issued on day one"},
    )


async def current(demo: Demo) -> None:
    """Requests in each state: fulfilled, approved, pending and rejected."""
    cat = demo.ids["asset_category"]
    # Relative to today: a needed-by date in the past is refused.
    soon = (date.today() + timedelta(days=7)).isoformat()
    later = (date.today() + timedelta(days=10)).isoformat()
    request = await demo["meera"].post(
        "/assets/requests",
        {"category_id": cat["MON"], "reason": "A second monitor for code reviews",
         "needed_by": later},
    )
    await demo["vikram"].post(f"/assets/requests/{request['id']}/approve",
                              {"note": "Approved - use one of the spares."})
    spare = await demo["karthik"].get("/assets", category_id=cat["MON"], status="in_stock")
    await demo["karthik"].post(
        f"/assets/requests/{request['id']}/fulfil",
        {"asset_id": spare["items"][0]["id"], "condition_out": "new",
         "notes": "Collected from the store room"},
    )

    request = await demo["nikhil"].post(
        "/assets/requests",
        {"category_id": cat["HDS"], "reason": "Headset for user research calls"},
    )
    await demo["kavya"].post(f"/assets/requests/{request['id']}/approve",
                             {"note": "Yes, please."})

    await demo["pooja"].post(
        "/assets/requests",
        {"category_id": cat["LAP"], "reason": "Laptop keyboard keys are failing",
         "needed_by": soon},
    )
    request = await demo["imran"].post(
        "/assets/requests",
        {"category_id": cat["PHN"], "reason": "A second phone for client calls"},
    )
    await demo["rahul"].post(
        f"/assets/requests/{request['id']}/reject",
        {"note": "Use the dialler on the laptop; phones are for account managers."},
    )
    demo.note(FLOW, "asset requests: 1 fulfilled, 1 approved, 1 pending, 1 rejected")
