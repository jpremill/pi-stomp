"""Integration & edge-case tests for live MOD-UI Web UI parameter, bypass, snapshot/preset, plugin add/remove, and patch_set rename updates on piStomp LCD display."""

from unittest.mock import MagicMock
from common.parameter import Symbol
from tests.v3.conftest import SystemFixture
from uilib.panel import Panel
from uilib.box import Box
from plugins.nam import NAM_URIS


class DummyEditorPanel(Panel):
    pass


def test_poll_ws_messages_param_set_triggers_coalesced_lcd_draw_main_panel(
    v3_system: SystemFixture, make_plugin, make_parameter
):
    """Multiple inbound param_set WS messages in a tick update values and trigger a single draw_main_panel call."""
    handler = v3_system.handler
    ws_bridge = v3_system.ws_bridge
    assert handler.current is not None

    gain_param = make_parameter("gain", "noise", value=0.5)
    plugin = make_plugin("noise", bypassed=False, parameters={Symbol("gain"): gain_param})
    handler.current.pedalboard.plugins = [plugin]

    handler.lcd.draw_main_panel = MagicMock()

    # Inject multiple parameter updates in one batch
    ws_bridge.inject(f"param_set /graph/{plugin.instance_id} gain 0.75")
    ws_bridge.inject(f"param_set /graph/{plugin.instance_id} gain 0.80")
    handler.poll_ws_messages()

    # Parameter updated to latest value
    assert plugin.parameters[Symbol("gain")].value == 0.80

    # Triggered exactly 1 coalesced LCD redraw call
    handler.lcd.draw_main_panel.assert_called_once()


def test_poll_ws_messages_plugin_bypass_triggers_lcd_draw_main_panel(v3_system: SystemFixture, make_plugin):
    """Inbound plugin bypass toggle from Web UI updates state and triggers draw_main_panel."""
    handler = v3_system.handler
    ws_bridge = v3_system.ws_bridge
    assert handler.current is not None

    plugin = make_plugin("noise", bypassed=False)
    handler.current.pedalboard.plugins = [plugin]

    handler.lcd.draw_main_panel = MagicMock()

    # Inject bypass toggle message from Web UI
    ws_bridge.inject(f"param_set /graph/{plugin.instance_id} :bypass 1.0")
    handler.poll_ws_messages()

    assert plugin.is_bypassed() is True
    handler.lcd.draw_main_panel.assert_called_once()


def test_poll_ws_messages_duplicate_value_suppresses_lcd_draw_main_panel(
    v3_system: SystemFixture, make_plugin, make_parameter
):
    """Inbound param_set WS message matching current value does not trigger unnecessary LCD redraw."""
    handler = v3_system.handler
    ws_bridge = v3_system.ws_bridge
    assert handler.current is not None

    gain_param = make_parameter("gain", "noise", value=0.5)
    plugin = make_plugin("noise", bypassed=False, parameters={Symbol("gain"): gain_param})
    handler.current.pedalboard.plugins = [plugin]

    handler.lcd.draw_main_panel = MagicMock()

    # Inject duplicate value message (0.5 -> 0.5)
    ws_bridge.inject(f"param_set /graph/{plugin.instance_id} gain 0.5")
    handler.poll_ws_messages()

    # Value unchanged, draw_main_panel suppressed
    handler.lcd.draw_main_panel.assert_not_called()


def test_poll_ws_messages_suppressed_during_pedalboard_loading(
    v3_system: SystemFixture, make_plugin, make_parameter
):
    """WS parameter messages arriving during pedalboard loading update in-memory state but suppress draw_main_panel."""
    handler = v3_system.handler
    ws_bridge = v3_system.ws_bridge
    assert handler.current is not None

    gain_param = make_parameter("gain", "noise", value=0.5)
    plugin = make_plugin("noise", bypassed=False, parameters={Symbol("gain"): gain_param})
    handler.current.pedalboard.plugins = [plugin]

    # Simulate active pedalboard loading
    handler._is_pedalboard_loading = True
    handler.lcd.draw_main_panel = MagicMock()

    ws_bridge.inject(f"param_set /graph/{plugin.instance_id} gain 0.90")
    handler.poll_ws_messages()

    # Parameter updated in memory
    assert plugin.parameters[Symbol("gain")].value == 0.90
    # LCD redraw suppressed so "Loading..." screen remains intact
    handler.lcd.draw_main_panel.assert_not_called()


def test_poll_ws_messages_suppressed_when_editor_panel_open(
    v3_system: SystemFixture, make_plugin, make_parameter
):
    """WS parameter messages arriving while an editor panel is active do NOT reset the LCD to main_panel."""
    handler = v3_system.handler
    ws_bridge = v3_system.ws_bridge
    assert handler.current is not None and handler.lcd is not None

    gain_param = make_parameter("gain", "noise", value=0.5)
    plugin = make_plugin("noise", bypassed=False, parameters={Symbol("gain"): gain_param})
    handler.current.pedalboard.plugins = [plugin]

    # Push a real Panel instance onto pstack
    dummy_panel = DummyEditorPanel(box=Box.xywh(0, 0, 320, 240))
    handler.lcd.pstack.push_panel(dummy_panel, refresh=False)
    assert handler.lcd.is_main_panel_active() is False

    handler.lcd.draw_main_panel = MagicMock()

    ws_bridge.inject(f"param_set /graph/{plugin.instance_id} gain 0.95")
    handler.poll_ws_messages()

    # In-memory parameter updated
    assert plugin.parameters[Symbol("gain")].value == 0.95
    # Main panel draw suppressed so open editor panel is not closed/nuked
    handler.lcd.draw_main_panel.assert_not_called()


