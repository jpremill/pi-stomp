from __future__ import annotations

import pytest
from common.parameter import Parameter, PortInfo, Symbol
from modalapi.pedalboard import BPM_SYMBOL
from modalapi.plugin import Plugin
from modalapi.ws_protocol import LoadingEndMessage, LoadingStartMessage
from tests.types import SystemFixture
from uilib.parameterdialog import Parameterdialog


def _make_plugin(make_plugin, instance_id="fuzz", bypassed=False, binding=None) -> Plugin:
    gain_info: PortInfo = {"shortName": "Gain", "symbol": "gain", "ranges": {"minimum": 0.0, "maximum": 1.0}}
    gain_param = Parameter(gain_info, 0.5, binding, instance_id)
    plugin = make_plugin(instance_id, bypassed=bypassed, parameters={Symbol("gain"): gain_param})
    return plugin


def _install(v3_system: SystemFixture, make_plugin, instance_id="fuzz", binding=None) -> Plugin:
    handler = v3_system.handler
    hw = v3_system.hw
    assert handler.current
    plugin = _make_plugin(make_plugin, instance_id, bypassed=False, binding=binding)
    handler.current.pedalboard.plugins = [plugin]
    handler.lcd.link_data(handler.pedalboard_list, handler.current, hw.footswitches)
    handler.lcd.draw_main_panel()
    return plugin


def test_parameterdialog_nav_turn_updates_value_and_sends_ws(v3_system: SystemFixture, make_plugin):
    """When a parameter is edited in Parameterdialog with NAV encoder, it updates value and queues param_set."""
    plugin = _install(v3_system, make_plugin)
    param = plugin.parameters[Symbol("gain")]
    dialog = v3_system.handler.lcd.draw_parameter_dialog(param)
    assert isinstance(dialog, Parameterdialog)
    assert param.value == 0.5

    # Turn NAV forward
    dialog.input_step(1, 1)
    new_val = param.value
    assert new_val > 0.5

    # Outbound queue should have param_set
    assert len(v3_system.ws_bridge.sent) > 0
    msg = v3_system.ws_bridge.sent[-1]
    assert msg.startswith("param_set /graph/fuzz/gain")

    # The confirmed value should be updated because MOD-UI suppresses WebSocket echoes to sender
    assert param._confirmed == pytest.approx(new_val, abs=1e-4)


def test_parameterdialog_rollback_when_loading_is_active(v3_system: SystemFixture, make_plugin):
    """If _is_pedalboard_loading is True, sink returns False and parameter reverts to confirmed."""
    plugin = _install(v3_system, make_plugin)
    param = plugin.parameters[Symbol("gain")]
    dialog = v3_system.handler.lcd.draw_parameter_dialog(param)
    assert isinstance(dialog, Parameterdialog)
    assert param.value == 0.5

    # Simulate loading state stuck True
    v3_system.handler._is_pedalboard_loading = True

    dialog.input_step(1, 1)

    # Because loading is active, commit rolls back to 0.5
    assert param.value == 0.5
    assert dialog.last_param_value == 0.5


def test_midi_cc_bound_param_confirms_on_cc_emit(v3_system: SystemFixture, make_plugin):
    """For a MIDI CC-bound parameter, _publish_cc emits CC. Because mod-host emits no echo
    for CC, a successful CC emit MUST confirm the value so it does not stay unconfirmed."""
    hw = v3_system.hw
    enc1 = next(e for e in hw.encoders if e.id == 1)
    binding = f"{enc1.midi_channel}:{enc1.midi_CC}"
    hw.controllers[binding] = enc1

    plugin = _install(v3_system, make_plugin, binding=binding)
    param = plugin.parameters[Symbol("gain")]
    enc1.bind_to_parameter(param)
    dialog = v3_system.handler.lcd.draw_parameter_dialog(param)
    assert isinstance(dialog, Parameterdialog)

    # Initial state
    assert param.value == 0.5
    assert param._confirmed == 0.5

    # Turn encoder in dialog
    dialog.input_step(1, 1)
    new_val = param.value
    assert new_val > 0.5

    # Verify MIDI CC was emitted
    hw.midiout.send_message.assert_called()

    # The confirmed value should be updated because CC emit has no remote echo
    assert param._confirmed == pytest.approx(new_val, abs=1e-4)


