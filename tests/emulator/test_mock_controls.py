"""Soundness checks for the emulator's mock controls.

These exercise the same superclass attributes the real hardware reads (toggled,
midi_CC, midi_value, value) so a field rename on Footswitch / Encoder /
AnalogControl fails here instead of silently in the emulator window."""

from unittest.mock import MagicMock

import pistomp.switchstate as switchstate
from pistomp.input.event import EncoderEvent, SwitchEvent, SwitchEventKind

from emulator.controls import (
    MockAnalogControl,
    MockEncoder,
    MockEncoderMidi,
    MockFootswitch,
)


def _make_sink():
    sink = MagicMock()
    sink.handle.return_value = True
    return sink


# ---------------------------------------------------------------------------
# MockFootswitch
# ---------------------------------------------------------------------------


def _make_fs(midi_CC: int | None = 60, midi_channel: int = 0):
    refresh = MagicMock()
    fs = MockFootswitch(id=1, midi_CC=midi_CC, midi_channel=midi_channel, refresh_callback=refresh)
    return fs, refresh


def test_footswitch_press_toggles_and_calls_refresh():
    fs, refresh = _make_fs(midi_CC=60, midi_channel=0)
    assert fs.toggled is False

    fs.press()

    assert fs.toggled is True
    refresh.assert_called_once_with(footswitch=fs)


def test_footswitch_press_twice_toggles_off():
    fs, _ = _make_fs(midi_CC=61, midi_channel=5)

    fs.press()
    fs.press()

    assert fs.toggled is False


def test_footswitch_without_midi_cc_still_toggles_and_calls_refresh():
    fs, refresh = _make_fs(midi_CC=None)

    fs.press()

    assert fs.toggled is True
    refresh.assert_called_once_with(footswitch=fs)


def _make_dispatching_fs(midi_CC: int | None = 60, midi_channel: int = 0):
    fs, refresh = _make_fs(midi_CC=midi_CC, midi_channel=midi_channel)
    sink = _make_sink()
    fs.sink = sink
    return fs, refresh, sink


def test_footswitch_starts_released():
    fs, _refresh, _sink = _make_dispatching_fs()
    assert fs.press_state is switchstate.Value.RELEASED
    assert fs._press_time is None


def test_footswitch_press_down_marks_pressed_without_dispatch():
    fs, refresh, sink = _make_dispatching_fs()
    fs.press_down(timestamp=100.0)

    assert fs.press_state is switchstate.Value.PRESSED
    sink.handle.assert_not_called()


def test_footswitch_short_press_dispatches_press_event():
    fs, refresh, sink = _make_dispatching_fs()

    fs.press_down(timestamp=100.0)
    fs.press_up()

    sink.handle.assert_called_once()
    event = sink.handle.call_args.args[0]
    assert isinstance(event, SwitchEvent)
    assert event.kind == SwitchEventKind.PRESS
    assert event.timestamp == 100.0
    assert fs.press_state is switchstate.Value.RELEASED


def test_footswitch_hold_matures_to_longpress_then_release_is_silent():
    fs, refresh, sink = _make_dispatching_fs()

    fs.LONG_PRESS_TIME = 0
    fs.press_down(timestamp=100.0)
    fs.poll()  # PRESSED held past threshold -> LONGPRESSED

    sink.handle.assert_called_once()
    event = sink.handle.call_args.args[0]
    assert isinstance(event, SwitchEvent)
    assert event.kind == SwitchEventKind.LONGPRESS
    assert fs.press_state is switchstate.Value.LONGPRESSED

    sink.handle.reset_mock()
    fs.press_up()  # LONGPRESSED release clears silently, no second event
    assert fs.press_state is switchstate.Value.RELEASED
    sink.handle.assert_not_called()


def test_footswitch_repress_after_release_rearms():
    fs, refresh, sink = _make_dispatching_fs()

    fs.LONG_PRESS_TIME = 0
    fs.press_down(timestamp=100.0)
    fs.press_up()
    sink.handle.reset_mock()

    fs.press_down(timestamp=200.0)
    assert fs.press_state is switchstate.Value.PRESSED
    fs.poll()
    assert fs.press_state is switchstate.Value.LONGPRESSED
    assert sink.handle.call_args.args[0].kind == SwitchEventKind.LONGPRESS


