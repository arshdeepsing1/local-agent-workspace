"""Credential-looking file names: what a command must not read without the user seeing it (M6j).

looks_secret(word) says whether one command word or path names such a file, and returns the part
that matched (the approval card names it). Two kinds of names count:

- Credential files, in any folder: the file tools' SECRET_NAMES and `.env.*` (not `.env.example`);
  private keys (`id_rsa`, `id_ed25519`, ... but not their `.pub`); key and certificate stores
  (`*.pem`, `*.key`, `*.p12`, `*.pfx`, `*.jks`, `*.keystore`); every file in `.ssh` except public
  keys, `known_hosts`, `authorized_keys` and `config`; every file in `.gnupg`; `.aws/credentials`
  and `.aws/config`, `.kube/config` and other kube config files, `.docker/config.json`,
  `.config/gh/hosts.yml`, `.my.cnf`, `.vault-token`, `.s3cfg` and `.boto`.
- Data files whose name contains token, secret, password, passwd, credential or apikey: with a
  data extension (.txt, .json, .env, .cfg, .conf, .ini, .yaml, .yml, .toml, .properties), or with
  none when the word is a path or the name a dotfile (`secrets/db_token`, `.npm-token`). Source and
  documentation files never match (`tokenizer.py`, `password_reset.ts`, `secret_santa.md`), nor
  tokenizer files (`tokenizer.json`, `special_tokens_map.json`), nor a
  bare word such as `token` in `grep -rn token src`, which is a search pattern, nor a URL.

Plus the configured credential file (by path) and the name patterns from Settings → Secret file
names (`*.tfvars`). Pure functions, standard library only.
"""
import fnmatch
import os
import re
from pathlib import PurePosixPath

from .tools import SECRET_NAMES

KEY_NAMES = ("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "id_xmss")
KEY_SUFFIXES = frozenset({".pem", ".key", ".p12", ".pfx", ".jks", ".keystore"})
OTHER_NAMES = frozenset({".my.cnf", ".vault-token", ".s3cfg", ".boto", "kubeconfig"})
# Folders whose files are credentials: None means every file, else only the names listed.
FOLDER_FILES = {".ssh": None, ".gnupg": None, ".aws": frozenset({"credentials", "config"}),
                ".kube": frozenset({"config"}), ".docker": frozenset({"config.json"})}
SSH_PUBLIC = frozenset({"known_hosts", "known_hosts.old", "authorized_keys", "authorized_keys2", "config"})
NESTED_FILES = ((".config", "gh", "hosts.yml"),)
WORDS = ("token", "secret", "password", "passwd", "credential", "apikey", "api_key", "api-key")
# Model and tokenizer files are data too, but hold no secret; nor does the system's world-readable /etc/passwd.
NOT_SECRET = re.compile(r"tokeni[sz]\w*|special_tokens\w*|tokens?_map\w*|token_type\w*|^passwd$")
DATA_SUFFIXES = frozenset({".txt", ".json", ".env", ".cfg", ".conf", ".ini", ".yaml", ".yml", ".toml", ".properties"})
MAX_PATTERNS = 32
MAX_PATTERN_CHARS = 100


def check_patterns(value):
    """Settings → Secret file names: a list of at most 32 name patterns ('*.tfvars'), or ValueError."""
    if (not isinstance(value, list) or len(value) > MAX_PATTERNS
            or any(not isinstance(item, str) for item in value) or len(set(value)) != len(value)):
        raise ValueError(f"List at most {MAX_PATTERNS} different secret file name patterns.")
    for item in value:
        if (not item.strip() or item != item.strip() or len(item) > MAX_PATTERN_CHARS or "/" in item
                or any(ord(c) < 32 or ord(c) == 127 for c in item)):
            raise ValueError(f"A secret file name pattern is a file name of 1 to {MAX_PATTERN_CHARS} characters "
                             "without '/', such as *.tfvars or prod-*.yaml.")
        if item.strip("*?") == "":
            raise ValueError(f"'{item}' would match every file; name the files more exactly.")
    return list(value)


def patterns_setting(values):
    """The valid patterns from Settings values (a hand-edited bad value means none)."""
    value = values.get("secret_file_patterns") if isinstance(values, dict) else None
    try:
        return tuple(item.casefold() for item in check_patterns(value))
    except ValueError:
        return ()


def _named_by_parts(parts, patterns):
    """The part of a path (as casefolded parts) that makes it a credential file, or None."""
    for index, part in enumerate(parts):
        if part in SECRET_NAMES or (part.startswith(".env.") and part != ".env.example") or part in OTHER_NAMES:
            return part
        if any(fnmatch.fnmatchcase(part, pattern) for pattern in patterns):
            return part
        if part in FOLDER_FILES and index + 1 < len(parts):
            child, allowed = parts[index + 1], FOLDER_FILES[part]
            if part == ".ssh" and (child in SSH_PUBLIC or child.endswith(".pub")):
                continue
            if allowed is None or child in allowed:
                return f"{part}/{child}"
    for nested in NESTED_FILES:
        for index in range(len(parts) - len(nested) + 1):
            if tuple(parts[index:index + len(nested)]) == nested:
                return "/".join(nested)
    return None


def _named_by_name(name, is_path):
    """Whether a file name alone looks like a key or a data file holding a secret."""
    if not name or name in (".", ".."):
        return False
    if any(name == key or (name.startswith(key) and name[len(key)] in "._-") for key in KEY_NAMES):
        return not name.endswith(".pub")
    suffix = PurePosixPath(name).suffix
    if suffix in KEY_SUFFIXES or name.endswith(".kubeconfig"):
        return True
    if not any(word in NOT_SECRET.sub("", name) for word in WORDS):
        return False
    if suffix in DATA_SUFFIXES:
        return True
    # No extension: a dotfile, or a word that is plainly a path (a bare word is usually a pattern).
    return suffix == "" and (name.startswith(".") or is_path)


def looks_secret(word, credential_file=None, patterns=()):
    """The matched part when a command word (or the value after its first '=') names a credential-looking
    file, else None. patterns are casefolded name patterns; credential_file is an absolute path."""
    if not isinstance(word, str) or not word:
        return None
    candidates = [word]
    if "=" in word:
        candidates.append(word.split("=", 1)[1])
    configured = str(credential_file) if credential_file else ""
    for candidate in candidates:
        if not candidate or "://" in candidate:
            continue
        if configured and (candidate == configured or os.path.expanduser(candidate) == configured):
            return candidate
        parts = [part.casefold() for part in PurePosixPath(candidate).parts if part != "/"]
        if not parts:
            continue
        found = _named_by_parts(parts, patterns)
        if found:
            return found
        if _named_by_name(parts[-1], "/" in candidate.rstrip("/")):
            return parts[-1]
    return None


def grep_excludes(patterns=()):
    """Name globs a recursive search (grep -r, rg) skips so that it never reads a credential-looking file."""
    names = sorted({*SECRET_NAMES, *OTHER_NAMES, ".env.*", *(f"{key}*" for key in KEY_NAMES),
                    *(f"*{suffix}" for suffix in KEY_SUFFIXES), "*.kubeconfig", *patterns,
                    *(f"*{word}*{suffix}" for word in WORDS for suffix in sorted(DATA_SUFFIXES)),
                    *(f".*{word}*" for word in WORDS)})
    folders = sorted({".ssh", ".gnupg", ".aws", ".kube", ".docker"})
    return names, folders
