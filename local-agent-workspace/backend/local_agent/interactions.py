"""Tool calls that wait for the user inside a turn: ask_user questions and submit_plan reviews.

They wait in their own registry, not in AgentManager.pending, so approval routes and
approval re-checks never resolve them. The wait uses the Approval wait setting (a scheduled
run's approval limit in its turn: AgentManager.approval_seconds) and the 'awaiting_approval'
status, so notifications, sidebar badges and subagent progress work as for approval cards.
mode_changed closes a waiting plan when the chat leaves Plan mode and skips waiting
questions when it switches to Don't ask.
"""
import asyncio
import json
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .config import DEFAULT_APPROVAL_MINUTES

MODE_LABELS = {"manual": "Manual", "auto": "Auto", "acceptEdits": "Accept edits", "plan": "Plan",
               "bypassPermissions": "Bypass permissions", "dontAsk": "Don't ask"}
MINUTE = 60  # Seconds in a minute of an approval wait (tests shorten it).
MAX_OTHER = 1000
MAX_FEEDBACK = 2000
DONT_ASK = "Nobody can answer questions in Don't ask mode. Continue with your best judgment and state your assumptions."
SKIPPED = "The user chose not to answer. Continue with your best judgment and state your assumptions."
QUESTIONS_EXPIRED = ("The user did not answer in time. Continue with your best judgment and state your assumptions, "
                     "or ask again later.")
PLAN_EXPIRED = "The user did not review the plan in time. Stay in Plan mode and ask the user to review it."
QUESTION_GONE = "This question is no longer waiting for an answer."
OTHER_LABEL = "other"
PLAN_GONE = "This plan is no longer waiting for review."


class InteractionError(ValueError):
    """A refused ask_user or submit_plan call; the model gets its kind and hint."""

    def __init__(self, message, error_kind, hint=None):
        super().__init__(message)
        self.error_kind, self.hint = error_kind, hint


def validate_questions(questions):
    """Stripped questions; ValueError for an empty text, a repeated question or repeated option labels.

    The schema bounds (1-4 questions, 2-4 options, lengths) are checked by validate_arguments.
    """
    result, seen = [], set()
    for item in questions:
        text = item["question"].strip()
        options = [{"label": option["label"].strip(), "description": str(option.get("description") or "").strip()}
                   for option in item["options"]]
        labels = [option["label"].casefold() for option in options]
        if not text or text.casefold() in seen or not all(labels) or len(set(labels)) != len(labels):
            raise InteractionError("Each question needs a text and 2-4 distinct options.", "invalid_arguments",
                                   "Give every question a text and 2-4 options with different labels, then ask again.")
        if OTHER_LABEL in labels:
            raise InteractionError("Leave out the Other option; the card always offers one.", "invalid_arguments",
                                   "Leave out Other; the user can always write their own answer.")
        seen.add(text.casefold())
        result.append({"question": text, "header": str(item.get("header") or "").strip(), "options": options,
                       "multi_select": item.get("multi_select") is True})
    return result


def later_calls(session, call_id):
    """The names of the tool calls that follow call_id in the same model response."""
    for message in reversed(session.get("wire") or []):
        calls = message.get("tool_calls") if message.get("role") == "assistant" else None
        ids = [item.get("id") for item in calls or []]
        if call_id and call_id in ids:
            return [item["function"]["name"] for item in calls[ids.index(call_id) + 1:]]
    return []


def require_last_call(session, event, name):
    """Refuse a card when other tool calls follow it in the same response.

    The tool loop runs a response's calls in order, so a later call would run before the
    model read the answer, and after an approved plan under the new mode.
    """
    if later_calls(session, event.get("call_id")):
        raise InteractionError(
            f"{name} must be the last tool call in its response: calls after it would run before you read the "
            "user's reply. Nothing was shown to the user.", "call_order",
            f"Call {name} again, after the other calls or in a response of its own.")


def wait_seconds(settings, minutes=None):
    """The Approval wait (or these minutes: a scheduled run's approval limit) in seconds, or None (no limit), exactly
    as approval cards use it."""
    minutes = settings.values.get("approval_timeout_minutes", DEFAULT_APPROVAL_MINUTES) if minutes is None else minutes
    return minutes * MINUTE if type(minutes) is int and minutes > 0 else None


