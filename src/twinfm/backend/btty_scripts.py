"""Remote shell scripts for BttyBackend, and the parsers for what they print.

Every builder returns the text of one POSIX ``sh`` script for ``btty --exec``.

* Every path is absolute and goes through ``shlex.quote``. Because each path
  starts with ``/``, no path can be mistaken for an option.
* A script never calls ``exit``: btty ``eval``s it in its own shell. It
  removes aliases on its first line, because that shell is an interactive
  ``bash -i`` where ``rm``, ``mv`` or ``cp`` may be aliased to prompt.
* Listing, walking and stat run one fixed ``python3`` program (``_PY``):
  ``python3 -I -c <program> <l|w|s> <path>``, the program and the path each
  passed as one ``shlex.quote``'d argument, never interpolated into the
  Python source. It works on bytes paths (``os.fsencode``), so every name
  round-trips exactly, non-UTF-8 bytes included. The user verified live that
  ``python3`` and ``--exec --json`` work in the Beacon IDE. Without
  ``python3`` on the remote, listing fails with OperationFailed (exit
  status 127); there is no shell fallback.
* The mutation scripts (mkdir, delete, rename, copy) are plain ``sh``. Each
  failure is classified by an explicit shell test (``[ -e ]``, ``[ -d ]``,
  ``[ -L ]``, ``[ -w ]``...), never by GNU or BSD message text. They use
  ``base64``, ``ls -A``, ``mkdir``, ``rmdir``, ``rm``, ``mv`` and ``cp -a``.

Size budget
    btty base64-encodes the command into a wrapper script, then base64-encodes
    that wrapper into a line it types at a shell, capped at 3500 bytes
    (bcli src/btty/exec.ts buildScriptLine, src/btty/tty.ts MAX_TYPED). The
    largest command that fits today is 1590 bytes. Every script here is kept
    within ``COMMAND_BUDGET`` (1200 UTF-8 bytes), which leaves a margin for
    changes to btty's wrapper. A script that would exceed it raises
    OperationFailed about the offending path, and nothing is sent. Bulk deletes
    are split into as many scripts as needed (``delete_scripts``). The
    python3 program takes most of a listing script's budget, so list, walk
    and stat accept quoted paths of up to about 270 bytes.

Output records
    btty merges the remote stdout and stderr, so every record is one line
    starting with ``@`` and every other line is noise (an error message from a
    tool, for instance). Names, link targets and paths travel base64-encoded
    (standard alphabet, padded). GNU ``base64`` wraps its output, so an @X
    path is every token after the code, joined back together.

    ``@J <json object>``
        One entry, printed by the python3 program, for example::

            @J {"name": "ZG9jcw==", "kind": "symlink", "size": 4,
                "mtime": 1760000000.25, "mode": 511,
                "link_target": "ZG9jcw==", "target_kind": "dir"}

        (all on one line). The keys:

        ``name``
            base64 of the raw bytes of the path relative to the listed
            directory: the entry's name for a listing, ``sub/dir/name`` for a
            walk, empty for a stat (the requested path is used instead).
        ``kind``
            ``"file"``, ``"dir"``, ``"symlink"`` or ``"other"``, by lstat.
        ``size``
            lstat ``st_size`` in bytes. It is kept only for a file or a
            symlink; for the other kinds the parser makes it None.
        ``mtime``
            lstat ``st_mtime``, POSIX seconds (a JSON number, may be fractional).
        ``mode``
            The permission bits (``stat.S_IMODE``) as a JSON integer, e.g.
            420 for 0o644.
        ``link_target``
            For a symlink, base64 of the raw link text; otherwise null.
        ``target_kind``
            For a symlink, the kind of what it finally resolves to
            (``"file"``, ``"dir"`` or ``"other"``); null when it dangles or
            loops, and for every other kind.

        A missing or wrongly typed ``name`` or ``kind`` makes the line noise;
        a wrongly typed optional field becomes None.
    ``@X <code> [<base64 path>]``
        A classified failure. ``code`` is an errno name: ENOENT, EEXIST,
        ENOTDIR, EISDIR, ENOTEMPTY, EACCES or EPERM map to their FsError,
        EINVAL means a directory into itself, and anything else (EIO, ELOOP...)
        is OperationFailed carrying the noise lines as the reason. The path is
        the one at fault (for a walk, possibly a directory below the walked
        one); when it is absent, the error is about the operation's own path.
        The python3 program prints the exception text as a noise line just
        before its @X. Its stat reports ENOTDIR as ENOENT, like the shell tests.
    ``@K``
        The script ran to completion and succeeded. Output without ``@K`` or
        ``@X`` means the script did not finish.
"""

