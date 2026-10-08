from email.message import EmailMessage

import pytest

from recall.connectors.email.notmuch import _extract_text_from_html, _message_text


@pytest.mark.parametrize(
    "html,expected",
    [
        ("<style>.hidden { color: red; }</style><p>Visible fact.</p>", "Visible fact."),
        ("<script>technical_script()</script><p>Visible fact.</p>", "Visible fact."),
        ("Before<STYLE media='all'>hidden</STYLE>after", "Before after"),
        ('<script>"<style>not a tag</style>"</script><p>Safe.</p>', "Safe."),
        ("<style>hidden</style><script>hidden too</script>", ""),
        ("Visible<style>unclosed hidden", "Visible"),
        ("<style/><p>Visible.</p><script/>", "Visible."),
        (
            "<p>Caf&eacute; 🙂 &amp; tea</p><blockquote>Quoted fact.</blockquote>",
            "Café 🙂 & tea Quoted fact.",
        ),
        (
            "<p>Meet at 10.</p><!-- private comment --><p>Bring notes.</p>",
            "Meet at 10. Bring notes.",
        ),
    ],
)
def test_html_fallback_excludes_style_and_script_but_preserves_visible_text(html, expected):
    assert _extract_text_from_html(html) == expected


def test_html_only_mime_uses_visible_text_without_changing_message_bytes():
    message = EmailMessage()
    message.set_content("<style>hidden_css</style><p>Original fact.</p>", subtype="html")
    before = message.as_bytes()
    assert _message_text(message) == "Original fact."
    assert message.as_bytes() == before


def test_multipart_plain_text_preference_does_not_interpret_html_like_text():
    message = EmailMessage()
    plain = "Keep literal <style>words</style> and quoted history."
    message.set_content(plain)
    message.add_alternative("<script>hidden</script><p>Different HTML fact.</p>", subtype="html")
    before = message.as_bytes()
    assert _message_text(message) == plain
    assert message.as_bytes() == before
