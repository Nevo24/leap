"""Cursor-aware line editing buffer used by raw-terminal input prompts."""


class LineBuffer:
    """Mutable text buffer with a cursor position for line editing."""

    def __init__(self, initial: str = "") -> None:
        self.buf: list[str] = list(initial)
        self.pos: int = len(self.buf)

    @property
    def text(self) -> str:
        return "".join(self.buf)

    def insert(self, ch: str) -> None:
        self.buf.insert(self.pos, ch)
        self.pos += 1

    def backspace(self) -> None:
        if self.pos > 0:
            self.buf.pop(self.pos - 1)
            self.pos -= 1

    def delete(self) -> None:
        if self.pos < len(self.buf):
            self.buf.pop(self.pos)

    def move_left(self) -> None:
        self.pos = max(0, self.pos - 1)

    def move_right(self) -> None:
        self.pos = min(len(self.buf), self.pos + 1)

    def home(self) -> None:
        self.pos = 0

    def end(self) -> None:
        self.pos = len(self.buf)

    def clear(self) -> None:
        self.buf.clear()
        self.pos = 0

    def delete_word(self) -> None:
        while self.pos > 0 and self.buf[self.pos - 1] == " ":
            self.buf.pop(self.pos - 1)
            self.pos -= 1
        while self.pos > 0 and self.buf[self.pos - 1] != " ":
            self.buf.pop(self.pos - 1)
            self.pos -= 1
