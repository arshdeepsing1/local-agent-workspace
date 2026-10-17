"""Scheduled runs (Agent tools → Schedules): a prompt sent to a new chat of a project at set times.

Schedules are kept in the state folder (schedules.json, 0600, atomic writes). Each one has a fixed permission
mode (never Bypass permissions), a per-run approval limit (a card that waits that long ends the run with
terminal_reason 'approval_expired': AgentManager.approve), a token limit and a time limit per run (turns.py) and a
spending limit (spending.py; 0 follows the Settings default), which the new chat gets as its own. Its run policy is 'ask' by default: each run
waits for Run (or Skip) in the app; 'auto' starts runs without a click, as an explicit choice per schedule.

The scheduler is an asyncio task the app starts when Settings → Agent → Scheduled runs is on (schedules_enabled,
off by default) and stops at shutdown. It ticks every TICK_SECONDS and holds a lock file in the state folder, so
two app instances on one state folder never both fire. A run first makes the free connection check (the endpoint
listing of serving.endpoints_url, no model call): a rejected token (HTTP 401 or 403) pauses the schedule and says so.
At most one run per schedule and MAX_RUNNING scheduled runs run at a time; a run that would pass that is skipped
with a note in the schedule's history. Each run checks the saved schedule again (schedule_problem): one changed
outside the app is paused instead of run. Runs missed while the app was not running (or asleep, or with scheduled
runs off) are caught up once, not once per missed time, when the latest of them was due within MAX_AGE; older
ones are skipped. Either way the history says how many were missed.

Times: 'every N minutes|hours|days' counts from when the schedule was saved; a five-field cron line (minute,
hour, day of month, month, day of week with 0 or 7 for Sunday; *, lists, ranges and steps) is read in the
schedule's time zone (this computer's unless one is named). A local time that a clock change skips runs an hour
later that day, and a local time that repeats runs once. A schedule runs at most every MIN_INTERVAL_MINUTES.
"""
import asyncio
import json
import logging
import math
import os
import re
import tempfile
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from . import serving
from .config import ENDPOINT_NAME, MAX_MAX_AGENT_STEPS, MIN_MAX_AGENT_STEPS, MAX_TURN_MINUTES, MAX_TURN_TOKENS
from .notify import short_title
from .permissions import MODE_LABELS
from .reasoning import EFFORT_LEVELS
from .spending import validate_limit
from .user_paths import checked_workspace

log = logging.getLogger(__name__)

VERSION = 1
TICK_SECONDS = 30
MAX_AGE = 24 * 3600  # A missed run older than this is skipped; a run waiting for Run expires after it.
LATE_SECONDS = 300  # A due time this much older than the tick counts as missed (the app was closed or asleep).
MAX_RUNNING = 2  # Scheduled runs at a time, over all schedules.
MAX_SCHEDULES = 50
HISTORY = 20  # Runs a schedule keeps in its history.
MIN_INTERVAL_MINUTES = 5
MAX_INTERVAL_MINUTES = 31 * 24 * 60
MAX_APPROVAL_MINUTES = 60
MAX_NAME = 80
MAX_PROMPT = 20_000
MODES = ("manual", "auto", "acceptEdits", "plan", "dontAsk")  # Never bypassPermissions.
POLICIES = ("ask", "auto")
CATCH_UP = ("once", "skip")
TAG = "scheduled"
SCAN_DAYS = 8 * 366 + 1  # A cron line's next time is searched this far ahead (29 February included).
MISSED_SCAN = 1_000  # Missed cron times counted at most.
ZONE_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_+\-]*(?:/[A-Za-z0-9_+\-]+){0,2}\Z")
INTERVAL = re.compile(r"every\s+(?:(\d{1,5})\s+)?(minute|minutes|hour|hours|day|days)\Z", re.I)
CRON_PART = re.compile(r"(\*|\d{1,2}(?:-\d{1,2})?)(?:/(\d{1,2}))?\Z")
CRON_FIELDS = (("minute", 0, 59), ("hour", 0, 23), ("day of month", 1, 31), ("month", 1, 12), ("day of week", 0, 7))
UNIT_MINUTES = {"minute": 1, "hour": 60, "day": 24 * 60}
clock = time.time  # Patched by tests (a fake clock).


# Times

def local_zone():
    """This computer's time zone: its IANA zone (TZ, or the /etc/localtime link) so clock changes are known,
    else the current fixed offset."""
    names = [os.environ.get("TZ", "").lstrip(":")]
    try:
        link = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in link:
            names.append(link.split("zoneinfo/", 1)[1])
    except OSError:
        pass
    for name in names:
        if name and ZONE_NAME.match(name):
            try:
                return ZoneInfo(name)
            except (ZoneInfoNotFoundError, ValueError, OSError):
                continue
    return datetime.now().astimezone().tzinfo


