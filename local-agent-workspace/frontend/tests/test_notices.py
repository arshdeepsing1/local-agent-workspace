"""Error notices (a short reason, the full text under Details) and the Stop divider (Conversation.js)."""
import json

import pytest
from playwright.sync_api import expect

BAD_REQUEST = ('Databricks returned HTTP 400: {"error_code": "BAD_REQUEST", "message": "Invalid request: the model '
               'rejected the input because the parameter max_tokens is out of range for this endpoint."}')
STOPPED = "Stopped. You can continue this conversation."


def summary(ui, event: dict):
    ui.start()
    return ui.js(f"import('/static/js/components/Conversation.js').then(m => m.errorSummary({json.dumps(event)}))")


def test_an_http_error_reads_as_a_sentence_and_the_provider_message(ui):
    # B-T11. Mutation: return text unchanged.
    result = summary(ui, {"type": "error", "text": BAD_REQUEST, "error_kind": "invalid_request", "http_status": 400})
    assert result == {"summary": "Databricks rejected the request (HTTP 400). Invalid request: the model rejected the "
                                 "input because the parameter max_tokens is out of range for this endpoint.",
                      "details": BAD_REQUEST}


@pytest.mark.parametrize("event, lead", [
    ({"text": "Databricks returned HTTP 401: <html>Unauthorized</html>", "error_kind": "authentication", "http_status": 401},
     "Databricks did not accept your credentials (HTTP 401). Check the token in your credential file."),
    ({"text": "All connection attempts failed", "error_kind": "network"},
     "Could not reach Databricks. Check your network or VPN connection."),
    ({"text": "Databricks returned HTTP 503: upstream unavailable", "error_kind": "server", "http_status": 503},
     "Databricks had a server problem (HTTP 503). Try again in a moment."),
    ({"text": "Databricks returned HTTP 403: <html>Forbidden</html>", "error_kind": "permission", "http_status": 403},
     "Databricks denied access to this model (HTTP 403)."),
    ({"text": "Databricks returned HTTP 429: Too Many Requests", "error_kind": "rate_limit", "http_status": 429},
     "Databricks rate limited this request."),
])
def test_authentication_network_and_server_errors_lead_with_a_plain_sentence(ui, event, lead):
    # B-T12. Mutation: drop the lead table.
    result = summary(ui, {"type": "error", **event})
    assert result == {"summary": lead, "details": event["text"]}


@pytest.mark.parametrize("kind, text", [
    ("output_limit", "The reply reached the output limit (Max output tokens) after automatic retries."),
    ("payload_too_large", "This request would be 5.2 MB, and Databricks accepts at most 4 MB per request, so it was not "
                          "sent. A body like {\"message\": \"x\"} is quoted here."),
    ("output_tokens_over_limit", "Max output tokens (64000) is above the endpoint's limit {\"message\": \"limit\"}."),
    ("content_filter", "The provider's content filter stopped the reply."),
])
def test_other_kinds_pass_through(ui, kind, text):
    # B-T13. Mutation: apply the JSON extraction to every kind.
    assert summary(ui, {"type": "error", "text": text, "error_kind": kind}) == {"summary": text, "details": None}


def test_a_compaction_failure_keeps_the_text_after_the_body(ui):
    # B-T14. Mutation: drop the text after '}'.
    text = 'Context compaction failed (HTTP 400): {"message":"x"} History is preserved; retry or adjust the context budget in Settings.'
    result = summary(ui, {"type": "error", "text": text})
    assert result == {"summary": "Context compaction failed (HTTP 400). x. History is preserved; retry or adjust the "
                                 "context budget in Settings.", "details": text}


ITPM_ADVICE = ("Each agent step resends the whole conversation: at about 400,000 input tokens per request, the per-minute "
               "input limit of 1,000,000 allows about 2 requests per minute. For about five steps a minute, keep "
               "Settings → Context budget near 200,000 tokens.")


@pytest.mark.parametrize("body, provider", [
    ('{"error": {"message": "Rate limit exceeded: ITPM limit of 1,000,000 tokens reached", "limit_type": '
     '"input_tokens_per_minute"}}', " Rate limit exceeded: ITPM limit of 1,000,000 tokens reached."),
    ("Too Many Requests", ""),
])
def test_a_final_rate_limit_error_keeps_its_advice_in_the_summary(ui, body, provider):
    # agents.py appends the ITPM advice to a final 429 (test_rate_limit_details.py pins the text): it is what the user
    # needs to stop repeated failures, so it is not hidden under Details. Mutation: drop `after` from the lead summary.
    text = f"Databricks returned HTTP 429: {body} {ITPM_ADVICE}"
    result = summary(ui, {"type": "error", "text": text, "error_kind": "rate_limit", "http_status": 429})
    assert result == {"summary": f"Databricks rate limited this request.{provider} {ITPM_ADVICE}", "details": text}
    assert result["summary"].endswith("keep Settings → Context budget near 200,000 tokens.")