def test_bpm_param_confirms_on_successful_emission(v3_system: SystemFixture):
    """Transport BPM publishes via set_mod_tap_tempo. Because MOD-UI emits no echo
    to the sender, a successful send must confirm the parameter."""
    handler = v3_system.handler
    assert handler.current is not None
    time_info = {
        "available": 0x7,
        "bpb": 4.0,
        "bpbCC": {"channel": -1, "control": 0},
        "bpm": 120.0,
        "bpmCC": {"channel": -1, "control": 0},
        "rolling": False,
        "rollingCC": {"channel": -1, "control": 0},
    }
    handler.current.pedalboard.transport_plugin = handler.current.pedalboard._build_transport_plugin(time_info)
    bpm_param = handler.current.pedalboard.transport_plugin.parameters[BPM_SYMBOL]
    assert bpm_param._confirmed == 120.0

    handler.parameter_value_commit(bpm_param, 135.0)

    assert bpm_param.value == 135.0
    assert bpm_param._confirmed == 135.0


def test_parameterdialog_rollback_reverts_to_last_successful_edit(v3_system: SystemFixture, make_plugin):
    """When an edit succeeds, confirmed state advances. If a subsequent edit fails,
    it rolls back to the last confirmed edit, NOT the initial boot/load value."""
    plugin = _install(v3_system, make_plugin)
    param = plugin.parameters[Symbol("gain")]
    dialog = v3_system.handler.lcd.draw_parameter_dialog(param)
    assert isinstance(dialog, Parameterdialog)
    assert param.value == 0.5
    assert param._confirmed == 0.5

    # Step 1: Successful edit
    dialog.input_step(1, 1)
    step1_val = param.value
    assert step1_val > 0.5
    assert param._confirmed == pytest.approx(step1_val, abs=1e-4)

    # Step 2: Failed edit (e.g. transient backpressure or loading)
    v3_system.handler._is_pedalboard_loading = True
    dialog.input_step(1, 1)

    # Value rolls back to step 1's confirmed value, not 0.5
    assert param.value == pytest.approx(step1_val, abs=1e-4)
    assert param._confirmed == pytest.approx(step1_val, abs=1e-4)


def test_audio_parameter_confirms_on_alsa_emission(v3_system: SystemFixture):
    """Audio card parameters (ALSA) write locally with no remote echo and must confirm on send."""
    handler = v3_system.handler
    info: PortInfo = {"shortName": "Master Vol", "symbol": "master_volume", "ranges": {"minimum": -60.0, "maximum": 12.0}}
    param = Parameter(info, 0.0, None, None)  # instance_id=None routes to _publish_audio
    assert param._confirmed == 0.0

    handler.parameter_value_commit(param, -6.0)

    assert param.value == -6.0
    assert param._confirmed == -6.0


def test_loading_end_resets_is_pedalboard_loading_allowing_param_edits(v3_system: SystemFixture, make_plugin):
    """When MOD-UI sends loading_start followed by loading_end (e.g. WebSocket connect dump or snapshot load),
    _is_pedalboard_loading must reset to False so subsequent parameter edits succeed and do not roll back."""
    handler = v3_system.handler
    plugin = _install(v3_system, make_plugin)
    param = plugin.parameters[Symbol("gain")]
    dialog = handler.lcd.draw_parameter_dialog(param)
    assert isinstance(dialog, Parameterdialog)

    # MOD-UI sends connect dump / snapshot loading
    handler._handle_ws_message(LoadingStartMessage(is_default=False))
    assert handler._is_pedalboard_loading is True

    # Loading ends
    handler._handle_ws_message(LoadingEndMessage(snapshot_id=1))
    assert handler._is_pedalboard_loading is False

    # Now turn NAV encoder: edit MUST succeed and commit over WebSocket
    dialog.input_step(1, 1)
    new_val = param.value
    assert new_val > 0.5
    assert param._confirmed == pytest.approx(new_val, abs=1e-4)

    # WebSocket bridge must have sent the param_set message
    assert len(v3_system.ws_bridge.sent) > 0
    assert v3_system.ws_bridge.sent[-1].startswith("param_set /graph/fuzz/gain")