def zone_of(name):
    """The zone a schedule names ('' = this computer's), or ValueError."""
    if not name:
        return local_zone()
    if not isinstance(name, str) or not ZONE_NAME.match(name):
        raise ValueError("Time zone must be empty (this computer's) or a name such as Europe/Berlin or America/New_York.")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ValueError(f"Unknown time zone {name}. Use a name such as Europe/Berlin or America/New_York.") from None


def zone_label(zone):
    return getattr(zone, "key", None) or datetime.now(zone).strftime("UTC%z")


class Interval:
    """'every N minutes|hours|days': times anchor + k × the interval (k ≥ 1)."""

    def __init__(self, minutes, text):
        self.seconds, self.text = minutes * 60, text

    def next_after(self, after, anchor, zone=None):
        if after < anchor:
            return anchor + self.seconds
        return anchor + (math.floor((after - anchor) / self.seconds) + 1) * self.seconds

    def due_times(self, first, now, anchor, zone=None):
        """(how many times from first to now, the latest of them); first is one of its times."""
        count = math.floor((now - first) / self.seconds) + 1
        return count, first + (count - 1) * self.seconds


class Cron:
    """A five-field cron line. When both day fields are restricted, a day matches either (as cron does)."""

    def __init__(self, sets, stars, text):
        self.minutes, self.hours, self.days, self.months, self.weekdays = sets
        self.day_star, self.weekday_star = stars
        self.text = text

    def day_matches(self, day):
        if day.month not in self.months:
            return False
        by_date, by_weekday = day.day in self.days, (day.weekday() + 1) % 7 in self.weekdays
        if self.day_star or self.weekday_star:
            return by_date and by_weekday
        return by_date or by_weekday

    def next_after(self, after, anchor=None, zone=None):
        """The first time after `after` (a timestamp) in zone, or None. Each local time is read with fold 0: a time
        a clock change skips maps an hour later, and the second of two equal local times is never chosen."""
        start = datetime.fromtimestamp(after, zone)
        day = start.date()
        for _ in range(SCAN_DAYS):
            if self.day_matches(day):
                # On the first day, hours well before `after` cannot follow it (2 hours covers any clock change).
                floor = start.hour - 2 if day == start.date() else -1
                stamps = [datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone).timestamp()
                          for hour in self.hours if hour >= floor for minute in self.minutes]
                later = [stamp for stamp in stamps if stamp > after]
                if later:
                    return min(later)
            day += timedelta(days=1)
        return None

    def due_times(self, first, now, anchor=None, zone=None):
        count, latest = 1, first
        while count < MISSED_SCAN:
            following = self.next_after(latest, anchor, zone)
            if following is None or following > now:
                break
            count, latest = count + 1, following
        return count, latest


def cron_field(text, name, low, high):
    """(the values a cron field matches, whether it starts with *), or ValueError."""
    values = set()
    for part in text.split(","):
        match = CRON_PART.match(part)
        if not match:
            raise ValueError(f"The {name} field '{text}' is not a number, a range, a list or *, with an optional /step.")
        base, step = match.group(1), int(match.group(2) or 1)
        if base == "*":
            start, end = low, high
        else:
            start, _, end = base.partition("-")
            start = int(start)
            end = int(end) if end else (high if match.group(2) else start)
        if not low <= start <= end <= high or not 1 <= step <= high:
            raise ValueError(f"The {name} field '{text}' must stay within {low}-{high}.")
        values.update(range(start, end + 1, step))
    if name == "day of week" and 7 in values:
        values = (values - {7}) | {0}
    return frozenset(values), text.startswith("*")