from __future__ import annotations

import base64
import binascii
import json
import posixpath
import secrets
import shlex
from collections.abc import Sequence
from dataclasses import dataclass, field

from twinfm.backend.base import (
    AlreadyExists,
    DirectoryNotEmpty,
    Entry,
    FsError,
    IsADirectory,
    Kind,
    NotADirectory,
    NotFound,
    OperationFailed,
    PermissionDenied,
)

__all__ = [
    "COMMAND_BUDGET",
    "EntryRecord",
    "ScriptOutput",
    "copy_script",
    "delete_scripts",
    "error_for",
    "list_script",
    "mkdir_script",
    "parse_output",
    "rename_script",
    "stat_script",
    "to_entry",
    "walk_script",
]

COMMAND_BUDGET = 1200
"""The most UTF-8 bytes one ``--exec`` command may have (see the module docstring)."""

_HEAD = "unalias -a 2>/dev/null\n"

# The python3 program behind list_script, walk_script and stat_script, run as
# `python3 -I -c <_PY> <l|w|s> <path>`: l lists, w walks, s stats. It prints
# one @J record per entry, then @K, or the exception text and an @X record
# (see the module docstring). Every path it handles is bytes. It holds no
# single quote, so shlex.quote passes it as one '...' word.
_PY = """\
import os,sys,json,stat as S,base64,errno
m=sys.argv[1];p=os.fsencode(sys.argv[2])
b=lambda x:base64.b64encode(x).decode()
K={S.S_IFDIR:"dir",S.S_IFREG:"file",S.S_IFLNK:"symlink"}
k=lambda s:K.get(S.S_IFMT(s.st_mode),"other")
def e(q,r):
 s=os.lstat(q);n=k(s);l=t=None
 if n=="symlink":
  l=b(os.readlink(q))
  try:t=k(os.stat(q))
  except OSError:pass
 print("@J",json.dumps(dict(name=b(r),kind=n,size=s.st_size,mtime=s.st_mtime,\
mode=S.S_IMODE(s.st_mode),link_target=l,target_kind=t)))
 return n
def w(d,r):
 for i in sorted(os.scandir(d),key=lambda i:i.name):
  c=r+b"/"+i.name if r else i.name
  try:n=e(i.path,c)
  except FileNotFoundError:continue
  if n=="dir" and m=="w":w(i.path,c)
try:
 if m=="s":e(p,b"")
 else:w(p,b"")
 print("@K")
except OSError as x:
 c=errno.errorcode.get(x.errno,"EIO")
 if m=="s" and c=="ENOTDIR":c="ENOENT"
 print(x);print("@X",c,b(x.filename or b""))"""
assert "'" not in _PY

# Prints @X <code> <base64 of $2>.
_FAIL2 = 'x(){ echo "@X $1" $(printf %s "$2"|base64);}\n'

_KINDS: dict[str, Kind] = {"file": "file", "dir": "dir", "symlink": "symlink", "other": "other"}
_TARGETS: dict[str, Kind] = {"file": "file", "dir": "dir", "other": "other"}

_ERRORS: dict[str, type[FsError]] = {
    "ENOENT": NotFound,
    "EEXIST": AlreadyExists,
    "ENOTDIR": NotADirectory,
    "EISDIR": IsADirectory,
    "ENOTEMPTY": DirectoryNotEmpty,
    "EACCES": PermissionDenied,
    "EPERM": PermissionDenied,
}


