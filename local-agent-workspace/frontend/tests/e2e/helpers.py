"""Steps the end-to-end tests share (the fixtures are in conftest.py)."""


def send(page, text):
    """Type a message in the composer and send it."""
    box = page.get_by_role("textbox", name="Message")
    box.fill(text)
    box.press("Enter")