def parse_spec(spec):
    """An Interval or a Cron for a schedule's text, or ValueError with what is wrong."""
    text = " ".join(str(spec or "").split())
    if not text:
        raise ValueError("Enter when it runs: every N minutes, hours or days, or a cron line such as 0 9 * * 1-5.")
    match = INTERVAL.match(text)
    if match:
        count, unit = int(match.group(1) or 1), match.group(2).lower().rstrip("s")
        minutes = count * UNIT_MINUTES[unit]
        if not MIN_INTERVAL_MINUTES <= minutes <= MAX_INTERVAL_MINUTES:
            raise ValueError(f"A schedule runs at most every {MIN_INTERVAL_MINUTES} minutes and at least every 31 days.")
        return Interval(minutes, f"every {unit}" if count == 1 else f"every {count} {unit}s")
    parts = text.split(" ")
    if len(parts) != 5:
        raise ValueError("Write every N minutes, hours or days, or a cron line of five fields (minute hour day month "
                         "weekday), such as 0 9 * * 1-5 for 9:00 on weekdays.")
    fields = [cron_field(part, name, low, high) for part, (name, low, high) in zip(parts, CRON_FIELDS)]
    minutes = sorted(fields[0][0])
    gaps = [later - earlier for earlier, later in zip(minutes, minutes[1:])] + [60 - minutes[-1] + minutes[0]]
    if len(minutes) > 1 and min(gaps) < MIN_INTERVAL_MINUTES:
        raise ValueError(f"A schedule runs at most every {MIN_INTERVAL_MINUTES} minutes: leave at least "
                         f"{MIN_INTERVAL_MINUTES} minutes between the minutes it names.")
    return Cron([values for values, _ in fields], (fields[2][1], fields[4][1]), text)


def next_fire(schedule, after):
    """The schedule's first time after `after`, or None (a cron line no date matches, or a damaged schedule)."""
    try:
        spec, zone = parse_spec(schedule.get("spec")), zone_of(schedule.get("timezone") or "")
    except ValueError:
        return None
    anchor = schedule.get("anchor") if isinstance(schedule.get("anchor"), (int, float)) else after
    return spec.next_after(after, anchor, zone)


def preview(spec, zone_name="", now=None, count=3, anchor=None):
    """{text, zone, next_runs} of a schedule text before it is saved, or ValueError. anchor: the saved schedule's,
    when an edit keeps its timing (an interval then still counts from when it was saved), else now."""
    now = clock() if now is None else now
    parsed, zone = parse_spec(spec), zone_of(zone_name)
    runs, after = [], now
    for _ in range(count):
        after = parsed.next_after(after, now if anchor is None else anchor, zone)
        if after is None:
            break
        runs.append(after)
    if not runs:
        raise ValueError("No date matches this cron line (such as 30 February).")
    return {"text": parsed.text, "zone": zone_label(zone), "next_runs": runs}


def missed_text(count):
    many = f"{MISSED_SCAN:,} or more" if count >= MISSED_SCAN else f"{count:,}"
    return f"{many} run{'' if count == 1 else 's'} missed while the app was not running"


# The PAT warning (Settings → Credentials → Token expires on)

def token_warning(value, today=None):
    """The warning for a Databricks token that expires within 7 days or has expired, else None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        expires = date.fromisoformat(value)
    except ValueError:
        return None
    days = (expires - (today or date.today())).days
    renew = ("Create a new token in Databricks, put it in your credential file and update the date in Settings → "
             "Credentials.")
    if days < 0:
        return f"Your Databricks token expired on {value}. Chats and scheduled runs fail until you renew it. {renew}"
    if days > 7:
        return None
    when = "today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days"
    return (f"Your Databricks token expires {when} ({value}). Scheduled runs stop working when it expires. "
            f"{renew}")


# The schedules file

class ScheduleStore:
    def __init__(self, state_dir):
        self.path = Path(state_dir) / "schedules.json"

    def read(self):
        """The saved schedules ([] without a file); ValueError when the file cannot be read, which is then never
        overwritten."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, UnicodeError, ValueError) as exc:
            raise ValueError(f"The schedules file {self.path.name} in the state folder could not be read "
                             f"({type(exc).__name__}). Fix or remove it to use schedules.") from None
        items = data.get("schedules") if isinstance(data, dict) else None
        if not isinstance(items, list) or not all(isinstance(item, dict) and isinstance(item.get("id"), str)
                                                  for item in items):
            raise ValueError(f"The schedules file {self.path.name} in the state folder is not a list of schedules. "
                             "Fix or remove it to use schedules.")
        return items

    def write(self, items):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, filename = tempfile.mkstemp(prefix="schedules-", suffix=".tmp", dir=self.path.parent)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as target:
                json.dump({"version": VERSION, "schedules": items}, target, indent=1, ensure_ascii=False)
                target.flush()
                os.fsync(target.fileno())
            os.replace(filename, self.path)
        finally:
            Path(filename).unlink(missing_ok=True)

    def get(self, schedule_id):
        return next((item for item in self.read() if item["id"] == schedule_id), None)

    def update(self, schedule_id, change):
        """Apply change(schedule) to the saved schedule and save; the schedule, or None when it is gone."""
        items = self.read()
        item = next((entry for entry in items if entry["id"] == schedule_id), None)
        if item is None:
            return None
        change(item)
        self.write(items)
        return item


