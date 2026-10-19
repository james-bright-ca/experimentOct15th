"""Modal dialogs. Each is a ModalScreen whose result reaches the callback
given to ``push_screen``: text input, the transfer target with the Archive
checkbox, yes/no confirmation, the per-file conflict and error questions,
progress, and the end-of-batch failure summary.

Left/Right (and Tab) move between buttons, Enter presses the focused one and
Esc gives the dialog's safe answer.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from textual.app import ComposeResult
from textual.await_complete import AwaitComplete
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, Static

from twinfm.backend.base import Entry, FsError
from twinfm.ops.model import Conflict, OnError, Progress, Report

__all__ = [
    "ConfirmDialog",
    "ConflictDialog",
    "Dialog",
    "ErrorDialog",
    "InputDialog",
    "ProgressDialog",
    "SummaryDialog",
    "TransferDialog",
]


class Dialog[T](ModalScreen[T]):
    """A centred box with a title, a body and a row of buttons. Pressing a
    button dismisses the dialog with the answer registered for its id."""

    DEFAULT_CSS = """
    Dialog {
        align: center middle;
    }
    Dialog > Vertical {
        width: 76;
        max-width: 95%;
        height: auto;
        max-height: 90%;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    Dialog .title {
        text-style: bold;
        width: 100%;
        margin-bottom: 1;
    }
    Dialog .buttons {
        height: auto;
        width: 100%;
        align-horizontal: center;
        margin-top: 1;
    }
    Dialog Button {
        margin: 0 1;
    }
    """

    AUTO_FOCUS = ".default"

    BINDINGS = [
        Binding("left", "app.focus_previous", show=False),
        Binding("right", "app.focus_next", show=False),
    ]

    def __init__(self, title: str) -> None:
        super().__init__()
        self._title = title
        self._answers: dict[str, T] = {}
        self._dismissed = False

    def dismiss(self, result: T | None = None) -> AwaitComplete:
        """Close the dialog with ``result``. Only the first call counts: a
        second one would pop whatever screen lies below the dialog."""
        if self._dismissed:
            return AwaitComplete.nothing()
        self._dismissed = True
        return super().dismiss(result)

    def body(self) -> ComposeResult:
        """The widgets between the title and the buttons."""
        yield from ()

    def buttons(self) -> list[tuple[str, str, T]]:
        """(id, label, answer) for each button, left to right."""
        return []

    default_button = ""
    """The id of the button focused when the dialog opens."""

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self._title, classes="title", markup=False)
            yield from self.body()
            buttons = self.buttons()
            if buttons:
                with Horizontal(classes="buttons"):
                    for button_id, label, answer in buttons:
                        self._answers[button_id] = answer
                        classes = "default" if button_id == self.default_button else ""
                        yield Button(label, id=button_id, classes=classes, compact=True)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id in self._answers:
            self.dismiss(self._answers[event.button.id])


class InputDialog(Dialog[str | None]):
    """Asks for one line of text; the result is the text, or None if cancelled.

    With ``select`` the prefilled text starts selected, so typing replaces
    it; otherwise the cursor starts at its end.
    """

    AUTO_FOCUS = "Input"
    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(self, title: str, prompt: str, value: str = "", *, select: bool = True) -> None:
        super().__init__(title)
        self._prompt = prompt
        self._input = Input(value=value, select_on_focus=select)

    def body(self) -> ComposeResult:
        yield Label(self._prompt, markup=False)
        yield self._input

    def buttons(self) -> list[tuple[str, str, str | None]]:
        return [("ok", "OK", None), ("cancel", "Cancel", None)]

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.dismiss(self._input.value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        event.prevent_default()  # Textual would run Dialog.on_button_pressed as well
        self.dismiss(self._input.value if event.button.id == "ok" else None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class TransferDialog(Dialog[tuple[str, bool] | None]):
    """The target of a copy or move, as InputDialog asks for it, with the
    Archive checkbox below the input, checked. The result is ``(text,
    archive)``, or None if cancelled. Enter in the input accepts; on the
    checkbox Enter and Space toggle it."""

    AUTO_FOCUS = "Input"
    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(self, title: str, prompt: str, value: str = "") -> None:
        super().__init__(title)
        self._prompt = prompt
        self._input = Input(value=value)
        self._archive = Checkbox("Archive folders and multiple items", value=True, compact=True)

    def body(self) -> ComposeResult:
        yield Label(self._prompt, markup=False)
        yield self._input
        yield self._archive

    def buttons(self) -> list[tuple[str, str, tuple[str, bool] | None]]:
        return [("ok", "OK", None), ("cancel", "Cancel", None)]

    def _accept(self) -> None:
        self.dismiss((self._input.value, self._archive.value))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._accept()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        event.prevent_default()  # Textual would run Dialog.on_button_pressed as well
        if event.button.id == "ok":
            self._accept()
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmDialog(Dialog[bool]):
    """A yes/no question. ``default_yes`` decides which button has focus
    first; Esc and N answer no, Y answers yes."""

    BINDINGS = [
        Binding("escape,n", "answer(False)", "No", show=False),
        Binding("y", "answer(True)", "Yes", show=False),
    ]

    def __init__(
        self, title: str, message: str, *, yes: str = "Yes", no: str = "No", default_yes: bool = False
    ) -> None:
        super().__init__(title)
        self._message = message
        self._labels = (yes, no)
        self.default_button = "yes" if default_yes else "no"

    def body(self) -> ComposeResult:
        yield Static(self._message, markup=False)

    def buttons(self) -> list[tuple[str, str, bool]]:
        return [("yes", self._labels[0], True), ("no", self._labels[1], False)]

    def action_answer(self, answer: bool) -> None:
        self.dismiss(answer)


def _describe(entry: Entry) -> str:
    size = "?" if entry.size is None else f"{entry.size:,} bytes"
    when = "" if entry.mtime is None else time.strftime(", %Y-%m-%d %H:%M", time.localtime(entry.mtime))
    return f"{size}{when}"


class ConflictDialog(Dialog[Conflict]):
    """The destination file already exists. Focus starts on Skip; Esc cancels the batch."""

    default_button = "skip"
    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(self, src: Entry, dst: Entry) -> None:
        super().__init__("File exists")
        self._src = src
        self._dst = dst

    def body(self) -> ComposeResult:
        yield Static(
            f"{self._dst.path}\n\n"
            f"New:      {_describe(self._src)}\n"
            f"Existing: {_describe(self._dst)}",
            markup=False,
        )

    def buttons(self) -> list[tuple[str, str, Conflict]]:
        return [
            ("overwrite", "Overwrite", Conflict.OVERWRITE),
            ("skip", "Skip", Conflict.SKIP),
            ("overwrite_all", "Overwrite all", Conflict.OVERWRITE_ALL),
            ("skip_all", "Skip all", Conflict.SKIP_ALL),
            ("cancel", "Cancel", Conflict.CANCEL),
        ]

    def action_cancel(self) -> None:
        self.dismiss(Conflict.CANCEL)


class ErrorDialog(Dialog[OnError]):
    """An item failed. Focus starts on Skip; Esc aborts the batch."""

    default_button = "skip"
    BINDINGS = [Binding("escape", "abort", "Abort", show=False)]

    def __init__(self, path: str, err: FsError) -> None:
        super().__init__("Error")
        self._path = path
        self._err = err

    def body(self) -> ComposeResult:
        yield Static(f"{self._err.message}\n{self._path}", markup=False)

    def buttons(self) -> list[tuple[str, str, OnError]]:
        return [
            ("retry", "Retry", OnError.RETRY),
            ("skip", "Skip", OnError.SKIP),
            ("skip_all", "Skip all", OnError.SKIP_ALL),
            ("abort", "Abort", OnError.ABORT),
        ]

    def action_abort(self) -> None:
        self.dismiss(OnError.ABORT)


class ProgressDialog(Dialog[None]):
    """Shows the item in progress, or the stage of an archive transfer
    (``Progress.stage``). Esc calls ``on_cancel`` once: the batch stops
    after the current file. The owner dismisses it when the batch ends."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(self, title: str, on_cancel: Callable[[], None]) -> None:
        super().__init__(title)
        self._on_cancel = on_cancel
        self._current = Static("Starting...", markup=False)
        self._hint = Static("Esc: cancel after the current file", markup=False)

    def body(self) -> ComposeResult:
        yield self._current
        yield self._hint

    def update_progress(self, p: Progress) -> None:
        if p.stage is not None:
            self._current.update(f"{p.stage}\n{p.current}")
        else:
            self._current.update(f"{p.items_done} of {p.items_total} done\n{p.current}")

    def action_cancel(self) -> None:
        self._on_cancel()
        self._hint.update("Cancelling after the current file...")


class SummaryDialog(Dialog[None]):
    """What failed in a batch (and how much was skipped)."""

    DEFAULT_CSS = """
    SummaryDialog VerticalScroll {
        height: auto;
        max-height: 20;
    }
    """

    default_button = "ok"
    BINDINGS = [Binding("escape", "close", "Close", show=False)]

    def __init__(self, title: str, report: Report) -> None:
        super().__init__(title)
        self._report = report

    def body(self) -> ComposeResult:
        report = self._report
        lines = [f"{len(report.failed)} failed:"]
        lines += [f"  {path}: {err.message}" for path, err in report.failed]
        if report.skipped:
            lines.append(f"{len(report.skipped)} skipped.")
        if report.cancelled:
            lines.append("The batch stopped early.")
        with VerticalScroll():
            yield Static("\n".join(lines), markup=False)

    def buttons(self) -> list[tuple[str, str, None]]:
        return [("ok", "OK", None)]

    def action_close(self) -> None:
        self.dismiss(None)
