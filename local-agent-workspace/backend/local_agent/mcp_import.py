"""Import MCP servers from another tool's configuration (.mcp.json, claude_desktop_config.json).

A preview only: every entry is disabled, never started, and saved only when the user saves
Agent tools → Extensions. Environment variable and header values are never copied: each
becomes a secret reference ({"secret": NAME}) named after the ${VAR} it is or after its key,
and the preview lists the names to add to the credential file (a value that mixes text and a
variable is named after its key, with a note to put the whole value there). A server whose
command, arguments (including the one after a flag such as --api-key) or URL looks like it
holds a secret is skipped, so no value is echoed.
"""
import json
import re
import stat
import sys
from pathlib import Path

from .extensions import (DENIED_SERVER_ENV, DENIED_SERVER_ENV_PREFIXES, ENV_NAME, IDENTIFIER, MAX_SERVER_VALUES,
                         looks_secret, secret_reference_name, validate_config)
from .redaction import redact_secrets

MAX_FILE_BYTES = 64 * 1024
VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]{0,63})(?::-[^}]*)?\}|\$([A-Za-z_][A-Za-z0-9_]{0,63})")
SCHEME = re.compile(r"(?i)^(bearer|basic|token) ")
# Parts of a flag name that say its value names a place or a kind, not a secret (--token-file, --auth-type).
NOT_SECRET_FLAG_PARTS = {"FILE", "PATH", "DIR", "ENV", "VAR", "TYPE", "MODE", "URL", "NAME", "HEADER"}
SUPPORTED = {"command", "args", "env", "url", "headers", "type", "transport"}


def desktop_config_path():
    """Where the Claude desktop app keeps its MCP servers on this computer."""
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"
    return Path.home() / ".config/Claude/claude_desktop_config.json"


def read_config_file(path, credential_file=None):
    """The text of a configuration file to import: a regular file (not a symbolic link, not the
    credential file) of at most MAX_FILE_BYTES. FileNotFoundError when there is none; ValueError otherwise."""
    path = Path(path)
    info = path.lstat()  # FileNotFoundError: nothing to import.
    if stat.S_ISLNK(info.st_mode):
        raise ValueError(f"{path.name} is a symbolic link; only a regular file is imported.")
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"{path.name} is not a regular file.")
    if credential_file and Path(credential_file).expanduser().exists() and path.samefile(Path(credential_file).expanduser()):
        raise ValueError("The credential file is never imported.")
    if info.st_size > MAX_FILE_BYTES:
        raise ValueError(f"{path.name} is larger than {MAX_FILE_BYTES // 1024} KB.")
    with path.open("rb") as source:
        raw = source.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError(f"{path.name} is larger than {MAX_FILE_BYTES // 1024} KB.")
    return raw.decode("utf-8")


def _identifier(name, taken):
    # No "__": it separates the server from the tool in mcp__<server>__<tool>.
    base = re.sub(r"_{2,}", "_", re.sub(r"[^a-z0-9_-]+", "-", name.lower())).strip("-_") or "server"
    if not base[0].isalpha():
        base = "mcp-" + base
    base = base[:20].rstrip("-_") or "server"
    candidate, number = base, 2
    while candidate in taken or not IDENTIFIER.fullmatch(candidate):
        suffix = f"-{number}"
        candidate = base[:20 - len(suffix)].rstrip("-_") + suffix
        number += 1
    taken.add(candidate)
    return candidate


def _reference(name, value, header=False):
    """(reference, note): a secret reference for value, named after the ${VAR} that is the whole value, else
    after name. A header keeps a Bearer/Basic/Token scheme as its prefix; no other text of the value is
    kept. note (else None) says to put the whole value under the key when it mixes text and a variable."""
    text = value if isinstance(value, str) else ""
    scheme = SCHEME.match(text) if header else None
    rest = text[scheme.end():] if scheme else text
    match = VARIABLE.fullmatch(rest.strip())
    reference = {"secret": (match.group(1) or match.group(2)) if match else secret_reference_name(name)}
    if scheme:
        reference["prefix"] = scheme.group(0)
    note = None
    if not match and VARIABLE.search(rest):
        whole = f"the part after {scheme.group(0).strip()}" if scheme else "the whole value"
        note = (f"{name} mixes text with a variable: put {whole} under {reference['secret']} in your credential "
                "file (the text was not imported).")
    return reference, note


def _secret_argument(args):
    """Whether an argument is a flag that names a secret (--api-key, --token, --password) with its value
    (--api-key=…) or followed by one."""
    for index, arg in enumerate(args):
        if not isinstance(arg, str) or not arg.startswith("-"):
            continue
        flag, equals, value = arg.lstrip("-").partition("=")
        parts = set(re.split(r"[_-]", flag.upper()))
        if not flag or not looks_secret(flag) or parts & NOT_SECRET_FLAG_PARTS or flag.lower().startswith(("no-", "no_")):
            continue
        following = args[index + 1] if index + 1 < len(args) else None
        if (equals and value) or (not equals and isinstance(following, str) and following and not following.startswith("-")):
            return True
    return False


