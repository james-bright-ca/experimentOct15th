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

The footer shows the main keys. You can also click them.

| Key | Action |
|---|---|
| Enter | open the directory on the cursor row (`..` goes up) |
| Backspace | go to the parent directory, with the cursor on the one you left |
| Tab | switch to the other panel |
| Space, Insert, Ctrl-T | select or deselect the cursor row and move down (`..` cannot be selected) |
| F4 | edit the file on the cursor row (local panels only, see [Editing files](#editing-files-f4)) |
| F5 | copy the selection, or the cursor row, to the other panel |
| F6 | move the selection, or the cursor row, to the other panel |
| Ctrl-N, Shift-F6 | rename the cursor row in place |
| F7 | make a directory |
| F8 | delete the selection, or the cursor row (directories recursively) |
| F9 | connect the panel to another location |
| F10 | quit |
| Ctrl-R | refresh the panel, past any cached listing (see [Listing cache](#listing-cache-and-ctrl-r)) |

The status line under each panel shows the number of items, or the number of
selected items and the total size of the selected files.

F5 and F6 ask for the target, prefilled with the other panel's directory. An
existing directory receives the items. With one item, a path that does not
exist yet becomes its new name. A relative target is taken from the source
directory when both panels are on the same store, and otherwise from the
other panel's directory.

F9 asks for a location as `KIND:PATH`, where `KIND` is `local` or `btty`. A
path without `KIND:` stays on the panel's current store, and an empty `PATH`
is that store's home directory (your home directory for `local`, the remote
home for `btty`).

### Esc and Option with a digit

If your F-keys do not reach the terminal (for example because they act as
media keys), use one of these instead. Each works on the main screen:

- Esc, then the digit: Esc 4 is F4, Esc 5 is F5, and so on to Esc 9 for F9,
  and Esc 0 is F10. You can type it slowly or quickly.
- Option with the digit, on a US keyboard layout: Option-4 to Option-9 act as
  F4 to F9 and Option-0 as F10. twinfm reads the character Option types
  (`¢ ∞ § ¶ • ª º`) as the digit. In a dialog's text field these characters
  are typed as text.

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
means the defaults. The file holds one `[btty]` table. Every key is optional,
and the values below are the defaults:

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

Other top-level tables are ignored. An unknown key in `[btty]`, or a value of
the wrong type, is an error that names the file and the key.

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
- F4 blocks twinfm while the editor runs. Background operations carry on, and
  their questions wait until the editor exits.

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