def _q(path: str) -> str:
    if not path.startswith("/") or "\0" in path:
        raise OperationFailed(path, "not an absolute path")
    return shlex.quote(path)


def _size(text: str) -> int:
    return len(text.encode("utf-8", "surrogateescape"))


def _too_long(path: str, size: int) -> OperationFailed:
    return OperationFailed(
        path, f"path too long for one btty command ({size} of {COMMAND_BUDGET} bytes)"
    )


def _fit(script: str, path: str) -> str:
    size = _size(script)
    if size > COMMAND_BUDGET:
        raise _too_long(path, size)
    return script


# -- builders --------------------------------------------------------------------


def _python(op: str, path: str) -> str:
    return _fit(f"{_HEAD}python3 -I -c {shlex.quote(_PY)} {op} {_q(path)}", path)


def list_script(path: str) -> str:
    """The entries directly inside directory ``path`` (a symlink at ``path``
    is followed): one @J per entry, ``name`` being its name, then @K. A
    failure is @X ENOENT / ENOTDIR / EACCES / ... about ``path``."""
    return _python("l", path)


def walk_script(path: str) -> str:
    """Every entry below directory ``path``, each directory before its
    contents and siblings by name, without descending into symlinks: @J
    records, ``name`` being the path relative to ``path``, then @K. The first
    failure ends the walk with @X about the directory that failed."""
    return _python("w", path)


def stat_script(path: str) -> str:
    """One @J record for ``path`` itself (lstat) then @K, or @X ENOENT (also
    when a parent is not a directory), EACCES, ... The record's ``name`` is
    empty: use the requested path."""
    return _python("s", path)


def mkdir_script(path: str) -> str:
    """Create directory ``path`` (one level). @K, or @X EEXIST / ENOENT /
    ENOTDIR (the parent is missing or not a directory) / EACCES / EIO."""
    return _fit(
        _HEAD
        + f"p={_q(path)}"
        + ";q=${p%/*};q=${q:-/}\n"
        + 'if [ -e "$p" ]||[ -L "$p" ];then echo @X EEXIST\n'
        + 'elif [ ! -d "$q" ];then if [ -e "$q" ];then echo @X ENOTDIR;else echo @X ENOENT;fi\n'
        + 'elif [ ! -w "$q" ];then echo @X EACCES\n'
        + 'elif mkdir "$p";then echo @K;else echo @X EIO;fi',
        path,
    )


def _delete_parts(recursive: bool) -> tuple[str, str]:
    if recursive:
        dir_branch = 'rm -rf "$p"||x EIO'
    else:
        dir_branch = 'if [ -n "$(ls -A "$p")" ];then x ENOTEMPTY;else rmdir "$p"||x EIO;fi'
    head = _HEAD + 'x(){ echo "@X $1" $(printf %s "$p"|base64);e=1;};e=\nfor p in'
    tail = (
        ";do\n"
        + 'if [ ! -e "$p" ]&&[ ! -L "$p" ];then x ENOENT\n'
        + f'elif [ -d "$p" ]&&[ ! -L "$p" ];then {dir_branch}\n'
        + 'else rm -f "$p"||x EIO;fi\n'
        + '[ "$e" ]&&break;done;[ "$e" ]||echo @K'
    )
    return head, tail


