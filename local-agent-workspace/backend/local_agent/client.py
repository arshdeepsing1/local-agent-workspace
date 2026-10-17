"""A Python client for a running Local Agent Workspace app: start chats, send messages, answer cards and read the
result of each turn from a script. Standard library only, so it can be copied next to a script or imported without
the app's dependencies installed.

    from local_agent.client import Allow, Deny, LocalAgentClient

    client = LocalAgentClient()  # http://127.0.0.1:8765, the app's own address
    for message in client.query("Summarize the README", permission_mode="plan"):
        if message["type"] == "result":
            print(message["result"])

The client talks to the app's HTTP API on this computer only (127.0.0.1, localhost or ::1): it reads the local
token from /api/bootstrap as the page does and fetches it again when the app restarted (HTTP 403
reconnect_required). A chat's live messages come from GET /api/sessions/{id}/stream.ndjson, one JSON object per
line, the same messages the page's WebSocket gets.

query() is one prompt in a new chat (or an idle existing one), as a generator of the chat's messages ending with
one {"type": "result", ...} record built from the turn's result (turn_result). Cards are answered as they come:
an approval through can_use_tool(name, input, context) returning Allow() or Deny(message); an ask_user question
through on_question(questions, context) returning the answers (or None to skip them); a plan (Plan mode's
submit_plan) through can_use_tool("submit_plan", ...), where Allow(mode) approves it into Manual (or the mode
given) and Deny keeps planning. Without a callback every approval is declined and questions are skipped at once:
nothing waits for the approval timeout. An answer the app refuses (an option that is not listed, an Other answer
over 1,000 characters) is replaced at once by that default and reported as an {"type": "answer_refused", ...}
message. A subagent's cards are answered the same way (context["subagent"] is True). context also carries the
card's warnings when it has them, as the page shows them: secret_files (the credential-looking files a command
reads), protected (why the file is protected), hook_ask ({hook_id, reason}: a before-tool hook asked),
uncertain (the command could not be checked exactly against the deny and ask rules) and sibling_change
({subagent_id, agent_type, path}: another subagent of the chat changed the file).

When the turn ends while background subagents of the chat (delegate_task background) still work, query() yields
{"type": "background_wait", "running": n} and keeps following them, answering their cards the same way, until
they end; the result's background_results lists each one's end (its delegate_task result, or its last status).
A turn the app starts in the chat meanwhile (Settings → Subagents → Continue the chat when a background subagent
finishes) is followed too, and the result is the last turn's. Every model request is a paid call to your
Databricks workspace, made with the app's credentials.
"""
import http.client
import json
import queue
import socket
import threading
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

DEFAULT_URL = "http://127.0.0.1:8765"
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
MAX_NOTE = 2000  # approvals.MAX_DECLINE_NOTE
NO_APPROVER = "Declined: nobody can approve actions in this scripted run. Continue without this action."
NO_REVIEWER = ("Nobody can review plans in this scripted run. Do not make changes: end your turn with the plan as "
               "your reply.")
# The app's answers (HTTP 400) to a card that is no longer waiting: a mode change or Stop answered it first
# (AgentManager.decide, interactions.QUESTION_GONE and PLAN_GONE).
CARD_GONE = ("This approval is no longer pending.", "This question is no longer waiting for an answer.",
             "This plan is no longer waiting for review.")
SHUT_DOWN = "The app shut down before the turn ended (its shutdown stops running turns)."
LIMIT_REASONS = ("step_limit", "spending_limit", "budget_limit", "approval_expired")
STOP_REASONS = ("stopped", "interrupted")
RECONNECTS = 3  # New streams query() opens after the app dropped one whose reader fell behind.
# A card's warnings that can_use_tool's context carries (agents.card_fields, the sibling and hook checks).
CARD_WARNINGS = ("secret_files", "protected", "hook_ask", "uncertain", "sibling_change")


class LocalAgentError(RuntimeError):
    """An answer of the app that is not a success: status is its HTTP status (None: no answer at all)."""

    def __init__(self, message, status=None, detail=None):
        super().__init__(message)
        self.status, self.detail = status, detail


@dataclass
class Allow:
    """can_use_tool's answer that lets the action run. mode: for a plan, the mode it is approved into."""
    mode: str = "manual"