def test_two_mock_footswitches_resolve_a_chord():
    """End-to-end: held mocks emit LONGPRESS SwitchEvents that, run through the
    same observe/tick path modhandler uses (LONGPRESS no-row → observe, then
    tick), resolve as a chord — the capability the old arg-less press() lacked."""
    from pistomp.footswitch_chords import FootswitchChords

    fired: list = []
    chords = FootswitchChords()
    chords.rebuild({"chord_action": lambda: fired.append("chord_action")})

    fs1 = MockFootswitch(id=1, midi_CC=60, midi_channel=0, refresh_callback=MagicMock())
    fs2 = MockFootswitch(id=2, midi_CC=61, midi_channel=0, refresh_callback=MagicMock())
    for fs in (fs1, fs2):
        fs.sink = _make_sink()
        fs.LONG_PRESS_TIME = 0
        fs.set_longpress_groups(["chord_action"])
        chords.register(["chord_action"])

    def on_longpress(sink):
        chords.observe(sink.handle.call_args.args[0].controller,
                       sink.handle.call_args.args[0].timestamp)

    fs1.press_down(timestamp=100.0)
    fs2.press_down(timestamp=100.0)
    fs1.poll()
    on_longpress(fs1.sink)
    fs2.poll()
    on_longpress(fs2.sink)

    names = chords.tick()
    assert names == ["chord_action"]

    # The resolver hands names back; the handler (modhandler._tick_chords) is
    # what runs them. Confirm the callback fires for the resolved chord.
    for name in names:
        chords.callbacks[name]()
    assert fired == ["chord_action"]


# ---------------------------------------------------------------------------
# MockEncoder (nav / volume — no MIDI)
# ---------------------------------------------------------------------------


def test_nav_encoder_step_dispatches_to_sink():
    enc = MockEncoder(type="NAV", id=0)
    sink = _make_sink()
    enc.sink = sink

    enc.step(1)
    enc.step(-1)

    assert sink.handle.call_count == 2
    calls = sink.handle.call_args_list
    assert isinstance(calls[0].args[0], EncoderEvent)
    assert calls[0].args[0].rotations == 1
    assert calls[1].args[0].rotations == -1


def test_nav_encoder_step_zero_is_a_noop():
    enc = MockEncoder()
    sink = _make_sink()
    enc.sink = sink

    enc.step(0)

    sink.handle.assert_not_called()


def test_nav_encoder_press_dispatches_switch_event():
    enc = MockEncoder(type="NAV", id=0)
    sink = _make_sink()
    enc.sink = sink

    enc.press(switchstate.Value.RELEASED)

    sink.handle.assert_called_once()
    event = sink.handle.call_args.args[0]
    assert isinstance(event, SwitchEvent)
    assert event.kind == SwitchEventKind.PRESS
    assert event.timestamp > 0.0


def test_nav_encoder_longpress_dispatches_longpress_event():
    enc = MockEncoder(type="NAV", id=0)
    sink = _make_sink()
    enc.sink = sink

    enc.press(switchstate.Value.LONGPRESSED)

    sink.handle.assert_called_once()
    event = sink.handle.call_args.args[0]
    assert isinstance(event, SwitchEvent)
    assert event.kind == SwitchEventKind.LONGPRESS
    assert event.timestamp > 0.0


# ---------------------------------------------------------------------------
# MockEncoderMidi (tweak encoders)
# ---------------------------------------------------------------------------


def _make_enc_midi(midi_CC=70, midi_channel=0):
    enc = MockEncoderMidi(
        midi_channel=midi_channel, midi_CC=midi_CC, type="TWEAK", id=1
    )
    enc.sink = _make_sink()
    return enc


def test_tweak_encoder_step_dispatches_to_sink():
    enc = _make_enc_midi(midi_CC=70, midi_channel=0)
    sink = _make_sink()
    enc.sink = sink

    enc.step(1)

    sink.handle.assert_called_once()
    event = sink.handle.call_args.args[0]
    assert isinstance(event, EncoderEvent)


# ---------------------------------------------------------------------------
# MockAnalogControl (expression pedal)
# ---------------------------------------------------------------------------


def test_analog_control_set_value():
    ctrl = MockAnalogControl(midi_CC=75, midi_channel=3)
    ctrl.set_value(42)

    assert ctrl.value == 42
