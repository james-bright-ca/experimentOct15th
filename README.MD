# twinfm

A two-panel file manager in the Midnight Commander tradition, for local files
and for a Beacon IDE workspace reached through the `btty` command line tool.
Each panel shows a directory, either local or on the btty remote. You can
browse, select files and folders, copy and move them between the panels, and
rename and delete them on either side.

twinfm is a terminal (Textual) app. It needs Python 3.13 or later and, for the
remote side, a working `btty` on your PATH.

## Install

From the repository root, create a virtual environment and install twinfm into
it with pip:

```sh
python3.13 -m venv .venv
.venv/bin/python -m pip install -e .
```

This installs the `twinfm` command into `.venv/bin`.

## Run

```sh
.venv/bin/twinfm                      # both panels in the current directory
.venv/bin/twinfm ~/work btty:         # ~/work on the left, the btty workspace on the right
.venv/bin/python -m twinfm --help     # the same program, run as a module
```

```
twinfm [LEFT] [RIGHT] [--btty-exec | --no-btty-exec] [--remote-home PATH]
       [--config PATH] [--version]
```

`LEFT` and `RIGHT` are the two panels' starting locations. Each is a local
directory (relative paths are taken from the current directory) or `btty:` for
the btty workspace:

| Location | Opens |
|---|---|
| `btty:` | the remote home, `/mnt/home0/wst/workspaces` by default |
| `btty:NAME` | `NAME` inside the remote home, for example `btty:jake.blow.clearwateranalytics.com` opens `/mnt/home0/wst/workspaces/jake.blow.clearwateranalytics.com` |
| `btty:/abs/path` | that absolute remote path |
| anything else | a local path (write `./btty:x` for a local name that starts with `btty:`) |

Without arguments both panels open in the current directory. A bad flag, a
malformed config file or a relative `--remote-home` stops twinfm with an
error message and exit status 2.

## Keys

The footer shows the main keys in Midnight Commander's style, a digit for its
F-key and `^` for Ctrl:

```
^RRefresh ^OShell 3View 4Edit 5Copy 6Move ^NRename 7Mkdir 8Del 9Connect 10Quit
```

You can also click them.