@dataclass
class Deny:
    """can_use_tool's answer that declines the action; message goes back to the agent (at most 2,000 characters)."""
    message: str = ""


def exit_code(terminal_reason):
    """The exit code scripts/agent_run.py gives a turn's end: 0 completed, 1 error, 2 a limit was reached, 3
    stopped or interrupted."""
    if terminal_reason in LIMIT_REASONS:
        return 2
    if terminal_reason in STOP_REASONS:
        return 3
    return 0 if terminal_reason in ("completed", "tool_error") else 1


class LocalAgentClient:
    def __init__(self, url=DEFAULT_URL, timeout=30.0):
        parts = urlsplit(url)
        if parts.scheme != "http" or parts.hostname not in LOCAL_HOSTS:
            raise ValueError("The app listens on this computer only: use http://127.0.0.1:<port> (or localhost).")
        self.host, self.port = parts.hostname, parts.port or 80
        self.timeout = timeout
        self.token = None
        self.settings = None

    # The HTTP API

    def _connection(self, timeout=None):
        return http.client.HTTPConnection(self.host, self.port, timeout=self.timeout if timeout is None else timeout)

    def _headers(self, body=False):
        headers = {"Accept": "application/json"}
        if self.token:
            headers["X-Local-Token"] = self.token
        if body:
            headers["Content-Type"] = "application/json"
        return headers

    def bootstrap(self):
        """Read the local token (and the app's settings) as the page does when it loads."""
        status, data = self._send("GET", "/api/bootstrap", token=False)
        if status != 200 or not isinstance(data, dict) or not isinstance(data.get("token"), str):
            raise LocalAgentError("The app did not hand out its local token.", status, data)
        self.token, self.settings = data["token"], data.get("settings")
        return data

    def _send(self, method, path, body=None, token=True):
        connection = self._connection()
        try:
            payload = None if body is None else json.dumps(body).encode("utf-8")
            connection.request(method, path, body=payload,
                               headers=self._headers(payload is not None) if token else {"Accept": "application/json"})
            response = connection.getresponse()
            raw = response.read()
        except OSError as exc:
            raise LocalAgentError(f"No app answered at http://{self.host}:{self.port} ({exc}). Start it first "
                                  "(python3 run.py).") from exc
        finally:
            connection.close()
        try:
            data = json.loads(raw) if raw else None
        except ValueError:
            data = raw.decode("utf-8", "replace")
        return response.status, data

    def request(self, method, path, body=None):
        """One API call: its decoded JSON answer, or LocalAgentError. A token from before an app restart is
        fetched again once."""
        if self.token is None:
            self.bootstrap()
        status, data = self._send(method, path, body)
        if status == 403 and isinstance(data, dict) and data.get("code") == "reconnect_required":
            self.bootstrap()
            status, data = self._send(method, path, body)
        if not 200 <= status < 300:
            detail = data.get("detail") if isinstance(data, dict) else data
            if isinstance(detail, list):  # A validation error: its messages.
                detail = "; ".join(str(item.get("msg", item)) if isinstance(item, dict) else str(item) for item in detail)
            raise LocalAgentError(f"HTTP {status}: {detail}", status, data)
        return data

    @staticmethod
    def _path(session_id, suffix=""):
        return f"/api/sessions/{quote(str(session_id), safe='')}{suffix}"

    # Chats

    def create_session(self, workspace=None, permission_mode="plan", model=None, effort=None, max_steps=None,
                       spending_limit_dbu=None, turn_token_limit=None, turn_time_limit_minutes=None):
        """A new chat: its project folder (default: the Settings workspace), permission mode (default Plan, which
        changes nothing), endpoint, reasoning effort, Agent steps per message and limits (the Settings defaults
        when left out). Returns the chat as the app shows it."""
        body = {"permission_mode": permission_mode}
        for key, value in (("workspace", workspace), ("model", model), ("reasoning_effort", effort),
                           ("max_agent_steps", max_steps)):
            if value is not None:
                body[key] = str(value) if key == "workspace" else value
        session = self.request("POST", "/api/sessions", body)
        if spending_limit_dbu is not None:
            session = self.set_spending_limit(session["id"], spending_limit_dbu)
        if turn_token_limit is not None or turn_time_limit_minutes is not None:
            session = self.set_turn_limits(session["id"], tokens=turn_token_limit, minutes=turn_time_limit_minutes)
        return session

    def session(self, session_id):
        return self.request("GET", self._path(session_id))

    def info(self, session_id):
        """The chat's header (no events) and status."""
        return self.request("GET", self._path(session_id, "/info"))

    def send(self, session_id, text, verbatim=False):
        """Send a message: it starts a turn, or waits in the queue while one runs."""
        return self.request("POST", self._path(session_id, "/messages"), {"text": text, "verbatim": bool(verbatim)})

    def interrupt(self, session_id):
        """Stop the running turn (and its subagents), as Stop does."""
        return self.request("POST", self._path(session_id, "/stop"))

    def set_permission_mode(self, session_id, mode, confirm=False):
        """Change the mode, also while a turn runs; Bypass permissions needs confirm=True."""
        return self.request("PUT", self._path(session_id, "/permissions"), {"permission_mode": mode, "confirm": bool(confirm)})

    def set_model(self, session_id, model):
        return self.request("PUT", self._path(session_id, "/model"), {"model": model})

    def set_effort(self, session_id, effort):
        return self.request("PUT", self._path(session_id, "/reasoning-effort"), {"reasoning_effort": effort})

    def set_spending_limit(self, session_id, dbu):
        """The chat's spending limit in estimated DBU (0 = none, None = the Settings default)."""
        return self.request("PUT", self._path(session_id, "/spending-limit"),
                            {"spending_limit_dbu": None if dbu is None else float(dbu)})

    def set_turn_limits(self, session_id, tokens=None, minutes=None):
        """The chat's token and time limits per turn (0 = none); a limit left as None is not changed."""
        body = {key: value for key, value in (("turn_token_limit", tokens), ("turn_time_limit_minutes", minutes))
                if value is not None}
        return self.request("PUT", self._path(session_id, "/turn-limits"), body)

    def fork(self, session_id, before_event_id=None, title=None):
        """A copy of the chat, whole or before one of its messages (an event id)."""
        body = {key: value for key, value in (("before_event_id", before_event_id), ("title", title)) if value is not None}
        return self.request("POST", self._path(session_id, "/fork"), body)

    def rewind(self, session_id, before_event_id, conversation=True, code=False, resend_text=None):
        """Rewind the chat (and/or its files) to before one of its messages, optionally sending a new text."""
        body = {"before_event_id": before_event_id, "conversation": conversation, "code": code}
        if resend_text is not None:
            body["resend_text"] = resend_text
        return self.request("POST", self._path(session_id, "/rewind"), body)

    def answer_approval(self, session_id, event_id, allowed, note=None):
        body = {"allowed": bool(allowed)}
        if note and not allowed:
            body["note"] = str(note)[:MAX_NOTE]
        return self.request("POST", self._path(session_id, f"/approvals/{quote(event_id, safe='')}"), body)

    def answer_questions(self, session_id, event_id, answers=None):
        """answers: one {selected: [labels], other: text or None} per question; None skips them."""
        body = {"skip": True} if answers is None else {"answers": answers}
        return self.request("POST", self._path(session_id, f"/questions/{quote(event_id, safe='')}"), body)

    def answer_plan(self, session_id, event_id, choice, feedback=None):
        """choice: acceptEdits or manual (approve into that mode) or keep_planning."""
        body = {"choice": choice, **({"feedback": str(feedback)[:MAX_NOTE]} if feedback else {})}
        return self.request("POST", self._path(session_id, f"/plans/{quote(event_id, safe='')}"), body)

    # Live messages

    def events(self, session_id, stop=None):
        """The chat's live messages, one dict each, starting with its snapshot (keep_alive lines are skipped). The
        generator ends when the app closes the stream or stop (a threading.Event) is set."""
        for message in self._lines(session_id, stop):
            if message.get("type") != "keep_alive":
                yield message

    def _open(self, session_id):
        if self.token is None:
            self.bootstrap()
        for attempt in (1, 2):
            connection = self._connection(timeout=max(60.0, self.timeout))
            try:
                connection.request("GET", self._path(session_id, "/stream.ndjson"), headers=self._headers())
                response = connection.getresponse()
            except OSError as exc:
                connection.close()
                raise LocalAgentError(f"No app answered at http://{self.host}:{self.port} ({exc}).") from exc
            if response.status == 200:
                return connection, response
            raw = response.read()
            connection.close()
            try:
                data = json.loads(raw)
            except ValueError:
                data = None
            if attempt == 1 and response.status == 403 and isinstance(data, dict) and data.get("code") == "reconnect_required":
                self.bootstrap()
                continue
            raise LocalAgentError(f"HTTP {response.status}: {data.get('detail') if isinstance(data, dict) else raw[:200]}",
                                  response.status, data)

    def _lines(self, session_id, stop=None, opened=None):
        connection, response = self._open(session_id)
        if opened is not None:
            opened.append(connection)  # Closed by its owner to end the read at once.
        try:
            while stop is None or not stop.is_set():
                try:
                    raw = response.readline()
                except (OSError, ValueError, AttributeError):  # Closed from another thread.
                    return
                if not raw:
                    return
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(message, dict):
                    yield message
        finally:
            connection.close()

    def wait_result(self, session_id, after=None):
        """The result record of the chat's running turn once it ends, or of its last turn when it is idle; None
        when it is idle and had no turn, or none that started after `after` (the started time of an earlier turn's
        result: a message that never started a turn)."""
        replies = {}
        for message in self.events(session_id):
            for event in _events_of(message):
                if event.get("type") == "assistant":
                    replies[event.get("id")] = dict(event)
            if message.get("type") == "snapshot":
                session = message.get("session") or {}
                if session.get("status", "idle") == "idle":
                    last_turn = session.get("last_turn_result")
                    if not isinstance(last_turn, dict) or (after is not None and last_turn.get("started") == after):
                        return None
                    return result_record(session_id, last_turn, replies)
            if message.get("type") == "status" and message.get("status") == "idle" and isinstance(message.get("turn_result"), dict):
                return result_record(session_id, message["turn_result"], replies)
            if message.get("type") == "stream_closed" and message.get("reason") == "shutdown":
                raise LocalAgentError(SHUT_DOWN)
        raise LocalAgentError("The app closed the chat's stream before the turn ended.")

    # One prompt

    def query(self, prompt, *, session_id=None, workspace=None, permission_mode="plan", model=None, effort=None,
              max_steps=None, spending_limit_dbu=None, turn_token_limit=None, turn_time_limit_minutes=None,
              can_use_tool=None, on_question=None, verbatim=False):
        """Send prompt to a new chat (or the idle chat session_id, which keeps its own mode, endpoint, effort,
        folder and limits: the options of a new chat are not applied to it) and yield its messages (each with
        session_id; a subagent's cards and messages also come, with subagent True) until the turn ends and the
        chat's background subagents ended, then one result record: {type: 'result', session_id, terminal_reason,
        is_error, exit_code, duration_s, steps, tool_calls, usage, tokens, estimated_dbu, unpriced_calls,
        subagents, changed_files, result (the final reply), background_results}."""
        if session_id is None:
            session_id = self.create_session(workspace, permission_mode, model, effort, max_steps, spending_limit_dbu,
                                             turn_token_limit, turn_time_limit_minutes)["id"]
        messages, stop, readers, opened = queue.Queue(), threading.Event(), {}, []

        def read(chat_id, subagent):
            try:
                for attempt in range(RECONNECTS + 1):
                    closed = None
                    for message in self._lines(chat_id, stop, opened):
                        if message.get("type") == "stream_closed":
                            closed = message.get("reason")
                        else:
                            messages.put((chat_id, subagent, message))
                    # A reader that fell behind was dropped: a new stream starts with a fresh snapshot.
                    if closed != "slow_reader" or stop.is_set():
                        break
                if closed == "shutdown":
                    messages.put((chat_id, subagent, {"type": "stream_error", "error": SHUT_DOWN}))
            except LocalAgentError as exc:
                messages.put((chat_id, subagent, {"type": "stream_error", "error": str(exc)}))
            finally:
                messages.put((chat_id, subagent, {"type": "stream_ended"}))

        def follow(chat_id, subagent=False):
            if chat_id not in readers:
                readers[chat_id] = threading.Thread(target=read, args=(chat_id, subagent), daemon=True)
                readers[chat_id].start()

        follow(session_id)
        answered, replies, sent, before = set(), {}, False, None
        # The chat's background delegations seen running ({card id: its card}), followed until each ended; ended:
        # the result of the turn that ended meanwhile; check: whether to see if all of them ended.
        background, ended, check = {}, None, False
        try:
            while True:
                chat_id, subagent, message = messages.get()
                kind = message.get("type")
                if kind == "keep_alive":
                    continue
                if kind in ("stream_error", "stream_ended") and chat_id == session_id:
                    if ended is not None:  # The turn ended: what its background subagents did so far.
                        yield result_record(session_id, ended, replies, list(background.values()))
                        return
                    raise LocalAgentError(message.get("error") or "The app closed the chat's stream before the turn ended.")
                if kind == "stream_ended":
                    continue
                turn = None  # The result of a turn that just ended (also from a new snapshot after a reconnect).
                if kind == "snapshot" and chat_id == session_id:
                    chat = message["session"]
                    last_turn = chat.get("last_turn_result") if isinstance(chat.get("last_turn_result"), dict) else None
                    if not sent:
                        if chat.get("status", "idle") != "idle":
                            raise LocalAgentError("This chat is busy: wait for its turn to end, or use another chat.")
                        before = last_turn and last_turn.get("started")
                        self.send(session_id, prompt, verbatim)
                        sent = True
                    elif chat.get("status") == "idle" and last_turn and last_turn.get("started") != before:
                        turn = last_turn
                yield {**message, "session_id": chat_id, **({"subagent": True} if subagent else {})}
                for event in _events_of(message):
                    if chat_id == session_id and event.get("type") == "assistant":
                        replies[event.get("id")] = dict(event)
                    if chat_id == session_id and _background_card(event) and (
                            event["id"] in background or not _ended(event) and not event["delegation"].get("terminal_reason")):
                        check = check or (_ended(event) and not _ended(background.get(event["id"], event)))
                        background[event["id"]] = dict(event)
                    if isinstance(event.get("child_session_id"), str):
                        follow(event["child_session_id"], True)
                    if event.get("type") == "tool" and event.get("state") == "pending" and (chat_id, event.get("id")) not in answered:
                        answered.add((chat_id, event["id"]))
                        refused = self._answer(chat_id, subagent, event, can_use_tool, on_question)
                        if refused is not None:
                            yield refused
                if kind == "delta" and chat_id == session_id and message.get("id") in replies:
                    reply = replies[message["id"]]
                    reply["text"] = (reply.get("text") or "") + str(message.get("text") or "")
                if (kind == "status" and chat_id == session_id and message.get("status") == "idle"
                        and isinstance(message.get("turn_result"), dict)):
                    turn = message["turn_result"]
                if turn is not None:
                    ended, check = turn, True
                    running = sum(not _ended(card) for card in background.values())
                    if running:
                        yield {"type": "background_wait", "session_id": session_id, "running": running}
                if ended is not None and check and all(_ended(card) for card in background.values()):
                    check = False
                    # The app may continue the chat once a background subagent finished: that turn is followed too.
                    if background and self._busy(session_id):
                        continue
                    yield result_record(session_id, ended, replies, list(background.values()))
                    return
        finally:
            stop.set()
            for connection in list(opened):
                # Ends a read blocked in another thread at once (closing it here would wait for that read); the
                # reading thread then closes it.
                try:
                    connection.sock.shutdown(socket.SHUT_RDWR)
                except (AttributeError, OSError):
                    pass

    def _busy(self, session_id):
        """Whether the chat runs a turn (or anything else) now; a chat that is gone is not."""
        try:
            return self.info(session_id).get("status", "idle") != "idle"
        except LocalAgentError:
            return False

    def _answer(self, chat_id, subagent, event, can_use_tool, on_question):
        """Answer one waiting card. A callback that raises still answers it (declined, or questions skipped). An
        answer the app refuses is replaced by that default: returns the answer_refused message, else None."""
        name, arguments = event.get("name"), event.get("input") if isinstance(event.get("input"), dict) else {}
        context = {"session_id": chat_id, "event_id": event["id"], "preview": event.get("preview"), "subagent": subagent,
                   **{key: event[key] for key in CARD_WARNINGS if event.get(key)}}
        verdict, answers, failure, refused = None, None, None, None
        try:
            if name == "ask_user":
                answers = on_question(arguments.get("questions") or [], context) if on_question else None
            elif can_use_tool is not None:
                verdict = can_use_tool(name, arguments, context)
        except BaseException as exc:
            verdict, answers, failure = None, None, exc
        for attempt in (1, 2):
            try:
                self._send_answer(chat_id, name, event["id"], verdict, answers)
            except LocalAgentError as exc:
                detail = exc.detail.get("detail") if isinstance(exc.detail, dict) else None
                if exc.status != 400 or attempt == 2 and detail not in CARD_GONE:
                    raise
                if detail not in CARD_GONE:
                    refused, verdict, answers = str(detail or exc), None, None  # Again, with the default.
                    continue
            break
        if failure is not None:
            raise failure
        if refused is None:
            return None
        fallback = "skipped" if name == "ask_user" else "keep_planning" if name == "submit_plan" else "declined"
        return {"type": "answer_refused", "session_id": chat_id, "event_id": event["id"], "name": name, "error": refused,
                "answered": fallback, **({"subagent": True} if subagent else {})}

    def _send_answer(self, chat_id, name, event_id, verdict, answers):
        """The card's answer: the callback's, or (verdict and answers None) declined, keep planning or skipped."""
        if name == "ask_user":
            self.answer_questions(chat_id, event_id, answers)
        elif name == "submit_plan":
            if isinstance(verdict, Allow):
                self.answer_plan(chat_id, event_id, verdict.mode if verdict.mode in ("acceptEdits", "manual") else "manual")
            else:
                self.answer_plan(chat_id, event_id, "keep_planning",
                                 verdict.message if isinstance(verdict, Deny) and verdict.message else NO_REVIEWER)
        elif isinstance(verdict, Allow):
            self.answer_approval(chat_id, event_id, True)
        else:
            self.answer_approval(chat_id, event_id, False,
                                 verdict.message if isinstance(verdict, Deny) and verdict.message else NO_APPROVER)


