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
hook_change ({hooks, original}: before-tool hooks replaced what the agent proposed, the input is what would run),
uncertain (the command could not be checked exactly against the deny and ask rules) and sibling_change
({subagent_id, agent_type, path}: another subagent of the chat changed the file).

When the turn ends while background subagents of the chat (delegate_task background) still work, query() yields
{"type": "background_wait", "running": n} and keeps following them, answering their cards (theirs, and their
subagent_start and subagent_stop hooks' in the chat) the same way, until they end: a delegation result on the
card, or a stop. The result's background_results lists each one's end (its delegate_task result, or its last
status). A turn the app starts in the chat meanwhile (Settings → Subagents → Continue the chat when a background
subagent finishes) is followed too, and the result is the last turn's. A turn the user or another script starts
in the chat is theirs: query() neither answers its cards nor reports it, and a ! command or a compaction is no
turn. The result comes once nothing runs in the chat any more (it is idle, or a card of a background subagent
query() does not follow waits). Every model request is a paid call to your Databricks workspace, made with the
app's credentials.
"""
import http.client
import json
import queue
import socket
import threading
import time
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
CARD_WARNINGS = ("secret_files", "protected", "hook_ask", "hook_change", "uncertain", "sibling_change")
# How long query() waits for the turn the app may start once a background subagent finished (the wake option):
# the app starts it right after the subagent's notice, or after the turn, ! command or compaction it ended in.
WAKE_GRACE = 1.0


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

    def wait_result(self, session_id, after=None, background=False):
        """The result record of the chat's running turn once it ends, or of its last turn when no turn runs (it is
        idle, or a background subagent's card waits; a ! command or a compaction is waited for); None when it had
        no turn, or none that started after `after` (the started time of an earlier turn's result: a message that
        never started a turn). background: the record also has background_results, the background subagents of
        that turn (those it started, and those still working when it started) as they are then."""
        replies, cards, runs, last = {}, {}, False, None
        for message in self.events(session_id):
            kind, status = message.get("type"), message.get("status")
            for event in _events_of(message):
                if event.get("type") == "assistant":
                    replies[event.get("id")] = dict(event)
                elif _background_card(event):
                    cards[event["id"]] = dict(event)
                elif kind == "event" and event.get("type") == "user" and not event.get("steered"):
                    runs = True  # A turn starts.
            if kind == "snapshot":
                chat = message.get("session") or {}
                last = chat.get("last_turn_result") if isinstance(chat.get("last_turn_result"), dict) else None
                runs, status = _turn_runs(chat), chat.get("status", "idle")
                done = status == "idle" or status == "awaiting_approval" and not runs
            elif kind == "status":
                if isinstance(message.get("turn_result"), dict):  # A turn ended.
                    last, runs = message["turn_result"], False
                done = status in ("idle", "awaiting_approval") and not runs
            elif kind == "stream_closed" and message.get("reason") == "shutdown":
                raise LocalAgentError(SHUT_DOWN)
            else:
                done = False
            if done:
                if last is None or (after is not None and last.get("started") == after):
                    return None
                return result_record(session_id, last, replies, _turn_cards(cards, last) if background else None)
        raise LocalAgentError("The app closed the chat's stream before the turn ended.")

    # One prompt

    def query(self, prompt, *, session_id=None, workspace=None, permission_mode="plan", model=None, effort=None,
              max_steps=None, spending_limit_dbu=None, turn_token_limit=None, turn_time_limit_minutes=None,
              can_use_tool=None, on_question=None, verbatim=False):
        """Send prompt to a new chat (or the idle chat session_id, which keeps its own mode, endpoint, effort,
        folder and limits: the options of a new chat are not applied to it) and yield its messages (each with
        session_id; a subagent's cards and messages also come, with subagent True) until the turn ends and the
        chat's background subagents ended (and a turn the app started for them), then one result record: {type:
        'result', session_id, terminal_reason, is_error, exit_code, duration_s, steps, tool_calls, usage, tokens,
        estimated_dbu, unpriced_calls, subagents, changed_files, result (the final reply), background_results}.
        Cards of a turn the user or another script starts in the chat meanwhile are left to them."""
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
        answered, replies, sent = set(), {}, False
        # The chat's turns after the prompt: starts holds (created, whose) per turn start, "own" (the prompt's turn,
        # or one the app started when a background subagent finished: followed) or "other" (one the user or another
        # script started: neither answered nor reported); turn is whose turn runs now (None: none), ended the
        # result of the last own turn and latest the started time of the last turn end read. background: the
        # chat's background delegations followed ({card id: its card}) until each ended; check: whether to see if
        # query() is done; due: one finished since the last turn started, so the app may still continue the chat
        # (grace: until when query() waits for that turn).
        known, starts, turn, ended, latest = set(), [], None, None, None
        background, check, due, grace = {}, False, False, None
        try:
            while True:
                try:
                    chat_id, subagent, message = messages.get(
                        timeout=None if grace is None else max(0.0, grace - time.monotonic()))
                except queue.Empty:  # The app did not continue the chat.
                    yield result_record(session_id, ended, replies, self._background_ends(session_id, background),
                                        _next_start(starts, ended))
                    return
                kind, parent = message.get("type"), chat_id == session_id
                if kind == "keep_alive":
                    continue
                if kind in ("stream_error", "stream_ended") and parent:
                    if ended is not None and turn != "own":  # Only background subagents were left: what they did so far.
                        yield result_record(session_id, ended, replies, list(background.values()), _next_start(starts, ended))
                        return
                    raise LocalAgentError(message.get("error") or "The app closed the chat's stream before the turn ended.")
                if kind == "stream_ended":
                    continue
                finished, reconnected = None, False  # finished: the result of a turn that just ended.
                if kind == "snapshot" and parent:
                    chat = message["session"]
                    last_turn = chat.get("last_turn_result") if isinstance(chat.get("last_turn_result"), dict) else None
                    if not sent:
                        if chat.get("status", "idle") != "idle":
                            raise LocalAgentError("This chat is busy: wait for its turn to end, or use another chat.")
                        latest = last_turn and last_turn.get("started")
                        known.update(event.get("id") for event in _events_of(message) if event.get("type") == "user")
                        self.send(session_id, prompt, verbatim)
                        sent = True
                    else:  # A new stream after the app dropped a slow reader: what happened meanwhile.
                        reconnected = check = True
                        if last_turn and last_turn.get("started") != latest:
                            finished = last_turn
                yield {**message, "session_id": chat_id, **({"subagent": True} if subagent else {})}
                for event in _events_of(message):
                    if parent and event.get("type") == "user" and not event.get("steered") and event.get("id") not in known:
                        known.add(event.get("id"))  # A turn starts.
                        whose = "own" if not starts or event.get("background_wake") is True and ended is not None else "other"
                        starts.append((event.get("created", 0), whose))
                        turn, check, due, grace = whose, True, False, None
                    if parent and event.get("type") == "assistant":
                        replies[event.get("id")] = dict(event)
                    if parent and _background_card(event):
                        previous = background.get(event["id"])
                        # Followed: those running when the prompt was sent, and those its own turns start.
                        if previous is not None or turn != "other" and not _ended(event) and not event["delegation"].get("terminal_reason"):
                            background[event["id"]] = dict(event)
                        if previous is not None and _ended(event) and not _ended(previous):
                            result = event.get("delegation_result")
                            check, due = True, due or isinstance(result, dict) and result.get("status") in ("completed", "failed")
                    child = event.get("child_session_id")
                    if isinstance(child, str) and (not parent or turn == "own" or event.get("id") in background):
                        follow(child, True)
                    if (event.get("type") == "tool" and event.get("state") == "pending" and (chat_id, event.get("id")) not in answered
                            and (not parent or turn == "own" or _hook_of(event, background))):
                        answered.add((chat_id, event["id"]))
                        refused = self._answer(chat_id, subagent, event, can_use_tool, on_question)
                        if refused is not None:
                            yield refused
                if kind == "delta" and parent and message.get("id") in replies:
                    reply = replies[message["id"]]
                    reply["text"] = (reply.get("text") or "") + str(message.get("text") or "")
                if kind == "status" and parent:
                    if isinstance(message.get("turn_result"), dict):
                        finished = message["turn_result"]
                    check = check or message.get("status") in ("idle", "awaiting_approval")
                if finished is not None:
                    latest = finished.get("started")
                    if _whose(starts, finished) == "own":
                        ended = finished
                        running = sum(not _ended(card) for card in background.values())
                        if running:
                            yield {"type": "background_wait", "session_id": session_id, "running": running}
                    turn, check = None, True
                if reconnected:
                    turn = starts[-1][1] if starts and _turn_runs(chat) else None
                if check and ended is not None and turn != "own" and all(_ended(card) for card in background.values()):
                    check = False
                    # Done once nothing runs in the chat (a turn the user or another script runs is theirs) and the
                    # app did not continue it after a background subagent finished.
                    settled = turn == "other" or self._settled(session_id, latest)
                    if not settled:
                        grace = None
                    elif turn != "other" and grace is None and due and self._wakes():
                        due, grace = False, time.monotonic() + WAKE_GRACE
                    elif turn == "other" or grace is None:
                        yield result_record(session_id, ended, replies, self._background_ends(session_id, background),
                                            _next_start(starts, ended))
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

    def _settled(self, session_id, latest):
        """Whether nothing runs in the chat now (it is idle, or a card waits while no turn runs) and its last turn
        end is the one the stream showed last (latest: its started time); a chat that is gone is settled."""
        try:
            chat = self.info(session_id)
        except LocalAgentError:
            return True
        last = chat.get("last_turn_result") if isinstance(chat.get("last_turn_result"), dict) else {}
        return (chat.get("status", "idle") in ("idle", "awaiting_approval") and not _turn_runs(chat)
                and last.get("started") == latest)

    def _wakes(self):
        """Whether Settings → Subagents → Continue the chat when a background subagent finishes is on (also when
        the settings cannot be read)."""
        try:
            return self.request("GET", "/api/settings").get("background_subagent_wake") is not False
        except (LocalAgentError, AttributeError):
            return True

    def _background_ends(self, session_id, background):
        """The background delegation cards followed, for the result; one that ended without its result (a stop)
        is read again from the chat, whose card may have it by now."""
        cards = list(background.values())
        if all(isinstance(card.get("delegation_result"), dict) for card in cards):
            return cards
        try:
            events = {event.get("id"): event for event in self.session(session_id).get("events") or []
                      if isinstance(event, dict)}
        except (LocalAgentError, AttributeError):
            return cards
        return [events[card["id"]] if _background_card(events.get(card["id"], {})) else card for card in cards]

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
    """Whether a background delegation ended: its result is on its card, or it was stopped (also before its
    subagent started, when no result follows), or it ended without a subagent (stopped while it waited for a slot).
    Another end of a subagent comes with its result."""
    reason = card["delegation"].get("terminal_reason")
    return isinstance(card.get("delegation_result"), dict) or bool(
        reason and (reason in STOP_REASONS or not card.get("child_session_id")))


def _hook_of(card, background):
    """Whether a card of the chat is a subagent_start or subagent_stop hook's for a background subagent followed."""
    child = card.get("input", {}).get("subagent_session_id") if isinstance(card.get("input"), dict) else None
    return bool(child) and str(card.get("name")).startswith("hook_subagent_") and any(
        item.get("child_session_id") == child for item in background.values())


def _turn_runs(chat):
    """Whether a turn runs in the chat: its turn_ended is None from the turn's start to its end."""
    return "turn_ended" in chat and chat["turn_ended"] is None


def _whose(starts, turn):
    """Whose a turn's result is ("own" or "other"): that of the last turn start before its end (starts: (created,
    whose) each), the prompt's when none was read."""
    owners = [whose for created, whose in starts if created <= turn.get("ended", created)]
    return owners[-1] if owners else "own"


def _turn_cards(cards, turn):
    """The background delegation cards ({id: card}) of a turn: those it started, and those still working when it
    started."""
    started, ended = turn.get("started", 0), turn.get("ended", float("inf"))
    return [card for card in cards.values() if started <= card.get("created", 0) <= ended or (
        card.get("created", 0) < started and (not _ended(card) or card["delegation"].get("ended", started) >= started))]


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


def _next_start(starts, turn):
    """When the chat's next turn after this one started (starts: (created, whose) each), or None."""
    return next((created for created, _ in starts if created > turn.get("ended", float("inf"))), None)


def result_record(session_id, turn, replies=None, background=None, until=None):
    """query()'s last message: the turn's result record (turns.turn_result) with the final reply's text (a reply
    of the turn: until is when a later turn started), and (background: the chat's background delegation cards
    query() followed) background_results."""
    texts = [event.get("text") for event in (replies or {}).values()
             if isinstance(event.get("text"), str) and event["text"] and event.get("created", 0) >= turn.get("started", 0)
             and (until is None or event.get("created", 0) < until)]
    reason = turn.get("terminal_reason")
    return {"type": "result", "session_id": session_id, **{key: value for key, value in turn.items() if key not in ("started", "ended")},
            "exit_code": exit_code(reason), "result": texts[-1] if texts else "",
            **({"background_results": [background_result(card) for card in background]} if background is not None else {})}
