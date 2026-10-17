"""Hooks around tool calls and around a chat's turns: structured hook output and lifecycle events.

AgentManager (agents.py) inherits LifecycleHooks; this module never imports agents.py. Every
hook is a local command from Agent tools → Extensions (extensions.py runs it and reads the one
JSON object it may print). Each hook goes through the permission model hooks always had: the
permission is named hook_<event>, Manual asks with a hook card, rules allow, ask or deny a hook
by ID, Plan mode and restricted tool profiles skip hooks, and Bypass runs them without a card.

Events (extensions.HOOK_EVENTS):
- before_tool, after_tool, tool_failure: around one tool call (execute_tool). A before-tool
  hook runs after the call's schema check and its deny rules, before the call's own approval
  card, so the card shows the arguments that will run and the permission is evaluated on them.
- user_prompt (prompt_hooks): before a user's message goes to the model; never for the app's
  hidden continuations or a subagent's delegated prompt.
- session_start (prompt_hooks): before a chat's first model request only.
- agent_stop / subagent_stop (stop_hooks): when a turn would end with a final reply; subagent_stop
  only for a subagent's delegated turn (a subagent chat the user continues is an ordinary chat).
- subagent_start (subagent_start_hooks): before a subagent's first request.
- before_compaction (compaction_hook): once per compaction, before its first summary request; its
  context joins the preservation note. It cannot block: a decision is ignored (listed on its card).
- notification (notification_hooks): for each of the app's own notifications (notify.attention_message),
  in the background, only when the hook may run without a card (Bypass or an allow rule), output ignored.
Subagent events run in the parent chat: its mode, rules and cards. A tool event's stdin, and any event's
of a subagent chat, names the agent it belongs to (hook_agent).
"""
import asyncio
import fnmatch
import json
import logging

from .approvals import declined
from .context import HOOK_CONTINUATION_PREFIX
from .extensions import TOOL_HOOK_EVENTS
from .permissions import Verdict
from .planning import report_text, submitted_report
from .result_budget import cap_text
from .notify import short_title
from .tool_profiles import COVERED_BY
from .tool_schema import validate_arguments

log = logging.getLogger("local_agent.hooks")

# Consecutive continuations a stop hook may ask for in one turn.
STOP_CAPS = {"agent_stop": 3, "subagent_stop": 2}
WARNING_LABELS = {"after_tool": "After-tool hook", "tool_failure": "Failure-hook", "user_prompt": "User-prompt hook",
                  "session_start": "Session-start hook", "agent_stop": "Agent-stop hook",
                  "subagent_start": "Subagent-start hook", "subagent_stop": "Subagent-stop hook",
                  "before_compaction": "Before-compaction hook", "notification": "Notification hook"}
WARNING_TAILS = {
    "after_tool": "The tool action already completed.",
    "tool_failure": "The original tool error is unchanged.",
    "user_prompt": "This hook did not change the message.",
    "session_start": "The chat started without this hook's context.",
    "agent_stop": "This hook did not keep the agent working.",
    "subagent_start": "The subagent started without this hook's context.",
    "subagent_stop": "This hook did not change the subagent's report.",
    "before_compaction": "The compaction ran without this hook's context.",
    "notification": "",
}
HOOK_ASK_DONT_ASK = ("Not run: a before-tool hook ({hook}) asked for your approval, which Don't ask mode does not show. "
                     "Continue without it and tell the user which action needs their approval.")
# A subagent's change to a file a running sibling changed (AgentManager._execute_tool, DelegateManager.sibling_claim).
SIBLING_DONT_ASK = ("Not run: another subagent of this chat ({subagent}, {kind}) changed {path} during this run, and such "
                    "a change needs an approval card, which Don't ask mode does not show. Leave the file to that "
                    "subagent and say so in your report.")
PAYLOAD_TEXT_BYTES = 48_000  # a prompt, reply or task in a hook's stdin (run_hook refuses more than 128 KB)
NOTE_CHARS = 4_000  # all hook context added to one tool result or one message
SHOWN_ARGUMENTS_BYTES = 16_000  # arguments a card keeps for its before/after view