def _events_of(message):
    """The events a stream message carries: one event, or a snapshot's."""
    if message.get("type") == "event" and isinstance(message.get("event"), dict):
        return [message["event"]]
    if message.get("type") == "snapshot" and isinstance(message.get("session"), dict):
        return [event for event in message["session"].get("events") or [] if isinstance(event, dict)]
    return []


def _background_card(event):
    """Whether a chat's event is the card of a background delegation (delegate_task background)."""
    return (event.get("type") == "tool" and event.get("name") == "delegate_task" and isinstance(event.get("id"), str)
            and isinstance(event.get("delegation"), dict) and event["delegation"].get("background") is True)


def _ended(card):
    """Whether a background delegation ended: its result is on its card, or it was stopped while it waited for a
    slot (it never had a subagent)."""
    return isinstance(card.get("delegation_result"), dict) or bool(
        card["delegation"].get("terminal_reason") and not card.get("child_session_id"))


def background_result(card):
    """A background subagent's entry in the result record: its delegate_task result (status, terminal_reason,
    report or output, changed_files, ...) once it ended, else its last status."""
    values = card.get("input") if isinstance(card.get("input"), dict) else {}
    entry = {"task": values.get("task")}
    if isinstance(card.get("delegation_result"), dict):
        return {**entry, **card["delegation_result"]}
    progress = card["delegation"]
    return {**entry, "child_session_id": card.get("child_session_id"), "agent_type": progress.get("agent_type"),
            "status": progress.get("status") or "running", "terminal_reason": progress.get("terminal_reason")}


def result_record(session_id, turn, replies=None, background=None):
    """query()'s last message: the turn's result record (turns.turn_result) with the final reply's text, and
    (background: the chat's background delegation cards query() followed) background_results."""
    texts = [event.get("text") for event in (replies or {}).values()
             if isinstance(event.get("text"), str) and event["text"] and event.get("created", 0) >= turn.get("started", 0)]
    reason = turn.get("terminal_reason")
    return {"type": "result", "session_id": session_id, **{key: value for key, value in turn.items() if key not in ("started", "ended")},
            "exit_code": exit_code(reason), "result": texts[-1] if texts else "",
            **({"background_results": [background_result(card) for card in background]} if background is not None else {})}