class InteractionManager:
    def __init__(self, manager):
        self.manager = manager
        # (session_id, event_id) -> {future, kind: 'question'|'plan', session, questions?}
        self.waiting = {}

    async def handle(self, session, event, name, arguments):
        if name == "ask_user":
            return await self.ask(session, event, arguments)
        return await self.review_plan(session, event, arguments)

    async def wait(self, session, event, kind, **extra):
        """The user's answer (a dict), or None when the wait expired (at once in a scheduled run that already
        reached its approval limit). Stop cancels the wait."""
        run = self.manager.scheduled_run(session)
        if run is not None and run.get("expired"):
            return None
        future = asyncio.get_running_loop().create_future()
        key = (session["id"], event["id"])
        self.waiting[key] = {"future": future, "kind": kind, "session": session, **extra}
        seconds = self.manager.approval_seconds(session)
        try:
            await self.manager.update_event(session, event, state="pending",
                                            **({"approval_expires": time.time() + seconds} if seconds else {}))
            await self.manager.status(session, "awaiting_approval")
            try:
                return await asyncio.wait_for(future, timeout=seconds)
            except TimeoutError:
                self.manager.approval_expired(session)
                return None
        finally:
            self.waiting.pop(key, None)

    async def ask(self, session, event, arguments):
        questions = validate_questions(arguments["questions"])
        if session.get("permission_mode") == "dontAsk":
            await self.manager.update_event(session, event, question_outcome="skipped", skipped_reason="dont_ask")
            return DONT_ASK
        require_last_call(session, event, "ask_user")
        answer = await self.wait(session, event, "question", questions=questions)
        if answer is None:
            await self.manager.update_event(session, event, state="completed", question_outcome="expired")
            result = QUESTIONS_EXPIRED
        elif answer.get("skip"):
            reason = {"skipped_reason": "dont_ask"} if answer.get("dont_ask") else {}
            await self.manager.update_event(session, event, state="completed", question_outcome="skipped", **reason)
            result = DONT_ASK if answer.get("dont_ask") else SKIPPED
        else:
            await self.manager.update_event(session, event, state="completed", question_outcome="answered",
                                            answers=answer["answers"])
            result = json.dumps({"answers": answer["answers"]}, indent=2, ensure_ascii=False)
        await self.manager.status(session, "running")
        return result

    async def review_plan(self, session, event, arguments):
        if session.get("permission_mode", "manual") != "plan":
            raise InteractionError("submit_plan is only available in Plan mode. Describe the plan in your reply instead.",
                                   "unavailable_tool")
        if session.get("is_subagent"):
            raise InteractionError("Subagents cannot submit plans; return the plan as your final reply.", "unavailable_tool")
        require_last_call(session, event, "submit_plan")
        answer = await self.wait(session, event, "plan")
        update = self.manager.update_event
        current = session.get("permission_mode", "manual")
        if answer is not None and not answer.get("superseded") and answer.get("choice") != "keep_planning" and current != "plan":
            # The mode changed from the menu after the answer, before this task resumed: the newer choice wins.
            answer = {"superseded": current}
        if answer is None:
            await update(session, event, state="completed", plan_outcome="expired")
            result = PLAN_EXPIRED
        elif answer.get("superseded"):
            await update(session, event, state="completed", plan_outcome="superseded")
            result = (f"The user switched this chat to {MODE_LABELS.get(answer['superseded'], answer['superseded'])} mode "
                      "from the menu instead of reviewing the plan, so it was not approved. "
                      "Ask the user before implementing it.")
        else:
            feedback = answer.get("feedback")
            note = {"feedback": feedback} if feedback else {}
            if answer["choice"] == "keep_planning":
                await update(session, event, state="completed", plan_outcome="keep_planning", **note)
                result = (f"The user wants to keep planning and did not approve this plan. Their feedback: "
                          f"{feedback or 'none'}. Revise the plan; do not make changes.")
            else:
                mode = answer["choice"]
                label = MODE_LABELS[mode]
                session["permission_mode"] = mode
                self.manager.store.save(session)
                await self.manager.publish_permissions(session)
                await update(session, event, state="completed", plan_outcome="approved", plan_mode=mode, **note)
                await self.manager.event(session, "notice", text=f"Plan approved. Permission mode: {label}.")
                result = (f"The user approved the plan and switched this chat to {label} mode. "
                          "Implement the plan now, step by step." + (f" Their note: {feedback}" if feedback else ""))
        await self.manager.status(session, "running")
        return result

    async def mode_changed(self, session, old, new):
        """Close cards that the new mode makes moot: a plan when the chat leaves Plan, questions in Don't ask."""
        for (session_id, _), entry in list(self.waiting.items()):
            if session_id != session["id"] or entry["future"].done():
                continue
            if entry["kind"] == "plan" and new != "plan":
                entry["future"].set_result({"superseded": new})
            elif entry["kind"] == "question" and new == "dontAsk":
                entry["future"].set_result({"skip": True, "dont_ask": True})

    def _entry(self, session_id, event_id, kind, gone):
        entry = self.waiting.get((session_id, event_id))
        if not entry or entry["kind"] != kind or entry["future"].done():
            raise ValueError(gone)
        return entry

    def answer_questions(self, session_id, event_id, body):
        entry = self._entry(session_id, event_id, "question", QUESTION_GONE)
        answers, skip = body.get("answers"), body.get("skip") is True
        if skip:
            if answers:
                raise ValueError("Send answers or skip the questions, not both.")
            entry["future"].set_result({"skip": True})
            return
        questions = entry["questions"]
        if not isinstance(answers, list) or len(answers) != len(questions):
            raise ValueError(f"Answer each of the {len(questions)} questions, or skip them.")
        result = []
        for question, answer in zip(questions, answers):
            labels = [option["label"] for option in question["options"]]
            selected = [str(label).strip() for label in answer.get("selected") or []]
            other = str(answer.get("other") or "").strip() or None
            if any(label not in labels for label in selected):
                raise ValueError(f"Choose only the listed options for: {question['question']}")
            if len(set(selected)) != len(selected):
                raise ValueError(f"Choose each option once for: {question['question']}")
            if other is not None and len(other) > MAX_OTHER:
                raise ValueError(f"An Other answer can have at most {MAX_OTHER:,} characters.")
            if question["multi_select"]:
                if not selected and other is None:
                    raise ValueError(f"Choose at least one option or write your own answer for: {question['question']}")
            elif len(selected) + (other is not None) != 1:
                raise ValueError(f"Choose one option or write your own answer for: {question['question']}")
            result.append({"question": question["question"], "selected": selected,
                           "other": self.manager.settings.redact(other) if other is not None else None})
        entry["future"].set_result({"answers": result})

    def answer_plan(self, session_id, event_id, body):
        entry = self._entry(session_id, event_id, "plan", PLAN_GONE)
        if entry["session"].get("permission_mode", "manual") != "plan":
            raise ValueError(PLAN_GONE)
        choice = body.get("choice")
        if choice not in ("acceptEdits", "manual", "keep_planning"):
            raise ValueError("Choose Accept edits, Manual or Keep planning.")
        feedback = str(body.get("feedback") or "").strip()
        if len(feedback) > MAX_FEEDBACK:
            raise ValueError(f"Feedback can have at most {MAX_FEEDBACK:,} characters.")
        entry["future"].set_result({"choice": choice, "feedback": self.manager.settings.redact(feedback) or None})


class AnswerBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    selected: list[str] = Field(default_factory=list, max_length=4)
    other: str | None = Field(default=None, max_length=4 * MAX_OTHER)  # The stripped limit is checked with a 400.


class QuestionsBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    answers: list[AnswerBody] | None = Field(default=None, max_length=4)
    skip: bool = False


class PlanBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    choice: Literal["acceptEdits", "manual", "keep_planning"]
    feedback: str | None = Field(default=None, max_length=4 * MAX_FEEDBACK)


def register_interactions(app, manager, session_or_404):
    @app.post("/api/sessions/{session_id}/questions/{event_id}")
    async def answer_questions(session_id: str, event_id: str, body: QuestionsBody):
        session_or_404(session_id)
        manager.interactions.answer_questions(session_id, event_id, body.model_dump())
        return {"ok": True}

    @app.post("/api/sessions/{session_id}/plans/{event_id}")
    async def answer_plan(session_id: str, event_id: str, body: PlanBody):
        session_or_404(session_id)
        manager.interactions.answer_plan(session_id, event_id, body.model_dump())
        return {"ok": True}