def delete_scripts(paths: Sequence[str], recursive: bool = False) -> list[tuple[list[str], str]]:
    """Scripts that delete ``paths`` in order, as ``(paths, script)`` pairs,
    each script within the budget and holding as many paths as fit.

    A path is removed with ``rm -f`` (file, symlink, other), ``rmdir`` (an
    empty directory) or, with ``recursive``, ``rm -rf``; a symlink is never
    followed. Each script stops at its first failure and prints
    ``@X <code> <base64 path>`` (ENOENT, ENOTEMPTY or EIO); otherwise it prints
    @K. Paths must not be ``/``.
    """
    head, tail = _delete_parts(recursive)
    fixed = _size(head + tail)
    chunks: list[list[str]] = []
    size = fixed
    for path in paths:
        cost = _size(_q(path)) + 1
        if fixed + cost > COMMAND_BUDGET:
            raise _too_long(path, fixed + cost)
        if not chunks or size + cost > COMMAND_BUDGET:
            chunks.append([])
            size = fixed
        chunks[-1].append(path)
        size += cost
    return [
        (chunk, _fit(head + "".join(" " + _q(p) for p in chunk) + tail, chunk[0]))
        for chunk in chunks
    ]


def _transfer_script(src: str, dst: str, overwrite: bool, action: str, rename: bool) -> str:
    same = 'elif [ "$s" = "$d" ];then echo @K\n' if rename else ""
    failed = '"$s"' if rename else '"$d"'
    return _fit(
        _HEAD
        + f"s={_q(src)};d={_q(dst)};o={'1' if overwrite else ''}"
        + ";q=${d%/*};q=${q:-/};r=;y=\n"
        + '[ -d "$s" ]&&[ ! -L "$s" ]&&r=1;{ [ -e "$d" ]||[ -L "$d" ]; }&&y=1\n'
        + _FAIL2
        + 'if [ ! -e "$s" ]&&[ ! -L "$s" ];then x ENOENT "$s"\n'
        + same
        + 'elif [ ! -d "$q" ];then if [ -e "$q" ];then x ENOTDIR "$d";else x ENOENT "$d";fi\n'
        + 'elif [ "$y" ]&&[ ! "$o" ];then x EEXIST "$d"\n'
        + 'elif [ -d "$d" ]&&[ ! -L "$d" ];then x EISDIR "$d"\n'
        + 'elif [ "$r" ]&&[ "$y" ];then x ENOTDIR "$d"\n'
        + 'elif [ "$r" ]&&case $(command cd -P "$q"&&pwd -P)/ in'
        + ' "$(command cd -P "$s"&&pwd -P||echo -)"/*);;*)false;;esac;then x EINVAL "$d"\n'
        + f"elif {action};then echo @K;else x EIO {failed};fi",
        src if len(src) >= len(dst) else dst,
    )


def rename_script(src: str, dst: str, overwrite: bool = False) -> str:
    """Move ``src`` to the full path ``dst`` with ``mv``, by the contract's
    rules: the same path is a no-op, nothing is replaced without
    ``overwrite``, a directory is never replaced, a directory never replaces
    a file, a symlink at ``dst`` is removed (not followed) before it is
    replaced, and moving a directory into its own subtree (checked on physical
    paths) is EINVAL. @K, or @X <code> <base64 path>."""
    action = '{ [ ! -L "$d" ]||rm -f "$d"; }&&mv -f "$s" "$d"'
    return _transfer_script(src, dst, overwrite, action, rename=True)


def copy_script(src: str, dst: str, overwrite: bool = False) -> str:
    """Copy ``src`` to the full path ``dst`` with ``cp -a`` (symlinks kept as
    links, modes kept), under rename's rules except that the same path counts
    as existing. The copy is made in a hidden sibling of ``dst`` and moved into
    place with ``mv``, so it appears all at once. @K, or @X <code> <base64 path>."""
    tmp = f".twinfm-{secrets.token_hex(6)}"
    action = (
        f't=$q/{tmp};cp -a "$s" "$t"&&{{ [ ! -L "$d" ]||rm -f "$d"; }}&&mv -f "$t" "$d"'
        '||{ rm -rf "$t";false;}'
    )
    return _transfer_script(src, dst, overwrite, action, rename=False)


# -- parsers ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EntryRecord:
    """One @J record, decoded."""

    rel: str
    kind: Kind
    target_kind: Kind | None
    size: int | None
    mtime: float | None
    mode: int | None
    link_target: str | None


