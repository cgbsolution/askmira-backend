"""Ask Mira: the AI companion (FRD 1.1, 7.1; FS-AIT-01).

Every workspace has its own Ask Mira. Its admin names it, tells it about the
company, chooses which tools it may use and how much it may be used; each
person then chats with it, and it answers *as that person*.

How a turn works — and why the model never sees a record:

1. The model reads the person's message, the conversation so far (only what
   people typed and what the model itself wrote) and the tools this person may
   use. It picks at most one tool and writes a line introducing the result.
2. AskMira runs the tool here, with the person's own access: the policy
   assistant (cited answers), the report compiler, the directory, who is on
   leave, attendance (HR only), the person's own record, a linked product.
3. The result goes to the screen, never back to the model. Colleagues' names,
   leave and attendance stay in AskMira; the model provider receives only
   AI-interaction and organisational data, as its DPDP registration allows.

    config.py   the workspace's own agent: name, greeting, notes, tools, limits
    tools.py    the toolbox, each tool gated by role, workspace settings and AI features
    service.py  conversations and the turn itself
"""
