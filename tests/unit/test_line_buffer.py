"""Unit tests for LineBuffer — cursor-aware line editing."""
import pytest
from leap.utils.line_buffer import LineBuffer


def test_initial_empty() -> None:
    lb = LineBuffer()
    assert lb.text == ""
    assert lb.pos == 0


def test_initial_with_text() -> None:
    lb = LineBuffer("hello")
    assert lb.text == "hello"
    assert lb.pos == 5  # cursor at end


def test_insert_at_end() -> None:
    lb = LineBuffer("hi")
    lb.insert("!")
    assert lb.text == "hi!"
    assert lb.pos == 3


def test_insert_at_start() -> None:
    lb = LineBuffer("hi")
    lb.home()
    lb.insert("X")
    assert lb.text == "Xhi"
    assert lb.pos == 1


def test_insert_in_middle() -> None:
    lb = LineBuffer("helo")
    lb.home()
    lb.move_right()
    lb.move_right()
    lb.insert("l")
    assert lb.text == "hello"
    assert lb.pos == 3


def test_backspace_at_end() -> None:
    lb = LineBuffer("hello")
    lb.backspace()
    assert lb.text == "hell"
    assert lb.pos == 4


def test_backspace_in_middle() -> None:
    lb = LineBuffer("helo")
    lb.home()
    lb.move_right()
    lb.move_right()
    lb.move_right()
    lb.backspace()
    assert lb.text == "heo"
    assert lb.pos == 2


def test_backspace_at_start_noop() -> None:
    lb = LineBuffer("hi")
    lb.home()
    lb.backspace()
    assert lb.text == "hi"
    assert lb.pos == 0


def test_delete_at_cursor() -> None:
    lb = LineBuffer("hello")
    lb.home()
    lb.move_right()
    lb.delete()
    assert lb.text == "hllo"
    assert lb.pos == 1


def test_delete_at_end_noop() -> None:
    lb = LineBuffer("hi")
    lb.delete()
    assert lb.text == "hi"
    assert lb.pos == 2


def test_move_left_clamps() -> None:
    lb = LineBuffer("hi")
    lb.home()
    lb.move_left()
    assert lb.pos == 0


def test_move_right_clamps() -> None:
    lb = LineBuffer("hi")
    lb.move_right()
    assert lb.pos == 2


def test_home_end() -> None:
    lb = LineBuffer("hello")
    lb.home()
    assert lb.pos == 0
    lb.end()
    assert lb.pos == 5


def test_clear() -> None:
    lb = LineBuffer("hello")
    lb.home()
    lb.move_right()
    lb.clear()
    assert lb.text == ""
    assert lb.pos == 0


def test_delete_word_from_end() -> None:
    lb = LineBuffer("foo bar")
    lb.delete_word()
    assert lb.text == "foo "
    assert lb.pos == 4


def test_delete_word_strips_trailing_spaces() -> None:
    lb = LineBuffer("foo   ")
    lb.delete_word()
    assert lb.text == ""
    assert lb.pos == 0


def test_delete_word_from_middle() -> None:
    lb = LineBuffer("foo bar baz")
    lb.home()
    for _ in range(7):  # cursor after "foo bar"
        lb.move_right()
    lb.delete_word()
    assert lb.text == "foo  baz"
    assert lb.pos == 4


def test_delete_word_at_start_noop() -> None:
    lb = LineBuffer("hello")
    lb.home()
    lb.delete_word()
    assert lb.text == "hello"
    assert lb.pos == 0


def test_quoted_flag_value_insert() -> None:
    """Simulate typing --model opus[1m] character by character."""
    lb = LineBuffer()
    for ch in '--model opus[1m]':
        lb.insert(ch)
    assert lb.text == '--model opus[1m]'
    assert lb.pos == 16


def test_cursor_navigation_sequence() -> None:
    """Type 'abc', move left twice, insert 'X', result is 'aXbc'."""
    lb = LineBuffer()
    for ch in 'abc':
        lb.insert(ch)
    lb.move_left()
    lb.move_left()
    lb.insert('X')
    assert lb.text == 'aXbc'
    assert lb.pos == 2