def _secret_in(values, tokens):
    return any(isinstance(value, str) and (redact_secrets(value) != value or any(token and token in value for token in tokens))
               for value in values)


def parse(data, existing_ids=(), tokens=()):
    """{entries, needs, skipped, ignored, notes}: the servers of data ({"mcpServers": {name: spec}}, or
    {"servers": {...}}) as disabled entries that validate_config accepts; needs: the credential-file
    keys their references name; skipped: [{name, reason}]; ignored: [{name, fields}] (fields such
    as cwd that this app does not use); notes: [{name, text}] (values to put whole in the credential
    file). tokens: secrets that must not appear in what is returned."""
    if not isinstance(data, dict):
        raise ValueError("The configuration must be a JSON object with an mcpServers object.")
    servers = data.get("mcpServers", data.get("servers"))
    if not isinstance(servers, dict):
        raise ValueError("No mcpServers object was found in this configuration.")
    taken = set(existing_ids)
    entries, needs, skipped, ignored, notes_shown = [], [], [], [], []
    for name, spec in list(servers.items())[:50]:
        label = str(name)[:80] or "server"
        if not isinstance(spec, dict):
            skipped.append({"name": label, "reason": "Not an object."})
            continue
        kind = str(spec.get("type") or spec.get("transport") or "").lower()
        if kind == "sse":
            skipped.append({"name": label, "reason": "SSE servers are not supported; use the server's Streamable HTTP "
                                                    "URL if it has one."})
            continue
        arguments = spec.get("args") if isinstance(spec.get("args"), list) else []
        if _secret_in([spec.get("command"), spec.get("url"), *arguments], tokens) or _secret_argument(arguments):
            skipped.append({"name": label, "reason": "Its command, arguments or URL look like they hold a secret, which "
                                                    "is never imported. Add it by hand with a secret reference."})
            continue
        entry = {"id": _identifier(label, taken), "name": label, "enabled": False}
        notes, values_notes = [], []
        if isinstance(spec.get("command"), str) and kind in ("", "stdio"):
            entry.update(transport="stdio", command=spec["command"],
                         args=spec.get("args") if isinstance(spec.get("args"), list) else [])
            env = {}
            for key, value in (spec.get("env") or {}).items() if isinstance(spec.get("env"), dict) else ():
                if not isinstance(key, str) or not ENV_NAME.fullmatch(key) or key in DENIED_SERVER_ENV \
                        or key.startswith(DENIED_SERVER_ENV_PREFIXES) or len(env) >= MAX_SERVER_VALUES:
                    notes.append(f"env {str(key)[:64]}")
                    continue
                env[key], note = _reference(key, value)
                values_notes.extend([note] if note else [])
            if env:
                entry["env"] = env
        elif isinstance(spec.get("url"), str) and kind in ("", "http", "streamable-http", "streamablehttp"):
            entry.update(transport="http", url=spec["url"])
            headers = {}
            for key, value in (spec.get("headers") or {}).items() if isinstance(spec.get("headers"), dict) else ():
                if len(headers) >= MAX_SERVER_VALUES:
                    notes.append(f"headers {str(key)[:64]}")
                    continue
                headers[str(key)], note = _reference(str(key), value, header=True)
                values_notes.extend([note] if note else [])
            if headers:
                entry["headers"] = headers
        else:
            skipped.append({"name": label, "reason": "It needs a command (stdio) or an HTTP url."})
            taken.discard(entry["id"])
            continue
        notes.extend(sorted(str(key)[:40] for key in spec if key not in SUPPORTED))
        try:
            entry = validate_config({"servers": [entry]}, saved_ids=())["servers"][0]
        except ValueError as error:
            skipped.append({"name": label, "reason": str(error)})
            taken.discard(entry["id"])
            continue
        entries.append(entry)
        if notes:
            ignored.append({"name": label, "fields": notes[:20]})
        notes_shown.extend({"name": label, "text": note} for note in values_notes)
        for field in ("env", "headers"):
            for value in (entry.get(field) or {}).values():
                if isinstance(value, dict) and value["secret"] not in needs:
                    needs.append(value["secret"])
    return {"entries": entries, "needs": needs, "skipped": skipped, "ignored": ignored, "notes": notes_shown}


def parse_text(text, existing_ids=(), tokens=()):
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_FILE_BYTES:
        raise ValueError(f"Paste at most {MAX_FILE_BYTES // 1024} KB of JSON.")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f"Not valid JSON ({error.msg} at line {error.lineno}).") from None
    return parse(data, existing_ids, tokens)
