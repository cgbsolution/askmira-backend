"""The branded email: layout, escaping, buttons, the logo and the SMTP message it becomes."""

from __future__ import annotations

import email
from email import policy
from typing import ClassVar

import pytest

from app.services.notifications.adapters import InlineImage, OutboundMessage, SmtpEmailAdapter
from app.services.notifications.email_layout import (
    DEFAULT_ACCENT,
    LOGO_CID,
    EmailAction,
    EmailBrand,
    body_to_html,
    render_email,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def brand(**overrides) -> EmailBrand:
    base = {
        "company_name": "Acme Technologies",
        "accent_colour": "#0A7B83",
        "sign_off": "Regards,\nPeople team",
        "footer_text": "Acme Technologies, Bengaluru",
    }
    base.update(overrides)
    return EmailBrand(**base)


def test_wording_is_escaped_and_laid_out() -> None:
    out = body_to_html(
        "Hi <b>Meera</b>,\n\nYou have:\n- **2 days** left\n- a meeting\n\nSee https://x.test/a."
    )
    assert "&lt;b&gt;Meera&lt;/b&gt;" in out
    assert "<b>" not in out
    assert out.count("<p ") == 3
    assert "<ul" in out and out.count("<li") == 2
    assert "<strong>2 days</strong>" in out
    assert '<a href="https://x.test/a"' in out
    # The full stop after a link is not part of the link.
    assert 'href="https://x.test/a."' not in out


def test_the_email_carries_the_brand_and_the_platform_line() -> None:
    rendered = render_email(
        brand(logo=PNG), subject="Your payslip is ready", body="It is in AskMira."
    )
    assert "#0A7B83" in rendered.html
    assert f'src="cid:{LOGO_CID}"' in rendered.html
    assert rendered.images == (InlineImage(cid=LOGO_CID, data=PNG, mime_type="image/png"),)
    assert "Regards,<br>People team" in rendered.html
    assert "Acme Technologies, Bengaluru" in rendered.html
    assert "Powered by" in rendered.html and "AskMira" in rendered.html
    assert "Powered by AskMira" in rendered.text
    assert rendered.text.startswith("Your payslip is ready")


def test_without_a_logo_the_company_name_heads_the_email() -> None:
    rendered = render_email(brand(), subject="Hello", body="Body")
    assert rendered.images == ()
    assert "<img" not in rendered.html
    assert ">Acme Technologies</span>" in rendered.html


def test_a_preview_inlines_the_logo_instead_of_attaching_it() -> None:
    rendered = render_email(brand(logo=PNG), subject="S", body="B", embed_logo=False)
    assert "data:image/png;base64," in rendered.html
    assert rendered.images == ()


def test_a_bad_colour_falls_back_to_the_default() -> None:
    rendered = render_email(brand(accent_colour="red;background:url(x)"), subject="S", body="B")
    assert "url(x)" not in rendered.html
    assert DEFAULT_ACCENT in rendered.html


def test_one_portal_button_unless_the_wording_has_its_own_link() -> None:
    plain = render_email(brand(), subject="S", body="Something happened.", action_url="https://p")
    assert 'href="https://p"' in plain.html
    assert "Open AskMira: https://p" in plain.text

    reset = render_email(
        brand(), subject="S", body="Use https://p/reset?t=1 to reset.", action_url="https://p"
    )
    assert 'href="https://p"' not in reset.html


def test_approval_buttons_replace_the_portal_button() -> None:
    rendered = render_email(
        brand(),
        subject="Leave approval needed",
        body="Meera asked for 2 days.",
        action_url="https://p",
        actions=(
            EmailAction("Approve", "https://p/act/t?do=approve"),
            EmailAction("Reject", "https://p/act/t?do=reject", kind="secondary"),
        ),
    )
    assert "https://p/act/t?do=approve" in rendered.html
    assert "https://p/act/t?do=reject" in rendered.html
    assert ">Open AskMira<" not in rendered.html
    assert "Approve: https://p/act/t?do=approve" in rendered.text


class _FakeSmtp:
    sent: ClassVar[list] = []

    def __init__(self, *args, **kwargs) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None

    def starttls(self, **kwargs) -> None:
        pass

    def login(self, *args) -> None:
        pass

    def send_message(self, message):
        _FakeSmtp.sent.append(message)
        return {}


@pytest.mark.asyncio
async def test_smtp_sends_text_and_branded_html_with_the_logo_inline() -> None:
    _FakeSmtp.sent = []
    rendered = render_email(brand(logo=PNG), subject="Hello", body="Body line")
    adapter = SmtpEmailAdapter(
        host="smtp.test", port=587, from_address="hr@acme.test", smtp_factory=_FakeSmtp
    )
    await adapter.send(
        OutboundMessage(
            recipient_name="Meera",
            recipient_email="meera@acme.test",
            recipient_phone=None,
            subject="Hello",
            body=rendered.text,
            event_key="x.y",
            html=rendered.html,
            inline_images=rendered.images,
        )
    )
    raw = _FakeSmtp.sent[0].as_bytes()
    parsed = email.message_from_bytes(raw, policy=policy.default)
    assert parsed.get_content_type() == "multipart/alternative"
    plain = parsed.get_body(preferencelist=("plain",))
    html_part = parsed.get_body(preferencelist=("html",))
    assert "Powered by AskMira" in plain.get_content()
    assert "cid:company-logo" in html_part.get_content()
    images = [part for part in parsed.walk() if part.get_content_type() == "image/png"]
    assert len(images) == 1
    assert images[0]["Content-ID"] == f"<{LOGO_CID}>"