def test_live_update_bypass_lcd_snapshots(v3_system: SystemFixture, make_plugin, make_parameter, snapshot):
    """Snapshot verification: Live WS bypass update visually updates main panel LCD display."""
    handler = v3_system.handler
    ws_bridge = v3_system.ws_bridge
    hw = v3_system.hw
    assert handler.current is not None

    gain_param = make_parameter("gain", "noise", value=0.5)
    plugin = make_plugin("noise", category="Distortion", bypassed=False, has_footswitch=True, parameters={Symbol("gain"): gain_param})
    handler.current.pedalboard.plugins = [plugin]
    handler.bind_current_pedalboard()
    handler.lcd.link_data(handler.pedalboard_list, handler.current, hw.footswitches)
    handler.lcd.draw_main_panel()

    # Baseline snapshot of main panel (active plugin)
    snapshot("main_panel_active")

    # Inject bypass from Web UI -> updates LCD display
    ws_bridge.inject(f"param_set /graph/{plugin.instance_id} :bypass 1.0")
    handler.poll_ws_messages()

    # Snapshot of main panel showing bypassed plugin tile
    snapshot("main_panel_bypassed")


def test_ws_snapshot_preset_change_lcd_snapshots(v3_system: SystemFixture, make_plugin, snapshot):
    """Snapshot verification: Inbound pedal_snapshot WS message updates preset index and title header on LCD."""
    handler = v3_system.handler
    ws_bridge = v3_system.ws_bridge
    hw = v3_system.hw
    assert handler.current is not None

    plugin = make_plugin("fuzz", category="Distortion", bypassed=False, has_footswitch=True)
    handler.current.pedalboard.plugins = [plugin]
    handler.current.presets = {0: "Default Preset", 1: "Crunch Lead"}
    handler.current.preset_index = 0

    handler.bind_current_pedalboard()
    handler.lcd.link_data(handler.pedalboard_list, handler.current, hw.footswitches)
    handler.lcd.draw_main_panel()

    # Baseline snapshot of preset 0
    snapshot("01_preset_0_default")

    # Inject preset switch from Web UI
    ws_bridge.inject("pedal_snapshot 1 Crunch Lead")
    handler.poll_ws_messages()

    # Verify model preset index updated
    assert handler.current.preset_index == 1

    # Snapshot showing updated title bar with preset 1
    snapshot("02_preset_1_crunch_lead")


def test_ws_plugin_remove_lcd_snapshots(v3_system: SystemFixture, make_plugin, snapshot):
    """Snapshot verification: Inbound remove WS message dynamically purges plugin tile and re-layouts LCD main panel."""
    handler = v3_system.handler
    ws_bridge = v3_system.ws_bridge
    hw = v3_system.hw
    assert handler.current is not None

    fuzz = make_plugin("Fuzz", category="Distortion", bypassed=False, has_footswitch=True)
    delay = make_plugin("Delay", category="Delay", bypassed=False, has_footswitch=True)
    handler.current.pedalboard.plugins = [fuzz, delay]

    handler.bind_current_pedalboard()
    handler.lcd.link_data(handler.pedalboard_list, handler.current, hw.footswitches)
    handler.lcd.draw_main_panel()

    # Baseline snapshot with 2 plugins
    snapshot("01_two_plugins")

    # Inject dynamic remove of Delay plugin from Web UI
    ws_bridge.inject("remove /graph/Delay")
    handler.poll_ws_messages()

    # Verify Delay removed from model
    assert not any(p.instance_id == "Delay" for p in handler.current.pedalboard.plugins)

    # Snapshot showing re-laid out LCD main panel with only Fuzz remaining
    snapshot("02_delay_removed")


def test_ws_patch_set_rename_lcd_snapshots(v3_system: SystemFixture, make_plugin, snapshot):
    """Snapshot verification: Inbound patch_set WS message dynamically updates plugin extra_data/name and repaints LCD tile."""
    handler = v3_system.handler
    ws_bridge = v3_system.ws_bridge
    hw = v3_system.hw
    assert handler.current is not None

    nam_uri = list(NAM_URIS)[0]
    nam_plugin = make_plugin("nam", uri=nam_uri, category="Amp", bypassed=False, has_footswitch=True)
    handler.current.pedalboard.plugins = [nam_plugin]

    handler.bind_current_pedalboard()
    handler.lcd.link_data(handler.pedalboard_list, handler.current, hw.footswitches)
    handler.lcd.draw_main_panel()

    # Baseline snapshot before model/customization load
    snapshot("01_nam_initial")

    # Inject patch_set model change from Web UI (Format: patch_set /graph/{instance} {writable} {paramUri} {valueType} {value})
    ws_bridge.inject(f"patch_set /graph/nam 1 http://github.com/mikeoliphant/neural-amp-modeler-lv2#model p /path/to/Marshall_JCM800.nam")
    handler.poll_ws_messages()

    # Verify display_name updated
    assert nam_plugin.display_name == "Marshall_JCM800"

    # Snapshot showing updated LCD plugin tile label
    snapshot("02_nam_model_renamed")