| Key | Action |
|---|---|
| Enter | open the directory on the cursor row (`..` goes up) |
| Backspace | go to the parent directory, with the cursor on the one you left |
| Tab | switch to the other panel |
| Space, Insert, Ctrl-T | select or deselect the cursor row and move down (`..` cannot be selected) |
| a letter, digit or other printable character (not Space) | find a name in the panel, or type a path to go to (see [Find and go to](#find-and-go-to)) |
| F3 | view the file on the cursor row (local panels only, see [Viewing files](#viewing-files-f3)) |
| F4 | edit the file on the cursor row (local panels only, see [Editing files](#editing-files-f4)) |
| F5 | copy the selection, or the cursor row, to the other panel |
| F6 | move the selection, or the cursor row, to the other panel |
| Ctrl-N, Shift-F6 | rename the cursor row in place |
| F7 | make a directory |
| F8 | delete the selection, or the cursor row (directories recursively) |
| F9 | connect the panel to another location |
| F10 | quit |
| Ctrl-R | refresh the panel, past any cached listing (see [Listing cache](#listing-cache-and-ctrl-r)) |
| Ctrl-O | hide the panels and show a local shell; Ctrl-O again brings the panels back (see [The shell](#the-shell-ctrl-o)) |

The status line under each panel shows the number of items, or the number of
selected items and the total size of the selected files. While the panel is
loading it reads `Loading…` (see [While a panel is loading](#while-a-panel-is-loading)).

A name wider than the Name column makes the listing scroll sideways, until the
panel lists a directory whose names fit. The Size and Modified columns keep
their width, so in a narrow terminal ordinary names scroll too: at 80 columns
a name longer than about 8 characters, at 100 columns about 18.

F5 and F6 ask for the target, prefilled with the other panel's directory. An
existing directory receives the items. With one item, a path that does not
exist yet becomes its new name. A relative target is taken from the source
directory when both panels are on the same store, and otherwise from the
other panel's directory.

F9 asks for a location as `KIND:PATH`, where `KIND` is `local` or `btty`. A
path without `KIND:` stays on the panel's current store, and an empty `PATH`
is that store's home directory (your home directory for `local`, the remote
home for `btty`).

### Find and go to

Type in a panel to find a name in it. Any printable character except Space
opens the find line, which takes the place of the active panel's status line
and reads `Find: <text>`, then the number of the match and of all matches
(`1/3`) or `no match`. Pasting opens it too, with the first pasted line. A
transfer-only btty panel has no find line.

The cursor goes to the first name that starts with the text, else to the
first name that contains it. Matching ignores case. `*` stands for any run of
characters and `?` for exactly one; every other character, `[` and `]`
included, stands for itself. `..` is never a match.

With the find line open:

- Up and Down go to the previous and the next match, wrapping around.
- Space types a space.
- Backspace deletes the last character; on the last one it closes the line.
  Ctrl-U clears the text and the line stays open.
- Enter closes the line and, when the text matches, opens the directory on
  the cursor row. On a file nothing opens.
- Esc closes the line, and the cursor stays on the match.
- Any other key closes the line and does its own job: for example Insert or
  Ctrl-T selects the match and moves down, Tab switches to the other panel,
  and the F-keys work as usual. Shift, Control, Option or Command pressed on
  its own does not close the line.

Text that contains `/`, is exactly `~`, or starts with `local:` or `btty:` is
a path: the line reads `Go to: <text>`, and Enter goes there.

- `/tmp` is absolute. `Sub/` and `../x` are taken from the panel's directory.
- `~` and `~/x` are in the home directory of the panel's store: your home
  directory on a local panel, the remote home on a btty panel.
- `KIND:PATH`, such as `btty:/x` or `local:/tmp`, goes there as F9 does.
- Write `./btty:x` for a local name that starts with `btty:`.
- `~x`, `.` and `..` are searches; type `../` to go up.

The path of a file opens its directory with the cursor on the file. Once the
panel is there, the line closes. A path that does not exist shows one error,
and the line stays open with your text.

In a terminal that uses the kitty keyboard protocol but does not send the
text of a key, Shift with a digit or punctuation key types into the find line
the character it gives on a US keyboard layout (Shift+/ is `?`, Shift+2 is
`@`), whatever your layout is. Space types a space into an open find line.

### While a panel is loading

While a panel is loading a listing, its status line reads `Loading…` (an
open find line ends in `· loading…`). Until the active panel's listing
arrives only Tab, Shift+Tab and quit (F10, Esc 0, Option-0, Ctrl-Q) work, and
Ctrl-P still opens the command palette. Every other key, a click on another
footer key, a paste and a click on the loading panel's rows are ignored:
typed characters are dropped, not saved for later. The other panel, once
listed, works as usual.

### Esc and Option with a digit

If your F-keys do not reach the terminal (for example because they act as
media keys), use one of these instead. Each works on the main screen:

- Esc, then the digit: Esc 3 is F3, Esc 4 is F4, and so on to Esc 9 for F9,
  and Esc 0 is F10. You can type it slowly or quickly.
- Option with the digit, on a US keyboard layout: Option-3 to Option-9 act as
  F3 to F9 and Option-0 as F10. twinfm reads the character Option types
  (`£ ¢ ∞ § ¶ • ª º`) as the digit. In a dialog's text field these characters
  are typed as text. They never open or type into the find line.

### macOS Terminal.app and Shift-F6

macOS Terminal.app sends Shift-F6 as `ESC[26~`, which Textual 8.2.8 cannot
read as a key, so Shift-F6 does nothing there. Use Ctrl-N to rename. The
footer shows Ctrl-N (`^N`) as the Rename key for this reason.

Shift-F6 does work when the terminal sends it as `ESC[17;2~`, as xterm and
iTerm2 do. To make Terminal.app send that, open Terminal > Settings >
Profiles > Keyboard and add a key mapping with Key F6, Modifier Shift, and
Action "Send Text:" `\033[17;2~`. Press Esc in the text field to type
`\033`.

### Dialogs

- Tab, or the Left and Right arrows, move between the buttons. Enter presses
  the focused button.
- In a text field Enter accepts, and Esc cancels.
- Delete (F8) asks for confirmation with the focus on No. Y answers yes, and
  N or Esc answers no.
- When a destination file exists you can choose Overwrite, Skip (the focused
  button), Overwrite all, Skip all or Cancel. Esc cancels the rest of the
  batch.
- When an item fails you can choose Retry, Skip (the focused button), Skip
  all or Abort. Esc aborts.
- While an operation runs, Esc cancels it after the current file.

## Configuration

### Config file

twinfm reads one TOML file: the `--config PATH` file if given, else
`$TWINFM_CONFIG` if set, else `~/.config/twinfm/config.toml`. A missing file
means the defaults. The file holds a `[btty]` table and a `[viewer]` table.
Every key is optional, and the `[btty]` values below are the defaults:

```toml
[btty]
home = "/mnt/home0/wst/workspaces"   # remote home directory that btty: opens at (absolute)
command = "btty"                     # the btty executable
exec = true                          # exec mode; false means transfer-only
timeout = 120                        # seconds, btty --timeout for exec commands
tmp = "/tmp"                         # remote directory for archives of remote selections
archive_timeout = 900                # seconds, btty --timeout for pack, unpack and merge
archive_max_bytes = 268435456        # 256 MiB; a larger batch is transferred per file
```

| Key | Meaning |
|---|---|
| `home` | The remote directory a `btty:` location opens at and resolves relative remote paths against. An absolute path; a trailing `/` is dropped. |
| `command` | The btty executable, found through PATH unless it is a path. It is one program, not a shell command line. |
| `exec` | `true` (the default) for exec mode, `false` for transfer-only mode. See [btty modes](#btty-modes). |
| `timeout` | Seconds passed to `btty --timeout` for every exec command except the archive ones. A positive whole number. |
| `tmp` | Absolute remote directory where archive mode packs a remote selection before downloading it. The archive is only written, downloaded and deleted there, never moved into the workspace, so `tmp` may be on another filesystem. |
| `archive_timeout` | Seconds passed to `btty --timeout` for the archive commands (pack, unpack, merge). |
| `archive_max_bytes` | The largest batch moved as one archive, in bytes. |

`[viewer]` has one key, `command`: the viewer F3 runs, with its arguments,
split as a shell would split it. It has no default; without it F3 uses
`$PAGER`, else `less -R` (see [Viewing files](#viewing-files-f3)). For
example:

```toml
[viewer]
command = "bat --paging=always"
```

Other top-level tables are ignored. An unknown key in `[btty]` or `[viewer]`,
or a value of the wrong type, is an error that names the file and the key.
`command` in `[viewer]` must be a command of at least one word, without an
unclosed quote.

### Command-line flags

Flags override the config file.

| Flag | Effect |
|---|---|
| `--btty-exec` | turn exec mode on, even when the file says `exec = false` |
| `--no-btty-exec` | turn exec mode off (transfer-only), even when the file says `exec = true` |
| `--remote-home PATH` | the remote home, overriding `home` in the file (must be absolute) |
| `--config PATH` | the config file to read |
| `--version` | print the version and exit |

### The remote folder

The default remote folder is `/mnt/home0/wst/workspaces`, so `btty:` opens
there and `btty:NAME` opens a folder inside it. To start somewhere else, set
`home` in `[btty]`, pass `--remote-home PATH`, or give an absolute path after
`btty:` (see the location table under [Run](#run)).

## btty modes

### Exec mode (the default)

Exec mode is on by default. In exec mode twinfm runs shell commands on the
Beacon IDE through `btty --exec --allow-unrestricted`, as

```
btty --yes --no-launch --allow-unrestricted --json --timeout <seconds> --exec <script>
```

This is how it lists, stats, makes directories, deletes, renames and copies on
the remote, and how archive mode packs and unpacks there. Listing, stat and
the archive steps run small `python3` programs on the remote; making
directories, deleting, renaming and copying are plain `sh`. A btty panel in exec mode supports browsing and every file
operation except F4, and two btty panels can copy and move within the remote.

File transfers use btty's own single-file commands:

```
btty --yes --put <local file> --to <absolute remote path>
btty --yes --get <absolute remote path> --out <local temporary file>
```

To turn exec mode off, set `exec = false` in `[btty]` or pass
`--no-btty-exec`.

### Transfer-only mode

Without exec mode, twinfm only uses `--put` and `--get`, so only single-file
upload and download work. An upload is the same call as
`btty --yes --put "$file" --to <remote path>`.

- A btty panel cannot list directories. It shows a message instead of a
  listing.
- F5 in the btty panel asks for the remote path of one file and downloads it
  into the other panel's directory.
- F5 or F6 in the local panel uploads the selected files into the btty panel's
  directory. twinfm cannot check which remote files exist, so it first asks
  you to confirm that files with the same name will be replaced. Folders in
  the selection are reported as unsupported.
- Move out of the btty panel, rename, make directory and delete are not
  available.

### NODE_OPTIONS

btty can fail with the `NODE_OPTIONS` set in your shell, which is why it is
often run as `NODE_OPTIONS='' btty`. twinfm does this itself: every btty
command runs with `NODE_OPTIONS` set to the empty string in that command's
environment only.
The rest of the environment, PATH included, is passed through unchanged, and
twinfm's own environment is not changed.

## Copying and moving

Between two local panels, or between two btty panels, an item is copied or
moved within that store. Between a local panel and a btty panel, files go one
by one with `--put` and `--get`, unless archive mode applies.

When copying file by file between the stores, a symlink to a file is copied
as the file's content, and a symlink to a directory is skipped. A move deletes
each source file only after it arrived, and nothing skipped or failed is
deleted.

### Archive mode

Archive mode moves a whole selection as one gzip tar archive in a few btty
calls, instead of one call per file.

When it applies: F5 or F6 between a local panel and a btty panel in exec
mode, in either direction, when the selection holds a folder or several
items. The target dialog then has an "Archive folders and multiple items"
checkbox, checked by default. To transfer file by file instead, Tab to the
checkbox and press Space to uncheck it. A single file, two local panels, two
btty panels and transfer-only mode show no checkbox and never use archive
mode.

How it works:

1. Both sides are scanned before anything is written. Every existing
   destination file is asked about first (Overwrite, Skip, ...), and so is
   every clash between a file and a folder, so Cancel at that point writes
   nothing.
2. The selection is packed on the source side. The archive is transferred
   with one `--put` or `--get`, unpacked into a staging directory inside the
   receiving directory, and merged into place. The progress dialog shows the
   stage: Packing, Transferring, Unpacking, Merging. Esc cancels between
   stages.
3. Each selected item is merged on its own. For a move, an item's sources
   are deleted only after its own merge succeeded, and a skipped file and the
   folders holding it stay at the source.

Symlinks are kept as links. File modes (without setuid, setgid, sticky and
group or other write) and modification times are kept; ownership is not.
Special files such as FIFOs are skipped. Hard links between files of the
selection stay hard links; file by file, each becomes a separate copy. A
destination folder that is a symlink to a folder counts as a clash: archive
mode does not merge into it.

When a download is unpacked on your computer, twinfm refuses an archive
member with an absolute name, one that would land outside the staging
directory (through `..` or a symlink), and a hard link to a file outside it.
Nothing of such an archive is merged.

On both sides, unpacking also refuses a damaged archive: one that is cut
short, fails the gzip CRC or size check, has bytes after the compressed data
that are neither another gzip member nor zero padding, or holds a hard link
to a file the archive does not contain. Unpacking stops with an error and
removes what it had extracted, and nothing of the batch is merged.

Temporaries: the staging directory and the uploaded archive are named
`.twinfm-<hex>` and `.twinfm-<hex>.tgz` in the receiving directory. A remote
selection is packed into the `tmp` directory (default `/tmp`). Local archives
go into a fresh local temporary directory. Every temporary is removed, also
when the transfer fails or is cancelled. One that cannot be removed is listed
in the summary as "temporary file left behind".

Requirements and limits:

- The remote `python3` must have `tarfile.tar_filter` (Python 3.12 or later).
  Without it twinfm falls back to the file-by-file transfer automatically.
- A batch larger than `archive_max_bytes` (default 256 MiB), counted before
  packing or as the packed archive, goes file by file. btty holds a whole
  transfer in memory, which is the reason for the limit.
- Pack, unpack and merge run with `--timeout` set to `archive_timeout`
  (default 900 seconds). A longer run fails.
- twinfm also falls back to file by file when the remote command would be too
  long (very long paths), or when the scan finds a directory whose owner
  lacks read, write or search permission. When only the merge of a later item
  would be too long, the items merged before it stay merged, and that item
  and the ones after it go file by file.
- Packing, the transfer and unpacking each handle the whole batch at once. If
  one of them fails and Retry does not get past it, every item of the batch
  is reported failed with that error, nothing of the batch arrives, and a
  move deletes no source. So one file that pack cannot read (for example a
  file with mode 000) fails the whole batch. To transfer the rest, uncheck
  Archive: file by file, the readable items arrive and only that file fails.
- After a fallback the files go one by one with the rules under [Copying and
  moving](#copying-and-moving), and a question you already answered is not
  asked again.
- A destination that changes between the scan and the merge is replaced
  without asking.

## Listing cache and Ctrl-R

btty listings are cached. Each remote directory is listed once and then
shown from the cache (up to 256 directories, shared by both panels), which
keeps browsing fast. twinfm's own operations (copy, move, rename, make
directory, delete) drop the listings they change, so their results show at
once without a refresh.

Changes made on the remote by anything else, such as the IDE, git or a build,
are not seen until you press Ctrl-R. Leaving a directory and entering it again
still shows the cached listing. Ctrl-R in a panel drops the cached listings of
its directory and of every directory below it, then lists it again. In a
local panel Ctrl-R simply lists the directory again.

The checks before an operation (does the destination exist?) always ask the
remote and never use the cache.

## Viewing files (F3)

F3 shows the file on the cursor row of a local panel in a viewer: `command`
in the config file's `[viewer]` table (see [Config file](#config-file)) if
set, else `$PAGER` (unless it is empty or has an unclosed quote), else
`less -R`, else `more` when `less` is not on PATH. The command is split as a
shell would split it (so `PAGER="less -FRX"` works) and the file's absolute
path is added as the last argument; no shell is involved. twinfm is suspended
until the viewer exits, then lists the panel again with the cursor on the
file.

F3 views local files only. On a btty panel it says so; copy the file to a
local panel to view it. On a directory it shows a warning. If the viewer
cannot start you get an error naming it, and no other viewer is tried. A
non-zero exit status gives a warning.
Esc 3 and Option-3 work as F3. On a Mac, F3 often opens Mission Control
instead; use Esc 3 or Option-3 there.

## Editing files (F4)

F4 opens the file on the cursor row of a local panel in an editor: `$VISUAL`
if set, else `$EDITOR`, else `vim`. The value is split as a shell would split
it (so `EDITOR="code --wait"` works) and the file's absolute path is added.
twinfm is suspended until the editor exits, then lists the panel again with
the cursor on the file.

F4 edits local files only. On a btty panel it says so; copy the file to a
local panel to edit it. On a directory it shows a warning. If the editor
cannot start you get an error, and a non-zero exit status gives a warning.
Esc 4 and Option-4 work as F4.

## The shell (Ctrl-O)

Ctrl-O hides the panels and shows a local shell in the terminal, as Midnight
Commander does. Ctrl-O in the shell brings the panels back. The shell stays
alive in the background, so the next Ctrl-O returns to it as you left it.
Output it writes while hidden is shown then; after 1 MiB of it the shell
waits until you look.

The shell is `$SHELL -i`, else your login shell from the password database,
else `/bin/sh -i`. It is always a local shell, never a btty one. It starts at
the first Ctrl-O, in the active panel's directory when that panel is local,
else in the other panel's when that one is local, else in twinfm's working
directory.

- At each Ctrl-O, when the shell is idle at an empty prompt in another
  directory, twinfm types ` cd -- <directory>` into it to move it to the
  active local panel's directory. This works with sh, bash, zsh, ksh, dash
  and fish. The command is visible, and it stays out of the shell's history
  only if the shell ignores lines that start with a space (bash
  `ignorespace`, zsh `HIST_IGNORE_SPACE`). While a command runs, or a line is
  half typed, the shell is not moved.
- After a `cd` in the shell, Ctrl-O takes a local active panel to the
  shell's directory.
- With a btty panel active you get the same local shell, without moving it
  or the panel.
- If the shell exits (for example after `exit`), the panels come back with a
  notice, and the next Ctrl-O starts a new shell.
- While a command runs in the shell, quitting twinfm asks "A command is still
  running in the shell. Quit anyway?" first. Quitting ends the shell and the
  command.
- Ctrl-O always switches back to the panels, so programs in the shell never
  receive it.
- A full-screen program in the shell (such as `vim` or `less`) gets its
  alternate screen and bracketed paste mode back at the next Ctrl-O and is
  asked to redraw. Its mouse mode is not restored.
- Ctrl-O needs pseudo-terminals, so it does not work on Windows.

## Risks

Limitations of the btty side:

- Exec mode lets twinfm run arbitrary shell commands in your Beacon IDE
  (`--exec --allow-unrestricted`). Use `--no-btty-exec` or `exec = false` if
  you want only single-file transfers.
- twinfm passes `--yes` to every btty call, so btty never prompts and uses the
  newest candidate container. Exec commands also pass `--no-launch`, so btty
  fails instead of starting an IDE when none is running.
- Listing needs `python3` on the remote; there is no shell fallback. Archive
  mode needs Python 3.12 or later there (see [Archive mode](#archive-mode)).
- Each remote command must fit btty's command line limit. Remote paths
  longer than about 270 bytes cannot be listed, and archive mode falls back to
  file by file for long paths.
- btty holds a whole transfer in memory. Archive mode keeps batches under
  `archive_max_bytes`, but a single large file is still one transfer.
- If btty cannot reach the IDE (no container, credentials, network, a
  timeout), the operation fails with btty's message and the panel stays where
  it was.
- Transfer-only mode cannot see the remote: uploads replace same-named files
  after one confirmation, folders cannot be uploaded, and downloads need the
  file's remote path.
- The listing cache hides changes made outside twinfm until Ctrl-R. A
  directory reached through a symlink is not refreshed when it changes through
  its real path.
- In archive mode, a destination changed between the scan and the merge is
  replaced without asking.
- In archive mode, one file that pack cannot read (for example mode 000)
  fails the whole batch: nothing of it is transferred, and a move deletes no
  source. Uncheck Archive to transfer the readable items file by file.
- F3, F4 and the Ctrl-O shell block twinfm while the viewer, the editor or
  the shell has the terminal. Background operations carry on, and their
  questions wait until the panels are back.

## Development

Install the test dependencies (pip 25.1 or later reads the `dev` dependency
group) and run the tests:

```sh
.venv/bin/python -m pip install -U pip
.venv/bin/python -m pip install -e . --group dev
.venv/bin/python -m pytest
```

The tests never run the real btty. `tests/conftest.py` puts a `btty` shim
first on PATH that refuses to run, fails any test that reaches it, and reports
the number of blocked btty executions at the end of the run. Tests that need
btty use the fake runner in `tests/backend/fake_btty.py`.