def test_a_long_unreadable_body_is_clipped_to_300_characters(ui, page):
    # B-T15. Mutation: change clip's default to 300 (the declined note below is clipped at 240).
    text = "Databricks returned HTTP 418: " + "teapot " * 168 + "[Error response truncated.]"
    text = text[:1200]
    result = summary(ui, {"type": "error", "text": text, "error_kind": "unknown", "http_status": 418})
    flat = " ".join(text.split())
    assert result == {"summary": flat[:300] + "…", "details": text}
    note = "n" * 300
    declined = json.dumps({"id": "d", "type": "tool", "name": "run_command", "state": "rejected", "approval": "declined",
                           "input": {"command": "rm x"}, "output": "", "decline_note": note})
    page.evaluate("() => preload('components/Conversation.js', 'default')")
    page.evaluate("() => { mount('components/Conversation.js', () => ({ session: { id: 's', title: 't', status: 'idle', "
                  f"updated: 1, permission_mode: 'manual', allowed_directories: [], events: [{declined}] }} }}), 'default') }}")
    expect(page.locator(".tool-outcome")).to_have_text(f"Action declined: {'n' * 240}…")


def start_app(ui, events, **session):
    fields = json.dumps(session)
    ui.app(session="a", scenario="workspace",
           setup=f"fake.sessions.set('a', {{ ...makeSession('a', 'Errors'), events: {json.dumps(events)}, ...{fields} }})")
    ui.show_session("a")


ERROR_TURN = [{"id": "u", "type": "user", "text": "Write the exporter"},
              {"id": "e", "type": "error", "text": BAD_REQUEST, "error_kind": "invalid_request", "http_status": 400}]


def test_an_error_turn_shows_the_summary_and_details_without_retry(ui, page):
    # B-T16. Mutation: render text unchanged.
    start_app(ui, ERROR_TURN, terminal_reason="inference_error")
    notice = page.locator(".conversation-notice.error")
    expect(notice).to_have_attribute("role", "note")
    expect(notice.locator(".notice-summary")).to_have_text(
        "Databricks rejected the request (HTTP 400). Invalid request: the model rejected the input because the "
        "parameter max_tokens is out of range for this endpoint.")
    details = notice.locator(".notice-details pre")
    expect(details).not_to_be_visible()
    notice.get_by_text("Details", exact=True).click()
    expect(details).to_contain_text("BAD_REQUEST")
    expect(notice.get_by_role("button", name="Retry")).to_have_count(0)


def test_error_details_wrap_on_a_phone(ui, page):
    # B-T18.
    page.set_viewport_size({"width": 360, "height": 740})
    long = 'Databricks returned HTTP 400: {"message": "' + "unbroken" * 60 + '", "trace": "' + "z" * 400 + '"}'
    start_app(ui, [ERROR_TURN[0], {**ERROR_TURN[1], "text": long}], terminal_reason="inference_error")
    notice = page.locator(".conversation-notice.error")
    notice.get_by_text("Details", exact=True).click()
    pre = notice.locator(".notice-details pre")
    expect(pre).to_be_visible()
    assert pre.evaluate("e => e.scrollWidth <= e.clientWidth")
    assert ui.js("document.documentElement.scrollWidth <= document.documentElement.clientWidth")


def test_a_stop_is_a_divider_and_continue_is_the_only_message(ui, page):
    # B-T19. Mutation: render event.text for stop markers.
    events = [{"id": "u", "type": "user", "text": "Write the exporter"},
              {"id": "r", "type": "assistant", "text": "Partial answer"},
              {"id": "n", "type": "notice", "text": STOPPED, "stop_phase": "streaming"}]
    start_app(ui, events, terminal_reason="stopped")
    marker = page.locator(".conversation-notice.stop-marker")
    expect(marker).to_have_text("Stopped while replying")
    expect(marker).to_have_attribute("title", STOPPED)
    expect(page.get_by_text("The response was stopped.")).to_have_count(1)
    expect(page.get_by_text("You can continue this conversation")).to_have_count(0)
    expect(page.get_by_role("button", name="Continue", exact=True)).to_be_visible()


@pytest.mark.parametrize("event, text, classes", [
    ({"text": STOPPED}, "Stopped", "conversation-notice stop-marker"),
    ({"text": STOPPED, "stop_phase": "tools"}, "Stopped during an action", "conversation-notice stop-marker"),
    ({"text": STOPPED, "stop_phase": "waiting"}, "Stopped", "conversation-notice stop-marker"),
    ({"text": "Context compaction completed. History is preserved.", "compaction": {"summarized_messages": 4}},
     "Context compaction completed. History is preserved.", "conversation-notice compaction"),
])
def test_older_stop_notices_and_other_notice_classes(ui, page, event, text, classes):
    ui.mount("components/Conversation.js", "{ session: { id: 's', title: 't', status: 'idle', updated: 1, permission_mode: 'manual', "
             f"allowed_directories: [], events: [{json.dumps({'id': 'n', 'type': 'notice', **event})}] }} }}")
    notice = page.locator(".conversation-notice")
    expect(notice).to_have_text(text)
    expect(notice).to_have_class(classes)