@dataclass(slots=True)
class ScriptOutput:
    """Everything a script printed, sorted out.

    entries
        The @J records, in output order.
    errors
        ``(code, path or None)`` for each @X record, in output order.
    done
        Whether @K was printed.
    noise
        Every other non-empty line (merged stderr of the remote tools).
    """

    entries: list[EntryRecord] = field(default_factory=list)
    errors: list[tuple[str, str | None]] = field(default_factory=list)
    done: bool = False
    noise: list[str] = field(default_factory=list)


def _decode(tokens: Sequence[str]) -> bytes:
    return base64.b64decode("".join(tokens), validate=True)


def _text(raw: bytes) -> str:
    return raw.decode("utf-8", "surrogateescape")


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _json_record(text: str) -> EntryRecord | None:
    """The EntryRecord of an @J line's JSON, or None if it is malformed."""
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    raw_kind, name = data.get("kind"), data.get("name")
    kind = _KINDS.get(raw_kind) if isinstance(raw_kind, str) else None
    if kind is None or not isinstance(name, str):
        return None
    is_link = kind == "symlink"
    target, target_kind = data.get("link_target"), data.get("target_kind")
    try:
        rel = _text(_decode([name]))
        link = _text(_decode([target])) if is_link and isinstance(target, str) else None
    except (binascii.Error, ValueError):
        return None
    mtime, mode = data.get("mtime"), _int(data.get("mode"))
    return EntryRecord(
        rel=rel,
        kind=kind,
        target_kind=_TARGETS.get(target_kind) if is_link and isinstance(target_kind, str) else None,
        size=_int(data.get("size")) if kind in ("file", "symlink") else None,
        mtime=float(mtime) if _int(mtime) is not None or isinstance(mtime, float) else None,
        mode=mode & 0o7777 if mode is not None else None,
        link_target=link,
    )


def parse_output(text: str) -> ScriptOutput:
    """Sort a script's output (btty's merged stdout and stderr) into records
    and noise. A malformed record line counts as noise."""
    out = ScriptOutput()
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        tokens = line.split()
        tag = tokens[0]
        if tag == "@J":
            record = _json_record(line[len(tag) :])
            if record is not None:
                out.entries.append(record)
                continue
        elif tag == "@X" and len(tokens) >= 2:
            try:
                path = _text(_decode(tokens[2:])) if len(tokens) > 2 else None
            except (binascii.Error, ValueError):
                path = None
            out.errors.append((tokens[1], path))
            continue
        elif tag == "@K" and len(tokens) == 1:
            out.done = True
            continue
        out.noise.append(line)
    return out


def to_entry(record: EntryRecord, base: str) -> Entry:
    """The Entry for ``record`` from a listing or walk of directory ``base``."""
    path = posixpath.join(base, record.rel)
    return Entry(
        name=posixpath.basename(path),
        path=path,
        kind=record.kind,
        size=record.size,
        mtime=record.mtime,
        mode=record.mode,
        link_target=record.link_target,
        target_kind=record.target_kind,
    )


def error_for(
    output: ScriptOutput, path: str, backend_id: str | None, status: int | None
) -> FsError | None:
    """The FsError a script's output reports, or None if it succeeded.

    The first @X record decides; its path (or ``path`` when it names none)
    becomes the error's path. Output with neither @X nor @K means the script
    did not finish, which is OperationFailed. EIO and unfinished scripts carry
    the noise lines as their message.
    """
    detail = "; ".join(output.noise)[:500]
    if output.errors:
        code, where = output.errors[0]
        where = where or path
        if code == "EINVAL":
            return OperationFailed(where, "cannot move or copy a directory into itself", backend_id)
        cls = _ERRORS.get(code)
        if cls is not None:
            return cls(where, None, backend_id)
        return OperationFailed(where, detail or "the remote command failed", backend_id)
    if output.done:
        return None
    reason = "the remote command did not finish"
    if status is not None:
        reason += f" (exit status {status})"
    return OperationFailed(path, f"{reason}: {detail}" if detail else reason, backend_id)
