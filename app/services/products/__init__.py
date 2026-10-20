"""Our separate products — the interview tool and the expense tool — linked to a workspace.

A product keeps its own copy of the company and its people. AskMira is the
source of truth for both: the platform console links a workspace to a product
(the product creates or adopts the organisation), AskMira pushes the people
who should have access (and takes it away from leavers), and a signed-in person
opens the product from the portal without a second password.

Every call AskMira makes to a product, and every launch, carries a short-lived
HS256 token signed with that product's own shared secret, so one product can
never act as AskMira towards the other.

    catalogue.py  which products exist, which module each replaces, who may use it
    settings.py   where each product lives and its secret, from the environment
    tokens.py     the signed assertions
    client.py     the HTTP calls to a product
    service.py    link, unlink, sync, launch
    data.py       read-only questions asked for one person (Ask Mira)
"""