def check_schedule(values):
    """The editable fields of a schedule, checked and tidied, or ValueError with what to change."""
    name = " ".join(str(values.get("name") or "").split())
    if not 1 <= len(name) <= MAX_NAME or any(ord(char) < 32 or ord(char) == 127 for char in name):
        raise ValueError(f"Name the schedule (1 to {MAX_NAME} characters, no control characters).")
    prompt = values.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_PROMPT or "\x00" in prompt:
        raise ValueError(f"Enter the prompt each run sends (at most {MAX_PROMPT:,} characters).")
    spec = parse_spec(values.get("spec"))
    zone_name = (values.get("timezone") or "").strip()
    zone_of(zone_name)
    model = (values.get("model") or "").strip()
    if model and not ENDPOINT_NAME.match(model):
        raise ValueError("The model must be empty (the Settings model) or an endpoint name of letters, digits, '.', "
                         "'_' and '-'.")
    effort = values.get("reasoning_effort") or ""
    if effort and effort not in EFFORT_LEVELS:
        raise ValueError("Reasoning effort must be empty (the Settings default) or one of: " + ", ".join(EFFORT_LEVELS) + ".")
    mode = values.get("permission_mode") or "dontAsk"
    if mode == "bypassPermissions":
        raise ValueError("Scheduled runs never use Bypass permissions: choose Manual, Auto, Accept edits, Plan or "
                         "Don't ask.")
    if mode not in MODES:
        raise ValueError("Permission mode must be Manual, Auto, Accept edits, Plan or Don't ask.")
    checks = (("approval_timeout_minutes", "an approval limit", "minutes", 1, MAX_APPROVAL_MINUTES),
              ("turn_token_limit", "a token limit per run", "tokens", 1, MAX_TURN_TOKENS),
              ("turn_time_limit_minutes", "a time limit per run", "minutes", 1, MAX_TURN_MINUTES))
    limits = {}
    for key, label, unit, low, high in checks:
        value = values.get(key)
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"Scheduled runs need {label}: a whole number of {unit} from {low} to {high:,}.")
        limits[key] = value
    spending = values.get("spending_limit_dbu", 0)
    spending = validate_limit(spending if spending is not None else 0, "Spending limit per run")
    steps = values.get("max_agent_steps")
    if steps is not None and (type(steps) is not int or not MIN_MAX_AGENT_STEPS <= steps <= MAX_MAX_AGENT_STEPS):
        raise ValueError(f"Agent steps must be empty (the Settings value) or {MIN_MAX_AGENT_STEPS} to {MAX_MAX_AGENT_STEPS}.")
    policy = values.get("run_policy") or "ask"
    if policy not in POLICIES:
        raise ValueError("Run policy must be ask (each run waits for Run) or auto.")
    catch_up = values.get("catch_up") or "once"
    if catch_up not in CATCH_UP:
        raise ValueError("Missed runs must be once (caught up once) or skip.")
    return {"name": name, "prompt": prompt, "spec": spec.text, "timezone": zone_name, "model": model,
            "reasoning_effort": effort, "permission_mode": mode, **limits, "spending_limit_dbu": spending,
            "max_agent_steps": steps, "run_policy": policy, "catch_up": catch_up}


def schedule_problem(item):
    """Why a saved schedule cannot run, or None. schedules.json is checked again at each run, as it may have been
    changed outside the app: a run never uses Bypass permissions or goes without its caps and approval limit."""
    try:
        check_schedule(item)
    except ValueError as exc:
        return f"The saved schedule is not valid: {exc} Edit and save it, then resume it if it is paused."
    return None


def open_entry(entry):
    """Whether a history entry is a started run whose end is not recorded yet."""
    return isinstance(entry, dict) and entry.get("outcome") == "started" and "ended" not in entry


async def connection_status(manager):
    """('ok' | 'rejected' | 'unavailable', why) from the free connection check (the serving endpoints listing)."""
    redact = manager.settings.redact
    try:
        host, token = manager.serving.credentials()
    except (ValueError, OSError) as exc:
        return "unavailable", redact(str(exc)) or "The credential file could not be read."
    try:
        async with manager.serving.client(serving.LISTING_TIMEOUT) as client:
            response = await client.get(serving.endpoints_url(host), headers=serving.request_headers(token))
    except httpx.HTTPError as exc:
        return "unavailable", f"Databricks could not be reached ({type(exc).__name__})."
    except ValueError:
        return "unavailable", "The Databricks host or token in your credential file cannot be sent in a request."
    status = response.status_code
    if status in (401, 403):
        return "rejected", (f"Databricks rejected the token (HTTP {status}). Renew it in your credential file, then "
                            "resume the schedule.")
    if status != 200:
        return "unavailable", f"Databricks answered the connection check with HTTP {status}."
    return "ok", None