def clip(text, limit):
    text = str(text)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def fit_text(text, limit=PAYLOAD_TEXT_BYTES):
    """(text, shortened): text whose JSON string form is at most limit bytes."""
    text = text if isinstance(text, str) else ""
    if len(json.dumps(text)) <= limit:
        return text, False
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if len(json.dumps(text[:middle])) <= limit:
            low = middle
        else:
            high = middle - 1
    return text[:low], True


def shown_arguments(value):
    """Arguments as a card keeps them: the object, or its JSON text shortened when it is large."""
    text = json.dumps(value, ensure_ascii=False)
    if len(text.encode("utf-8")) <= SHOWN_ARGUMENTS_BYTES:
        return value
    return clip(text, SHOWN_ARGUMENTS_BYTES // 4)


def fence_for(text):
    """A Markdown fence longer than any backtick run in text."""
    longest = run = 0
    for character in text:
        run = run + 1 if character == "`" else 0
        longest = max(longest, run)
    return "`" * max(3, longest + 1)


def note_lines(notes, heading):
    """The labelled block of hook context: notes are (event, hook ID, context)."""
    lines, used = [], 0
    for event, hook_id, context in notes:
        line = f"- {event} hook {hook_id}: {context}"
        if used + len(line) > NOTE_CHARS:
            lines.append(f"- … context from {len(notes) - len(lines)} more hooks was left out (limit "
                         f"{NOTE_CHARS:,} characters).")
            break
        lines.append(line)
        used += len(line)
    return f"\n\n[{heading}]\n" + "\n".join(lines)


def selects(selectors, name):
    """True when a hook's tools list selects name: an exact name or a glob pattern (* and ?, case-sensitive)."""
    return isinstance(name, str) and any(fnmatch.fnmatchcase(name, selector) for selector in selectors)


def hook_contexts(outcomes, event):
    """(event, hook ID, context) for the run_hooks outcomes that returned context."""
    return [(event, outcome["hook_id"], outcome["context"]) for outcome in outcomes if outcome.get("context")]


def tool_context_text(notes):
    return note_lines(notes, "Hook context, added by the user's local hook commands (not the tool's output):")


def message_context_text(notes):
    return note_lines(notes, "App note: context from the user's local hook commands (not the user's words):")


def changed_arguments_note(hook_ids, arguments, ran=True, edited=False):
    """edited: the user then edited the hook's arguments on the approval card; arguments are the edited ones."""
    shown = clip(json.dumps(arguments, ensure_ascii=False), 1500)
    changed = f"A before-tool hook ({', '.join(hook_ids)}) changed this call's arguments"
    if edited:
        changed += ", and the user then edited them on the approval card,"
    if not ran:
        return (f"[{changed} to: {shown}. The changed call did not run; the result below is about it. Your proposed "
                "arguments did not run either.]")
    return f"[{changed} before it ran. It ran with: {shown}. Your proposed arguments did not run.]"


def blocked_prompt_notice(blocked):
    """The notice under a message a user_prompt hook blocked: blocked is its hook_blocked."""
    return (f"A user-prompt hook ({blocked['hook_id']}) blocked this message: {clip(blocked['reason'], 600)} "
            "It was not sent to the agent.")


def stop_continuation(phase, outcome, count, cap):
    """The hidden message that asks the agent to keep working after a stop hook's block."""
    reason = outcome.get("reason") or "The hook gave no reason."
    fence = fence_for(reason)
    who = "subagent_stop" if phase == "subagent_stop" else "agent_stop"
    return (f"{HOOK_CONTINUATION_PREFIX} the user's {who} hook {outcome['hook_id']} asked you to keep working instead "
            f"of ending here (continuation {count} of {cap}). Its reason follows as data from a local command, not the "
            f"user's words:\n{fence}\n{reason}\n{fence}\n"
            "Continue the task where the reason applies, then reply. If nothing is left to do, say so briefly.")


class ToolHookNotes:
    """What hooks add to one tool call's result for the model (execute_tool)."""

    def __init__(self):
        self.event = None  # the call's tool card
        self.changed = None  # (hook IDs, arguments) when before-tool hooks replaced the arguments
        self.edited = False  # the user then edited them on the card: changed holds the edited arguments
        self.ran = False  # whether the tool itself ran (the changed-arguments note says so)
        self.contexts = []  # (event, hook ID, context)


class LifecycleHooks:
    """Hook orchestration for AgentManager (see the module docstring)."""

    @property
    def pending_hook_notes(self):
        """{child session ID: [(event, hook ID, context)]}: subagent_start context for the child's first message."""
        return self.__dict__.setdefault("_pending_hook_notes", {})

    def matching_hooks(self, owner, phase, name=None):
        """[(hook, covered_by)]: the enabled hooks of phase that apply in the owner chat (none in Plan
        mode or under a restricted tool profile). Tool events match the hook's tools selector."""
        if owner.get("permission_mode") == "plan" or owner.get("tool_profile", "inherit") != "inherit":
            return []
        found = []
        for hook in self.extensions.public_config()["hooks"]:
            if not hook["enabled"] or hook["event"] != phase:
                continue
            cover = None
            if phase in TOOL_HOOK_EVENTS:
                # Names or glob patterns (selects). A hook for read_file also runs for outline_file, and one for
                # list_files or find_files for project_overview (COVERED_BY), as their rules and turned-off tools do.
                selected = hook.get("tools")
                if selected is not None and not selects(selected, name):
                    cover = next((tool for tool in COVERED_BY.get(name, ()) if selects(selected, tool)), None)
                    if cover is None:
                        continue
            found.append((hook, cover))
        return found

    async def run_hooks(self, session, phase, name=None, arguments=None, result=None, *, call_id=None, error=None,
                        payload=None, owner=None, check=None):
        """Run the enabled hooks of one event in configuration order. Returns one outcome per hook that
        ran and exited 0: {hook_id, decision?, reason?, context?, arguments?, prompt?, report?}.

        Tool events take name, arguments, result, call_id and error; the other events take payload
        (their stdin fields). owner is the chat whose mode, rules and cards apply (default session;
        the parent for subagent events). check(parsed, hook) may adjust a structured result or raise
        ValueError (the hook then fails). A later hook sees an earlier hook's arguments or prompt, and
        a deny or block decision ends the event's hooks. A failing before-tool hook raises ValueError
        ("Before-tool hook blocked …"); other failing hooks leave a warning notice in the owner chat."""
        owner = owner or session
        tool_event = phase in TOOL_HOOK_EVENTS
        fields = dict(payload or {})
        outcomes = []
        for hook, cover in self.matching_hooks(owner, phase, name):
            if asyncio.current_task().cancelling():
                return outcomes
            hook_tools = self.session_tools(owner)
            verdict = self.permission(owner, hook_tools, f"hook_{phase}", {"hook_id": hook["id"]})
            if tool_event:
                card_input = {"hook_id": hook["id"], "tool": name, "call_id": call_id}
                subject = f"For tool: {name}"
            else:
                card_input = {"hook_id": hook["id"], "event": phase,
                              **({"subagent_session_id": fields["subagent_session_id"]}
                                 if fields.get("subagent_session_id") else {})}
                subject = f"Event: {phase}"
            event = await self.event(owner, "tool", name=f"hook_{phase}", state="running", input=card_input,
                                     preview=f"Hook: {hook['id']}\n{subject}\nCommand: {hook['command']}\n"
                                             f"Timeout: {hook['timeout_seconds']} seconds", output="",
                                     **(self.verdict_fields(verdict) if verdict.decision == "allow" else {}))
            try:
                if verdict.decision == "deny":
                    # A deny rule, or Don't ask with no allow rule: the hook does not run.
                    await self.update_event(owner, event, state="rejected", approval="blocked",
                                            blocked_reason=verdict.message)
                    raise ValueError(verdict.message)
                if verdict.decision == "ask":
                    status = self.statuses.get(owner["id"])
                    try:
                        allowed, _ = await self.request_approval(owner, hook_tools, event, f"hook_{phase}",
                                                                 {"hook_id": hook["id"]})
                    finally:
                        # ended: the owner has no running turn, so this hook runs in a background subagent's task
                        # (its turn may have ended during the wait, after approve() set it running).
                        turn = self.tasks.get(owner["id"])
                        ended = turn is None or turn.done()
                        if (status != "running" or ended) and (owner is not session or ended
                                                               or not asyncio.current_task().cancelling()):
                            await self.after_hook_card(owner, event, status)
                    if not allowed:
                        approval = event.get("approval")
                        if approval is None:  # Denied by a fresh check just before the card.
                            await self.update_event(owner, event, approval="blocked", blocked_reason=event["output"])
                        follow = f"Do not retry {name} without a new instruction." if tool_event else "The hook did not run."
                        raise ValueError(
                            "Hook approval was not answered in time." if approval == "expired"
                            else declined(event, "this hook", follow) if approval == "declined" else event["blocked_reason"])
                if asyncio.current_task().cancelling():
                    return outcomes
                if tool_event:
                    stdin = {"event": phase, "session_id": session["id"], "tool": name, "arguments": arguments,
                             "call_id": call_id, **({"covered_by": cover} if cover else {}),
                             "agent": self.hook_agent(session)}
                    if result is not None:
                        stdin["result"] = result
                    if phase == "tool_failure":
                        stdin.update(error=error, outcome="error")
                else:
                    stdin = {"event": phase, **fields}
                    if session.get("is_subagent"):
                        stdin.setdefault("agent", self.hook_agent(session))
                # The Python environment applies; variables named in Settings are not passed to hooks.
                outcome = await self.extensions.run_hook(hook, stdin, owner["workspace"],
                                                         env=self.jobs.command_environment(named=False))
                output = outcome["output"]
                if outcome["exit_code"] != 0 or outcome.get("timed_out"):
                    raise ValueError(f"Hook failed (exit {outcome['exit_code']}): {output}")
                if outcome.get("structured_error"):
                    raise ValueError(f"Hook output could not be used: {outcome['structured_error']}")
                parsed = outcome.get("structured")
                if parsed is not None and check is not None:
                    parsed = check(parsed, hook)
                summary = self.hook_summary(parsed, arguments, fields.get("prompt")) if parsed is not None else None
                await self.update_event(owner, event, state="completed", output=output,
                                        **({"hook_result": summary} if summary else {}))
                parsed = {key: value for key, value in (parsed or {}).items() if key != "ignored"}
                outcomes.append({"hook_id": hook["id"], **parsed})
                if "arguments" in parsed:
                    arguments = parsed["arguments"]
                if "prompt" in parsed:
                    fields["prompt"] = parsed["prompt"]
                if parsed.get("decision") in ("deny", "block"):
                    break
            except (ValueError, OSError, TimeoutError) as exc:
                warning = self.settings.redact(str(exc))
                await self.update_event(owner, event, state="rejected" if event["state"] == "rejected" else "error",
                                        output=warning)
                if phase == "before_tool":
                    raise ValueError(f"Before-tool hook blocked {name}: {warning}") from exc
                if verdict.decision == "deny":
                    continue  # Blocked by a rule or Don't ask: the hook card says so; no notice every time.
                warning = warning.rstrip().removesuffix(".")
                await self.event(owner, "notice", text=f"{WARNING_LABELS[phase]} warning: {warning}. {WARNING_TAILS[phase]}")
        return outcomes

    async def after_hook_card(self, owner, event, previous):
        """After a hook card in a chat that was not just running (a subagent's card in its delegating
        parent): approve() left it running, so put back its previous status, or awaiting_approval
        while another of its cards waits. A card a stopped subagent left waiting shows cancelled. A
        parent whose turn is over (an idle parent of a background subagent, or its turn ended while the card
        waited) becomes idle again, and stays running when the user started a turn there while the card
        waited."""
        if event.get("state") == "pending":
            await self.update_event(owner, event, state="cancelled")
        task = self.tasks.get(owner["id"])
        waiting = any(key[0] == owner["id"] and not future.done() for key, future in self.pending.items())
        if previous == "idle" or task is None or task.done():
            if task is None or task.done():
                status = "awaiting_approval" if waiting else "idle"
            elif task.cancelling():
                return  # The new turn is stopping: its own end sets the status.
            else:
                status = "awaiting_approval" if waiting else "running"
            if self.statuses.get(owner["id"], "idle") != status:
                await self.status(owner, status)
            return
        if previous is None or task.cancelling():
            return  # The owner's turn is stopping: its own end sets its status.
        # Another card's own wait ended meanwhile: the chat was delegating under it.
        status = "awaiting_approval" if waiting else "delegating" if previous == "awaiting_approval" else previous
        if self.statuses.get(owner["id"]) != status:
            await self.status(owner, status)

    @staticmethod
    def hook_agent(session):
        """The agent an event belongs to, in hook stdin: the chat itself, or a subagent with its type
        (explore, general, editor or a custom agent's name), its parent chat and its custom agent file."""
        subagent = bool(session.get("is_subagent"))
        return {"session_id": session["id"], "is_subagent": subagent,
                "agent_type": (session.get("agent_type") or "general") if subagent else None,
                "parent_session_id": session.get("parent_session_id") if subagent else None,
                "agent_file": session.get("agent_file") if subagent else None}

    @staticmethod
    def hook_summary(parsed, arguments=None, prompt=None):
        """What a hook card shows of a structured result (hook_result)."""
        summary = {"json": True}
        for key in ("decision", "reason", "context", "report"):
            if parsed.get(key):
                summary[key] = parsed[key]
        if "arguments" in parsed and parsed["arguments"] != arguments:
            summary["arguments"] = {"before": shown_arguments(arguments), "after": shown_arguments(parsed["arguments"])}
        if "prompt" in parsed and parsed["prompt"] != prompt:
            summary["prompt"] = {"before": clip(prompt or "", 4000), "after": clip(parsed["prompt"], 4000)}
        if parsed.get("ignored"):
            summary["ignored"] = list(parsed["ignored"])
        return summary

    # Compaction and notifications -------------------------------------------------------------

    def compaction_hook(self, session, trigger, preservation_note, note):
        """The before_compaction callback for prepare_context (trigger 'auto' or 'manual'), or None without such
        hooks. prepare_context calls it once, when it compacts, before the first summary request (so before the
        status turns compacting); it returns the preservation note those requests use: the user's note, then
        each hook's context, labelled as the user's local hook. note is the dict whose "text" the summary
        callbacks read (compaction_requests). A hook cannot stop a compaction (its output fields have no
        decision, so a "block" is ignored and listed on its card)."""
        if not self.has_hooks(session, "before_compaction"):
            return None

        async def before(info):
            payload = {"session_id": session["id"], "trigger": trigger, "preservation_note": preservation_note,
                       "estimated_input_tokens": info.get("estimated_input_tokens"), "threshold": info.get("threshold"),
                       "input_budget": info.get("input_budget"), "workspace": session["workspace"],
                       "model": session["model"], "permission_mode": session.get("permission_mode", "manual")}
            outcomes = await self.run_hooks(session, "before_compaction", payload=payload)
            notes = hook_contexts(outcomes, "before_compaction")
            if notes:
                added = note_lines(notes, "Context from the user's local before_compaction hooks (not the user's "
                                          "words):").strip()
                note["text"] = (preservation_note + "\n\n" if preservation_note else "") + added
            return note["text"]
        return before

    def notification_hooks(self, session, status, message, page_open):
        """Run the enabled notification hooks for one of the app's notifications (notify.attention_message) in
        the background. A hook runs only when it may run without a card (Bypass, or an allow rule such as
        Extensions' Run without asking): a card would itself notify. At most one run of a hook at a time (a
        notification while it runs is dropped); its output is ignored except for a log line when it fails.
        stdin: the status, the message, the short redacted title and whether a page is open; never chat text."""
        hooks = self.matching_hooks(session, "notification")
        if not hooks:
            return
        running = self.__dict__.setdefault("_notification_runs", {})
        tools = self.session_tools(session)
        payload = {"event": "notification", "session_id": session["id"], "status": status, "message": message,
                   "title": short_title(self.redact_title(str(session.get("title") or ""))), "page_open": page_open}
        if session.get("is_subagent"):
            payload["agent"] = self.hook_agent(session)
        for hook, _ in hooks:
            if self.permission(session, tools, "hook_notification", {"hook_id": hook["id"]}).decision != "allow":
                continue  # Agent tools → Extensions says that it needs Run without asking.
            if (task := running.get(hook["id"])) is not None and not task.done():
                continue
            running[hook["id"]] = asyncio.get_running_loop().create_task(
                self.notification_hook(hook, payload, session["workspace"]))

    async def notification_hook(self, hook, payload, workspace):
        try:
            outcome = await self.extensions.run_hook(hook, payload, workspace, env=self.jobs.command_environment(named=False))
        except Exception as exc:  # noqa: BLE001 - a notification hook never affects a conversation.
            log.warning("Notification hook %s could not run: %s", hook["id"], type(exc).__name__)
            return
        if outcome["exit_code"] != 0 or outcome.get("timed_out"):
            log.warning("Notification hook %s failed (exit %s).", hook["id"], outcome["exit_code"])

    # Tool calls ------------------------------------------------------------------------------

    def hooked_permission(self, session, tools, name, arguments, forced=None):
        """The tool's Verdict; forced (a before-tool hook's ask, or {sibling}: a running sibling subagent
        changed the file) turns an allow into a card, never the other way. In Don't ask such a call is
        refused: that mode never shows a card."""
        verdict = self.permission(session, tools, name, arguments)
        if not forced or verdict.decision != "allow":
            return verdict
        if "dontAsk" in (session.get("permission_mode"), self.mode_ceiling(session)):
            if forced.get("sibling"):
                shared = forced["sibling"]
                return Verdict("deny", "dont_ask", SIBLING_DONT_ASK.format(
                    subagent=str(shared["subagent_id"])[:8], kind=shared["agent_type"], path=shared["path"]))
            return Verdict("deny", "dont_ask", HOOK_ASK_DONT_ASK.format(hook=forced["hook_id"]))
        return Verdict("ask", "sibling" if forced.get("sibling") else "hook", protected=verdict.protected)

    async def before_tool_hooks(self, session, name, arguments, definition, call_id, *, fixed=False):
        """Run one call's before-tool hooks. Returns {arguments, forced, contexts, refusal, changed_by}.

        A hook's replacement arguments must pass the tool's JSON schema (else the hook fails and the
        call is blocked); fixed calls (Verify's command, the activity log) keep the app's arguments.
        forced is {hook_id, reason} when a hook asked for approval; refusal is the model's text when
        a hook denied the call."""
        limit = 32_000 if name.startswith("mcp__") else 512 * 1024

        def check(parsed, hook):
            if "arguments" not in parsed:
                return parsed
            if fixed:
                parsed = {key: value for key, value in parsed.items() if key != "arguments"}
                parsed["ignored"] = [*parsed.get("ignored", []), "arguments"]
                return parsed
            try:
                validate_arguments(definition["parameters"], parsed["arguments"], limit=limit)
            except ValueError as exc:
                raise ValueError(f"its replacement arguments are not valid for {name}: {exc}") from None
            return parsed

        outcomes = await self.run_hooks(session, "before_tool", name, arguments, call_id=call_id, check=check)
        result = {"arguments": arguments, "forced": None, "contexts": [], "refusal": None, "changed_by": []}
        for outcome in outcomes:
            if "arguments" in outcome and outcome["arguments"] != result["arguments"]:
                result["arguments"] = outcome["arguments"]
                result["changed_by"].append(outcome["hook_id"])
            if outcome.get("context"):
                result["contexts"].append(("before_tool", outcome["hook_id"], outcome["context"]))
            if outcome.get("decision") == "ask" and result["forced"] is None:
                result["forced"] = {"hook_id": outcome["hook_id"], "reason": outcome.get("reason", "")}
            if outcome.get("decision") == "deny":
                result["refusal"] = (f"Blocked by the before-tool hook {outcome['hook_id']}: {outcome['reason']} "
                                     "Do not retry it unchanged or work around it.")
                result["blocked"] = {"hook_id": outcome["hook_id"], "reason": outcome["reason"]}
        if result["arguments"] == arguments:
            result["changed_by"] = []
        return result

    async def add_hook_notes(self, session, notes, output, origin=None):
        """The tool result the model receives, with what hooks added: the changed-arguments note first,
        hook context last. Verify reads its own result, so nothing is added there."""
        if origin == "verify" or notes.event is None or not (notes.changed or notes.contexts):
            return output
        text = (changed_arguments_note(*notes.changed, ran=notes.ran, edited=notes.edited) + "\n"
                if notes.changed else "") + output
        if notes.contexts:
            text += tool_context_text(notes.contexts)
        text = cap_text(self.settings.redact(text))
        await self.update_event(session, notes.event, model_output=text)
        return text

    # Messages and turns ----------------------------------------------------------------------

    def has_hooks(self, session, phase):
        return bool(self.matching_hooks(session, phase))

    async def prompt_hooks(self, session, event, prompt, content=None, *, steered=False):
        """Before a user's message goes to the model: user_prompt hooks, then session_start hooks when
        the chat has made no model request yet. Returns (prompt, content) to send, or None when a hook
        blocked the message (the chat says why; nothing is sent, so nothing is billed). A replacement
        prompt is shown on the message as edited by a hook; context is appended for the model only.
        A steered message stays queued while its hooks run (drain_queue): event is then the fields of
        the user event drain_queue adds afterwards, with the notice of a block. Only a subagent's
        delegated prompt skips these hooks, not the user's own messages in a subagent chat."""
        if (event.get("origin") or {}).get("kind") == "delegated":
            return prompt, content

        async def mark(**values):
            if steered:
                event.update(values)  # Not in the chat yet.
            else:
                await self.update_event(session, event, **values)
        notes = []
        if self.has_hooks(session, "user_prompt"):
            shown, shortened = fit_text(prompt)
            payload = {"session_id": session["id"], "prompt": shown, "prompt_shortened": shortened,
                       "attachments": event.get("attachments") or [], "steered": steered,
                       "workspace": session["workspace"], "model": session["model"],
                       "permission_mode": session.get("permission_mode", "manual"), "title": session.get("title", "")}
            outcomes = await self.run_hooks(session, "user_prompt", payload=payload)
            final, editors = prompt, []
            for outcome in outcomes:
                if "prompt" in outcome and outcome["prompt"] != final:
                    final = outcome["prompt"]
                    editors.append(outcome["hook_id"])
                if outcome.get("context"):
                    notes.append(("user_prompt", outcome["hook_id"], outcome["context"]))
                if outcome.get("decision") == "block":
                    reason = outcome["reason"]
                    await mark(hook_blocked={"hook_id": outcome["hook_id"], "reason": reason})
                    if steered:
                        return None
                    await self.event(session, "notice", text=blocked_prompt_notice(event["hook_blocked"]))
                    return None
            if final != prompt:
                await mark(text=final, hook_edit={"hooks": editors, "original": prompt})
                # The @ files and ! output stay attached; a replacement's own @ tokens attach nothing.
                content = final + content[len(prompt):] if isinstance(content, str) and content.startswith(prompt) else None
                prompt = final
        if not steered and not session.get("wire") and self.has_hooks(session, "session_start"):
            payload = {"session_id": session["id"], "workspace": session["workspace"], "model": session["model"],
                       "permission_mode": session.get("permission_mode", "manual"), "title": session.get("title", "")}
            for outcome in await self.run_hooks(session, "session_start", payload=payload):
                if outcome.get("context"):
                    notes.append(("session_start", outcome["hook_id"], outcome["context"]))
        if notes:
            notes.sort(key=lambda note: note[0] != "session_start")
            await mark(hook_notes=[{"event": kind, "hook_id": hook_id, "context": text} for kind, hook_id, text in notes])
            content = (content if content is not None else prompt) + message_context_text(notes)
        return prompt, content

    def first_message_notes(self, session):
        """(event fields, model note) for a subagent's first message: the context its parent's
        subagent_start hooks returned (subagent_start_hooks), or ({}, "")."""
        notes = self.pending_hook_notes.pop(session["id"], None)
        if not notes:
            return {}, ""
        return ({"hook_notes": [{"event": kind, "hook_id": hook_id, "context": text} for kind, hook_id, text in notes]},
                message_context_text(notes))

    async def subagent_start_hooks(self, parent, child, *, task="", context="", max_steps=None):
        """Before a subagent's first request (planning.DelegateManager.delegate), in the parent chat.
        Context the hooks return is added to the child's first message for its model."""
        if not self.has_hooks(parent, "subagent_start"):
            return
        task_text, _ = fit_text(task)
        context_text, _ = fit_text(context, 16_000)
        payload = {"session_id": parent["id"], "subagent_session_id": child["id"],
                   "agent_type": child.get("agent_type"), "tool_profile": child.get("tool_profile"),
                   "task": task_text, "context": context_text, "max_steps": max_steps,
                   "workspace": parent["workspace"], "model": child.get("model"),
                   "permission_mode": parent.get("permission_mode", "manual")}
        notes = [("subagent_start", outcome["hook_id"], outcome["context"])
                 for outcome in await self.run_hooks(parent, "subagent_start", payload=payload, owner=parent)
                 if outcome.get("context")]
        if notes:
            self.pending_hook_notes[child["id"]] = notes

    def delegated_task(self, child, parent):
        spawn = next((item for item in parent.get("events", []) if item.get("id") == child.get("parent_event_id")), {})
        task = (spawn.get("input") or {}).get("task") if isinstance(spawn.get("input"), dict) else None
        return task if isinstance(task, str) else ""

    async def stop_hooks(self, session, wire, state, has_another_step):
        """When a turn would end with a final reply (after Verify): agent_stop hooks, or subagent_stop
        hooks (in the parent chat) for a subagent's delegated turn. Returns 'continue' when a hook's
        block decision was handed to the model as a hidden continuation, else 'done'. state['blocks']
        counts this turn's continuations: at most STOP_CAPS of them, and only while an agent step is left.
        A subagent chat the user continues (a message, Edit or Retry after its delegation ended) has
        no delegation to report to: its turns are the user's own and run agent_stop in that chat."""
        subagent = bool(session.get("is_subagent") and session.get("turn_delegated"))
        phase = "subagent_stop" if subagent else "agent_stop"
        # The parent waits on this delegation, so it is live, or it went on without it (a background
        # subagent) and is held in memory while it is idle: never a stored copy that a card's answer would
        # save over the parent's newer state.
        parent_id = session.get("parent_session_id")
        owner = (self.live.get(parent_id) or self.delegates.held_session(parent_id)) if subagent else session
        if owner is None or not self.has_hooks(owner, phase):
            session.pop("hook_report", None)
            return "done"
        reply = next((item.get("text") for item in reversed(session["events"])
                      if item.get("type") == "assistant" and item.get("text")), "")
        if subagent and (report := submitted_report(session["events"])) is not None:
            reply = report_text(report)  # Its submit_report report is what the parent receives.
        reply, shortened = fit_text(reply)
        payload = {"session_id": owner["id"], "reply": reply, "reply_shortened": shortened,
                   "continuations": state["blocks"], "workspace": session["workspace"], "model": session["model"],
                   "permission_mode": owner.get("permission_mode", "manual")}
        if subagent:
            task, _ = fit_text(self.delegated_task(session, owner), 16_000)
            payload.update(subagent_session_id=session["id"], agent_type=session.get("agent_type"), task=task)
        outcomes = await self.run_hooks(session, phase, payload=payload, owner=owner)
        if subagent:
            report = next((outcome for outcome in reversed(outcomes) if outcome.get("report")), None)
            if report:
                session["hook_report"] = {"hook_id": report["hook_id"], "text": report["report"]}
            else:
                session.pop("hook_report", None)
        block = next((outcome for outcome in outcomes if outcome.get("decision") == "block"), None)
        if block is None:
            return "done"
        cap = STOP_CAPS[phase]
        who = "the subagent" if subagent else "the agent"
        reason = clip(" ".join(block["reason"].split()), 400)
        if state["blocks"] >= cap:
            await self.event(session, "notice", text=(
                f"A hook asked {who} to continue again: {reason} The turn ended anyway: hooks can continue one turn "
                f"at most {cap} times in a row."))
            return "done"
        if not has_another_step:
            await self.event(session, "notice", text=(
                f"A hook asked {who} to continue: {reason} No agent steps are left in this turn, so it ended."))
            return "done"
        state["blocks"] += 1
        wire.append({"role": "user", "content": stop_continuation(phase, block, state["blocks"], cap)})
        self.store.save(session)
        await self.event(session, "notice", text=f"A hook asked {who} to continue: {reason} ({state['blocks']} of {cap})")
        return "continue"

    @staticmethod
    def hook_report(child, reply):
        """The report a delegate_task result carries: a subagent_stop hook's replacement, labelled, or reply."""
        report = child.get("hook_report")
        if not isinstance(report, dict) or not isinstance(report.get("text"), str) or not report["text"].strip():
            return reply
        return (f"[Report replaced by the subagent_stop hook {report.get('hook_id')} (a local command); the "
                f"subagent's own final reply is in its transcript.]\n{report['text']}")