class Scheduler:
    """The schedules of one app: the loop, the lock file and the runs it starts (manager.scheduled_turns marks
    each run's turn for AgentManager.approve)."""

    def __init__(self, manager, settings):
        self.manager, self.settings = manager, settings
        self.store = ScheduleStore(settings.state_dir)
        self.task = None
        self.lock = None  # The open lock file while this app runs the schedules.
        self.lock_problem = None
        self.launching = set()  # Schedules whose run is being started (the connection check runs).
        self.runs = {}  # {chat id: schedule id} of the runs this app started.

    @property
    def running(self):
        return self.task is not None and not self.task.done()

    def enabled(self):
        return self.settings.values.get("schedules_enabled") is True

    async def sync(self):
        """Start or stop the loop as Settings → Scheduled runs says (at startup and after a Settings save)."""
        if self.enabled() and not self.running:
            self.start()
        elif not self.enabled() and self.running:
            await self.stop()

    def start(self):
        if self.running or not self.acquire():
            return self.running
        self.task = asyncio.create_task(self.loop())
        return True

    async def stop(self):
        task, self.task = self.task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self.task is None:  # A start() while the old loop ended runs a new loop, which keeps the lock.
            self.release()

    def acquire(self):
        """Hold the state folder's lock file; False (and lock_problem says why) when another app holds it."""
        if self.lock is not None:
            return True
        path = self.settings.state_dir / "scheduler.lock"
        try:
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except OSError as exc:
            self.lock_problem = f"The scheduler lock file could not be opened ({type(exc).__name__})."
            return False
        try:
            import fcntl
        except ImportError:  # No advisory locks on this system: one app per state folder is assumed.
            fcntl = None
        if fcntl is not None:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(descriptor)
                self.lock_problem = ("Another app instance using this state folder runs its schedules. Close it, "
                                     "or turn Scheduled runs off there.")
                return False
        self.lock, self.lock_problem = descriptor, None
        return True

    def release(self):
        if self.lock is not None:
            try:
                os.close(self.lock)  # Closing the file releases its lock.
            except OSError:
                pass
            self.lock = None

    async def loop(self):
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad tick must not end the scheduler.
                log.exception("Scheduled runs: a check failed.")
            await asyncio.sleep(TICK_SECONDS)

    def notify(self, text):
        """A desktop notification (Settings → Notifications, no page open); an open page shows the banner."""
        page_open = bool(self.manager.pages or any(self.manager.listeners.values()))
        if self.settings.values.get("system_notifications") is True and not page_open:
            self.manager.notifier.send(text)

    def label(self, item):
        return short_title(self.manager.redact_title(str(item.get("name") or "")))

    async def tick(self, now=None):
        """Fire what is due at now (a timestamp; the clock by default): an 'auto' schedule starts its run, an 'ask'
        one waits for Run. Missed times are counted and caught up once."""
        now = clock() if now is None else now
        try:
            items = self.store.read()
        except ValueError:
            return  # GET /api/schedules reports it.
        launches, ready, changed = [], [], False
        for item in items:
            changed |= self.reconcile(item, now)
            if item.get("enabled") is not True:
                continue
            pending = item.get("pending")
            if isinstance(pending, dict) and now - (pending.get("created") or now) > MAX_AGE:
                self.add_history(item, {**self.entry(pending.get("due"), pending.get("trigger"), now),
                                        "outcome": "not_run", "note": "Not run: nobody chose Run within 24 hours."})
                item.pop("pending")
                changed = True
            due = item.get("next_run")
            if isinstance(due, bool) or not isinstance(due, (int, float)) or due > now:
                continue
            changed = True
            problem = schedule_problem(item)
            if problem:
                self.add_history(item, {**self.entry(due, "scheduled", now), "outcome": "not_run",
                                        "note": f"Not run: {problem}"})
                item.update(enabled=False, paused_reason="invalid", paused_note=problem, next_run=None)
                item.pop("pending", None)
                continue
            spec, zone = parse_spec(item.get("spec")), zone_of(item.get("timezone") or "")
            count, latest = spec.due_times(due, now, item.get("anchor", due), zone)
            item["next_run"] = spec.next_after(now, item.get("anchor", now), zone)
            missed = count > 1 or now - latest > LATE_SECONDS
            if missed and (item.get("catch_up") == "skip" or now - latest > MAX_AGE):
                why = "it was due more than 24 hours ago" if item.get("catch_up") != "skip" else "missed runs are skipped"
                self.add_history(item, {**self.entry(latest, "catch_up", now), "outcome": "skipped",
                                        "note": f"Not run: {missed_text(count)}, and {why}."})
                continue
            note = f"Caught up once: {missed_text(count)}." if missed else None
            trigger = "catch_up" if missed else "scheduled"
            if item.get("run_policy") == "auto":
                launches.append((item["id"], trigger, latest, note))
                continue
            if isinstance(item.get("pending"), dict):
                old = item["pending"]
                self.add_history(item, {**self.entry(old.get("due"), old.get("trigger"), now), "outcome": "not_run",
                                        "note": "Not run: nobody chose Run before the next scheduled time."})
            item["pending"] = {"due": latest, "created": now, "trigger": trigger, **({"note": note} if note else {})}
            ready.append(item)
        if changed:
            self.store.write(items)
        for item in ready:
            self.notify(f"Scheduled run ready: {self.label(item)}")
        # next_run is already past these runs, so each one that does not start gets a note: one that fails does not
        # keep the others from starting, and a stop of the scheduler (the app closing, Scheduled runs turned off)
        # during a connection check notes it and the runs after it.
        for index, (schedule_id, trigger, due, note) in enumerate(launches):
            try:
                await self.launch(schedule_id, trigger, due, note)
            except asyncio.CancelledError:
                for left_id, left_trigger, left_due, _ in launches[index:]:
                    self.record(left_id, {**self.entry(left_due, left_trigger, clock()), "outcome": "not_run",
                                          "note": "Not run: the scheduler stopped before the run started (the app "
                                                  "closed or Scheduled runs were turned off)."})
                raise
            except Exception:  # noqa: BLE001 - the other due runs still start.
                log.exception("Scheduled runs: a run could not be started.")
                self.record(schedule_id, {**self.entry(due, trigger, clock()), "outcome": "not_run",
                                          "note": "Not run: it could not be started because of an unexpected error "
                                                  "(the app's log says more)."})

    @staticmethod
    def entry(due, trigger, now):
        return {"id": uuid.uuid4().hex[:12], "due": due, "at": now, "trigger": trigger or "scheduled"}

    @staticmethod
    def add_history(item, entry):
        """Add an entry, keeping the last HISTORY: the oldest ended or not-run entries go first, so a run that is
        still running keeps its entry (and gets its result) however many notes follow it."""
        history = item.get("history") if isinstance(item.get("history"), list) else []
        history = [*history, {key: value for key, value in entry.items() if value is not None}]
        while len(history) > HISTORY:
            history.pop(next((index for index, old in enumerate(history[:-1]) if not open_entry(old)), 0))
        item["history"] = history

    def reconcile(self, item, now):
        """Fill in the result of runs that ended (a run the app was restarted during, a deleted chat). True if changed."""
        changed = False
        for entry in item.get("history") or []:
            if not open_entry(entry):
                continue
            session_id = entry.get("session_id")
            if session_id in self.manager.live or self.manager.busy_task(session_id) or session_id in self.runs:
                continue
            changed |= self.fill_result(entry, now)
        return changed

    def fill_result(self, entry, now):
        try:
            header = self.manager.store.header(entry["session_id"])
        except (ValueError, OSError):
            return False
        if header is None:
            entry.update(ended=now, terminal_reason="deleted")
            return True
        ended = header.get("turn_ended")  # The run's chat is new: its first turn ended is the run's.
        if not isinstance(ended, (int, float)) or isinstance(ended, bool):
            return False
        result = header.get("last_turn_result") if isinstance(header.get("last_turn_result"), dict) else {}
        entry.update(ended=ended, terminal_reason=result.get("terminal_reason") or header.get("terminal_reason"),
                     **{key: result[key] for key in ("estimated_dbu", "tokens", "duration_s", "steps") if key in result})
        return True

    def finished(self, schedule_id, session_id):
        """A run's turn ended: its result goes into the schedule's history."""
        self.runs.pop(session_id, None)

        def record(item):
            for entry in item.get("history") or []:
                if isinstance(entry, dict) and entry.get("session_id") == session_id and "ended" not in entry:
                    self.fill_result(entry, clock())
        try:
            self.store.update(schedule_id, record)
        except (ValueError, OSError):
            log.warning("Scheduled runs: the result of a run could not be saved.")

    def active(self, schedule_id=None):
        """The chats of the runs (of this schedule) whose scheduled turn is running. finished() drops a run when its
        turn ends, so a later turn of yours in its chat is not a run of the schedule."""
        return [session_id for session_id, owner in self.runs.items()
                if schedule_id in (None, owner) and self.manager.busy_task(session_id)]

    def busy(self, item):
        """Whether a run of this schedule is running."""
        return bool(self.active(item["id"]))

    def running_count(self, schedule_id):
        return len(self.active()) + len(self.launching - {schedule_id})

    async def launch(self, schedule_id, trigger, due=None, note=None, *, user=False):
        """Start one run in a new chat: its id, or None when it did not start (the history says why). user: Run now
        or Run on a waiting run, whose refusal is a ValueError for the page instead."""
        if schedule_id in self.launching:
            if user:
                raise ValueError("A run of this schedule is already starting.")
            return None
        self.launching.add(schedule_id)
        try:
            return await self._launch(schedule_id, trigger, due, note, user)
        finally:
            self.launching.discard(schedule_id)

    async def _launch(self, schedule_id, trigger, due, note, user):
        item = self.store.get(schedule_id)
        if item is None:
            if user:
                raise ValueError("This schedule no longer exists.")
            return None
        now = clock()
        entry = {**self.entry(due, trigger, now), **({"note": note} if note else {})}

        def refuse(text, pause=None):
            if user:
                raise ValueError(text)
            self.record(schedule_id, {**entry, "outcome": "not_run", "note": f"Not run: {text}"}, pause=pause,
                        pause_note=text if pause == "invalid" else None)
            return None
        if problem := schedule_problem(item):
            return refuse(problem, pause="invalid")
        if self.busy(item):
            return refuse("the previous run of this schedule is still running.")
        if self.running_count(schedule_id) >= MAX_RUNNING:
            return refuse(f"{MAX_RUNNING} scheduled runs were already running.")
        try:
            workspace = str(checked_workspace(item.get("workspace") or "", self.settings))
            self.manager.workspace_available(workspace)
        except (ValueError, OSError) as exc:
            return refuse(self.settings.redact(str(exc)))
        status, text = await connection_status(self.manager)
        if status == "rejected":
            if not user:
                self.notify(f"Scheduled runs paused: Databricks rejected the token ({self.label(item)})")
            return refuse(text, pause="token_rejected")
        if status != "ok":
            return refuse(text)
        item = self.store.get(schedule_id)  # Edited, paused or deleted while the check ran.
        if item is None and user:
            raise ValueError("This schedule no longer exists.")
        if item is None or (not user and item.get("enabled") is not True):
            return None
        if problem := schedule_problem(item):
            return refuse(problem, pause="invalid")
        session = self.create_chat(item, workspace, trigger, due, now)
        self.manager.scheduled_turns[session["id"]] = {
            "schedule_id": schedule_id, "approval_timeout_minutes": item["approval_timeout_minutes"]}
        try:
            self.manager.start(session["id"], item["prompt"])
        except (ValueError, OSError) as exc:
            self.manager.scheduled_turns.pop(session["id"], None)
            try:
                self.manager.store.delete(session["id"])
            except (ValueError, OSError):
                pass
            return refuse(self.settings.redact(str(exc))[:500])
        self.runs[session["id"]] = schedule_id
        task = self.manager.tasks.get(session["id"])
        if task is not None:
            task.add_done_callback(lambda _: self.finished(schedule_id, session["id"]))
        self.record(schedule_id, {**entry, "outcome": "started", "session_id": session["id"], "started": now},
                    clear_pending=True)
        return session["id"]

    def create_chat(self, item, workspace, trigger, due, now):
        """The run's chat: the schedule's mode, model, effort and limits as its own, tagged 'scheduled', and named
        after the schedule (no title request)."""
        store, values = self.manager.store, self.settings.values
        session = store.create({**values, "workspace": workspace})
        stamp = datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M")
        session.update(title=f"{self.manager.redact_title(item['name'])[:MAX_NAME]} · {stamp}", title_generated=True,
                       permission_mode=item["permission_mode"], model=item.get("model") or values["model"],
                       turn_token_limit=item["turn_token_limit"], turn_time_limit_minutes=item["turn_time_limit_minutes"],
                       tags=[TAG], schedule={"id": item["id"], "name": item["name"], "trigger": trigger,
                                             **({"due": due} if due is not None else {})})
        if item.get("spending_limit_dbu"):  # 0: the chat follows Settings → Spending limit per conversation.
            session["spending_limit_dbu"] = item["spending_limit_dbu"]
        if item.get("reasoning_effort"):
            session["reasoning_effort"] = item["reasoning_effort"]
        if type(item.get("max_agent_steps")) is int:
            session["max_agent_steps"] = item["max_agent_steps"]
        store.save(session)
        return session

    def record(self, schedule_id, entry, pause=None, clear_pending=False, pause_note=None):
        def change(item):
            self.add_history(item, entry)
            if pause:
                item.update(enabled=False, paused_reason=pause, **({"paused_note": pause_note} if pause_note else {}))
            if clear_pending or pause:
                item.pop("pending", None)
        try:
            self.store.update(schedule_id, change)
        except (ValueError, OSError):
            log.warning("Scheduled runs: a schedule's history could not be saved.")

    # The routes' operations (schedules_api.py)

    def create(self, values, workspace, now=None):
        now = clock() if now is None else now
        items = self.store.read()
        if len(items) >= MAX_SCHEDULES:
            raise ValueError(f"There are already {MAX_SCHEDULES} schedules. Delete one first.")
        fields = check_schedule(values)
        item = {"id": uuid.uuid4().hex[:12], **fields, "workspace": workspace, "enabled": values.get("enabled") is not False,
                "created": now, "anchor": now, "history": []}
        item["next_run"] = next_fire(item, now)
        if item["next_run"] is None:
            raise ValueError("No date matches this cron line (such as 30 February).")
        if not item["enabled"]:
            item["paused_reason"] = "paused"
        items.append(item)
        self.store.write(items)
        return item

    def edit(self, schedule_id, values, now=None):
        now = clock() if now is None else now
        fields = check_schedule(values)

        def change(item):
            timing = (fields["spec"], fields["timezone"]) != (item.get("spec"), item.get("timezone"))
            item.update(fields)
            if timing or item.get("next_run") is None:
                item["anchor"] = now
                item["next_run"] = next_fire(item, now)
                if item["next_run"] is None:
                    raise ValueError("No date matches this cron line (such as 30 February).")
        return self.changed(schedule_id, change)

    def changed(self, schedule_id, change):
        item = self.store.update(schedule_id, change)
        if item is None:
            raise KeyError("This schedule no longer exists.")
        return item

    def pause(self, schedule_id):
        def change(item):
            if isinstance(item.get("pending"), dict):
                old = item.pop("pending")
                self.add_history(item, {**self.entry(old.get("due"), old.get("trigger"), clock()), "outcome": "not_run",
                                        "note": "Not run: you paused the schedule."})
            item.update(enabled=False, paused_reason="paused")
        return self.changed(schedule_id, change)

    def resume(self, schedule_id, now=None):
        now = clock() if now is None else now

        def change(item):
            item.update(enabled=True, next_run=next_fire(item, now))  # Times passed while paused are not caught up.
            item.pop("paused_reason", None)
            item.pop("paused_note", None)
        return self.changed(schedule_id, change)

    def skip(self, schedule_id):
        def change(item):
            if not isinstance(item.get("pending"), dict):
                raise ValueError("No run of this schedule is waiting.")
            old = item.pop("pending")
            self.add_history(item, {**self.entry(old.get("due"), old.get("trigger"), clock()), "outcome": "skipped",
                                    "note": "Skipped by you."})
        return self.changed(schedule_id, change)

    def delete(self, schedule_id):
        items = self.store.read()
        kept = [item for item in items if item["id"] != schedule_id]
        if len(kept) == len(items):
            raise KeyError("This schedule no longer exists.")
        self.store.write(kept)

    def public(self, item):
        """A schedule as the page shows it: its fields, whether a run is running, and its zone's name."""
        try:
            zone = zone_label(zone_of(item.get("timezone") or ""))
        except ValueError:
            zone = item.get("timezone") or ""
        return {**item, "running": self.busy(item) or item["id"] in self.launching, "zone": zone,
                "mode_label": MODE_LABELS.get(item.get("permission_mode"), item.get("permission_mode"))}

    def status(self):
        """{enabled, running, problem} of the scheduler."""
        problem = self.lock_problem if self.enabled() and not self.running else None
        return {"enabled": self.enabled(), "running": self.running, "problem": problem}

    def attention(self):
        """The runs waiting for Run and the schedules a rejected token paused, over all projects (the page's banner)."""
        try:
            items = self.store.read()
        except ValueError:
            return {"ready": [], "paused": []}
        ready = [{"id": item["id"], "name": item.get("name"), "workspace": item.get("workspace"),
                  "due": item["pending"].get("due"), "note": item["pending"].get("note")}
                 for item in items if item.get("enabled") is True and isinstance(item.get("pending"), dict)]
        paused = [{"id": item["id"], "name": item.get("name"), "workspace": item.get("workspace")}
                  for item in items if item.get("paused_reason") == "token_rejected"]
        return {"ready": ready, "paused": paused}
