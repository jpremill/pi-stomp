# Investigation: Emulator MockFootswitch Cannot Exercise Longpress or Chords

> **Branch:** `fixes/emulator-footswitch` (working tree)
> **Date:** 2026-08-10
> **Status:** Implementation **applied and verified** (see §7–§9). The claim from
> §1 is validated; the fix ships the mock a real three-state press machine and
> the window key-down/key-up gesture path. 1387 passed / 2 skipped, pyright 0.

---

## 1. The claim being investigated

> `MockFootswitch.press()` (`emulator/controls.py:104`) toggles state and calls
> `refresh_callback` — there is no press/release distinction, so it never produces a
> LONGPRESSED event and cannot hold two switches down at once.
>
> **Consequences:** Footswitch longpress actions can't be exercised in the emulator at
> all; longpress chords (`longpress: groups`) are entirely untestable there (which is why
> a chord-resolution bug could ship unnoticed); chord resolution keys off physical hold
> state, which a mock with no underlying `AnalogSwitch`/`GpioSwitch` reports as RELEASED
> — so chords silently degrade to lone-longpress behavior in the emulator rather than
> failing loudly.

---

## 2. What the code actually does (phase 1–2)

### 2.1 `MockFootswitch.press()` — `emulator/controls.py:104-106`

```python
def press(self):
    self.toggled = not self.toggled
    self.refresh_callback(footswitch=self)
```

Verified verbatim. It only flips `toggled` and calls the LCD refresh callback. It never:
- emits a `SwitchEvent` into a sink,
- calls `_on_switch()`,
- tracks a press timestamp or duration,
- distinguishes a press from a release, or
- produces a `LONGPRESSED` state.

`poll()` is `pass` and `check_longpress_events()` is `pass` (`controls.py:101-102, 91-93`).
There is **no** key-down/key-up state anywhere on the mock.

### 2.2 Emulator window input handling — `emulator/window.py`

Footswitches are driven two ways, both calling the same arg-less `press()`:

- **Buttons:** `emulator/window.py:160` — `lambda fs=fs: fs.press()`.
- **Keyboard:** `emulator/window.py:376-378` `_press_fs()` → `footswitches[index].press()`.
  Keys `1/2/3/4` → `_press_fs(0..3)` (`window.py:339-346`).

There is **no `pygame.KEYUP` handling anywhere** in the window (grep confirms only
`KEYDOWN`), and **no footswitch longpress keybinding** at all. Compare the **nav encoder**,
which *does* support longpress (the only control that does):
- `window.py:335-336` — `L` key → `nav.press(switchstate.Value.LONGPRESSED)`.

So even at the input layer, the emulator can express longpress only for the nav encoder,
never for a footswitch, and cannot hold two switches down simultaneously (no `KEYUP`, no
held-state model).

### 2.3 What the real hardware does (phase 3) — for contrast

The real footswitch drivers are stateful three-state machines:

- `pistomp/switchstate.py:19-25` — `Value` enum: `PRESSED`, `RELEASED`, `LONGPRESSED`
  (plus unused `CLICKED`/`DOUBLECLICKED`).
- `pistomp/analogswitch.py:52-75`, `pistomp/gpioswitch.py:65-95` — both track
  press→hold→**LONGPRESSED** after `0.5s`, then `RELEASED` on release. `GpioSwitch`
  also carries a three-state `press_state` field (added by the newer resolver fix).
- `pistomp/footswitch.py:192-199` `_on_switch()` — maps `LONGPRESSED`→`SwitchEventKind.LONGPRESS`
  else `PRESS`, then `self.sink.handle(...)`. This is the **single dispatch entry** the mock
  bypasses entirely.

---

## 3. Verdict on each consequence

| # | Claim consequence | Verdict |
|---|---|---|
| 1 | Longpress actions can't be exercised in the emulator | **VALID.** `press()` is arg-less and emits no `SwitchEvent`; no LONGPRESS keybinding for footswitches. |
| 2 | Longpress chords are entirely untestable in the emulator | **VALID.** Chords enter via `chord_helper.observe()` from a dispatched `LONGPRESS` `SwitchEvent` (`modalapi/modhandler.py:477`); the mock never produces one. |
| 3 | A chord-resolution bug could ship unnoticed | **VALID & CONFIRMED BY HISTORY.** Commit `0e793be5` ("Resolve longpress chords at the stomp, not after a window") is a genuine chord-resolver bug fix — "A chord fired every member's solo action alongside the chord action" and the singleton race was broken. It lives on `feat/footswitch-ui` / `fix/footswitch-just-chord-action`, **not** in `origin/main`. The emulator could not have caught it. |
| 4 | Chord resolution keys off physical hold state, which the mock reports as RELEASED → silently degrades to lone-longpress | **PARTLY INVALID — mechanism differs.** The **current** `origin/main` resolver (`pistomp/footswitch_chords.py`) keys off the **`observe()` timestamp** count + a `WINDOW`, **not** a queried physical hold state; and the mock never dispatches a `SwitchEvent` at all (so it never even reaches `observe()`). The *newer* resolver on `feat/footswitch-ui` does key off `press_state`/membership (`PRESSED`/`LONGPRESSED`), which is where the "physical hold state" phrasing comes from. Net effect is still silent — but it's "no event / no chord at all" on current main, not "degrade to lone-longpress". The real gap (silent, untestable) stands regardless. |

**Bottom line:** the core finding is **correct and important** — the emulator's
`MockFootswitch` is a short-press/visual-toggle-only stand-in and cannot exercise
longpress or chords through the real dispatch path. Consequence #4's stated *mechanism* is
stale relative to `origin/main`, but its conclusion (silent, untestable) is correct.

---

## 4. Why the fix must target the real dispatch path, not cosmetic toggling

`MockFootswitch.press()` today short-circuits the entire pipeline — it never reaches
`_on_switch()` → sink → `_handle_footswitch()` → binding table **or** `chord_helper.observe()`.
So "adding a longpress to the window" alone is insufficient: the mock must emit real
`SwitchEvent`s, and the window must model key-down/key-up so held gestures can be built up.

Design principle (from `CLAUDE.md`): match the real hardware's idioms. The hardware is a
three-state state machine; the mock should be too.

---

## 5. Implementation plan

Intended to resolve the gap so the emulator can drive longpress actions and chord gestures.

### 5.1 `emulator/controls.py` — give `MockFootswitch` a real press state machine

Model the hardware detectors (`AnalogSwitch`/`GpioSwitch`): `RELEASED → PRESSED →
LONGPRESSED` based on hold time, dispatching real `SwitchEvent`s on the shared `_on_switch()`
path. Concretely:

```python
class MockFootswitch(footswitch.Footswitch):
    LONG_PRESS_TIME = 0.5

    def __init__(self, id, midi_CC, midi_channel, refresh_callback):
        super().__init__(id, None, None, midi_CC, midi_channel, refresh_callback)
        self.type = None
        self.cfg = {}
        self.press_state = switchstate.Value.RELEASED
        self._press_time = None  # monotonic() at press-down

    def press_down(self, timestamp=None):
        if self.press_state is switchstate.Value.RELEASED:
            self.press_state = switchstate.Value.PRESSED
            self._press_time = timestamp if timestamp is not None else time.monotonic()
        self.refresh_callback(footswitch=self)

    def press_up(self):
        if self.press_state is switchstate.Value.LONGPRESSED:
            self.press_state = switchstate.Value.RELEASED
            self._press_time = None
        elif self.press_state is switchstate.Value.PRESSED:
            self.press_state = switchstate.Value.RELEASED
            self._on_switch(switchstate.Value.RELEASED, self._press_time or 0.0)
            self._press_time = None
        else:
            self._press_time = None
        self.refresh_callback(footswitch=self)

    def poll(self):
        # Drive the PRESSED -> LONGPRESSED transition while held, mirroring the
        # hardware poll() that delegates to AnalogSwitch.refresh / GpioSwitch.poll.
        if self.press_state is switchstate.Value.PRESSED and self._press_time is not None:
            if time.monotonic() - self._press_time >= self.LONG_PRESS_TIME:
                self.press_state = switchstate.Value.LONGPRESSED
                self._on_switch(switchstate.Value.LONGPRESSED, self._press_time)

    def press(self):  # kept for backward-compat with existing tests/UI
        return self.press_down()
```

Notes:
- `press_up()` after `LONGPRESSED` clears silently (matching `AnalogSwitch`), no second event.
- Keep `press()` as a thin alias for `press_down()` so `tests/emulator/test_mock_controls.py`
  and the existing window button still work unchanged.
- `self.sink` is assigned by `register_sink()` (`pistomp/hardware.py:79`) — propagating, so
  `_on_switch()` now routes into the real dispatcher, binding table, and chord helper.

### 5.2 `emulator/window.py` — model key-down/key-up and a footswitch longpress key

- In `_handle_key`, add `pygame.KEYUP` handling (currently absent) with a mapping
  `key → footswitch index` so `KEYDOWN` = `press_down()`, `KEYUP` = `press_up()`.
- Add a footswitch longpress keybinding (e.g. hold the key) OR simpler: a dedicated key
  (like the nav `L`) that calls `press_down()` then lets the mock's `poll()` longpress path
  fire — but the *correct* gesture fix is key-down/key-up so a held switch drives `LONGPRESSED`
  and two switches can be held simultaneously for a chord.
- Update the on-screen hints (`window.py:276-297`) to advertise the new footswitch hold/longpress.

`poll()` is already called via `Modhandler.poll_controls()` → `super().poll_controls()` →
`hardware.poll_controls()` which iterates footswitches; `EmulatorModhandler.poll_controls()`
(`emulator/modhandler.py:123-126`) drains window events then calls super, and the base
`poll_controls` reaches the chord `_tick_chords()`. So the two-new-`poll()` (longpress
transition) and the existing `_tick_chords()` will run on the loop without extra wiring.

### 5.3 Tests

- `tests/emulator/test_mock_controls.py` — extend `MockFootswitch` coverage:
  - `press_down` + short `press_up` → dispatches `SwitchEvent(kind=PRESS)` to the sink.
  - `press_down` + hold past `LONG_PRESS_TIME` (drive `poll()`) → dispatches
    `SwitchEvent(kind=LONGPRESS)`; then `press_up` clears with no second event.
  - RELEASED→PRESSED→LONGPRESSED state transitions mirror the hardware.
- New emulator-level test (optional): a chord gesture — press two mock footswitches, drive
  `poll()` past the hold threshold, assert a lone longpress does **not** fire while a second
  partner is within the window (singleton group), and that a chord does fire (multi-member).
- Existing tests must stay green (the `press()` alias preserves current behavior).

### 5.4 Verification gates

- `uv run pytest` — full suite (currently 1382 passed / 2 skipped at this branch state).
- `uv run pyright` — 0 errors, 0 warnings.

### 5.5 Out of scope / note

- This document originally planned (not yet applied) the fix; it is now **applied**
  on `fixes/emulator-footswitch` and verified — see §7–§9 for the implementation,
  one deviation from §5.1 (§8.1), side effects (§8), and the press-state-resolver
  alongside analysis (§9).
- The newer **press-state** resolver on `feat/footswitch-ui` (`0e793be5`) is a separate
  future change not in `origin/main`; the mock fix above works with the current main
  resolver and is compatible with either since it matches the hardware's shared
  `_on_switch()` contract (the one caveat being the press-state branch's `press_state`
  property readback — §9.3).

---

## 6. Evidence index

| Item | Location |
|---|---|
| Mock state machine (press_down/press_up/poll) | `emulator/controls.py:111-143` |
| Mock `press()` visual toggle (backward-compat) | `emulator/controls.py:139-143` |
| Mock `check_longpress_events()` no-op | `emulator/controls.py:100-102` |
| Nav encoder longpress key (only longpress in emu) | `emulator/window.py:346` |
| Footswitch window buttons (visual toggle) | `emulator/window.py:168` |
| Footswitch key down/up + `_FS_KEYS` | `emulator/window.py:47-53, 350-351, 382-391` |
| KEYUP handling added | `emulator/window.py:240` |
| State enum (PRESSED/RELEASED/LONGPRESSED) | `pistomp/switchstate.py:19-25` |
| ADC state machine | `pistomp/analogswitch.py:52-75` |
| GPIO state machine + press_state | `pistomp/gpioswitch.py:65-95` |
| Real dispatch entry | `pistomp/footswitch.py:192-199` |
| Chord entry via LONGPRESS no-row | `modalapi/modhandler.py:464-487` |
| Current (main) timestamp resolver | `pistomp/footswitch_chords.py:58-87` |
| Newer press-state resolver (not in main) | commit `0e793be5` (feat/footswitch-ui, fix/footswitch-just-chord-action) |
| Confirmed chord bug that shipped prior | commit `0e793be5` message |

---

## 7. Implementation applied

Applied on branch `fixes/emulator-footswitch`. All verification gates green:
`uv run pytest` → **1387 passed / 2 skipped** (was 1382 / 2 at plan time);
`uv run pyright` → **0 errors, 0 warnings**.

### 7.1 `emulator/controls.py` — `MockFootswitch` is now a three-state machine

`press_state` (`RELEASED → PRESSED → LONGPRESSED → RELEASED`) mirrors
`AnalogSwitch.refresh` / `GpioSwitch.poll`, and real `SwitchEvent`s now dispatch
through the base `Footswitch._on_switch()` → `self.sink`, so a held mock reaches
the binding table **and** `chord_helper.observe()` — the path the old arg-less
`press()` short-circuited.

- `press_down(timestamp=None)` — `RELEASED→PRESSED`, records `_press_time`, refresh.
- `poll()` — while `PRESSED` and held ≥ `LONG_PRESS_TIME` (0.5 s, matching the
  hardware detectors), matures to `LONGPRESSED` and dispatches `LONGPRESS`. Runs
  from `hardware.poll_controls()` → `footswitches[i].poll()` with no extra wiring,
  exactly as §5.2 predicted.
- `press_up()` — `LONGPRESSED→RELEASED` clears silently (matches `AnalogSwitch`);
  `PRESSED→RELEASED` dispatches `RELEASED` → `SwitchEventKind.PRESS` (the short
  click). Passing `_press_time` as the event timestamp matches how the hardware
  passes `start_time`.
- `press()` — **kept as a direct visual toggle** (`toggled = not toggled` +
  refresh), not a `press_down()` alias. See §8.1 for why this deviates from §5.1.

### 7.2 `emulator/window.py` — key down/up + footswitch hold gesture

- `_FS_KEYS` maps `1..4` (and keypad variants) → footswitch index.
- `KEYDOWN` on a footswitch key → `press_down()`; new `KEYUP` handling →
  `press_up()`. Holding a key now drives the mock's `poll()` longpress transition,
  and two keys can be held simultaneously to build a chord.
- Buttons (`_Btn`) still call `press()` (visual toggle).
- On-screen hint updated: `"1-N fs  hold=long"`.

### 7.3 Tests

- `tests/emulator/test_mock_controls.py` — new coverage: initial `RELEASED`;
  `press_down` marks `PRESSED` without dispatch; short press dispatches `PRESS`;
  hold matures to `LONGPRESS` then release is silent; re-press after release
  re-arms; and an end-to-end two-mock chord through the real `observe`/`tick`
  resolver path.
- `tests/emulator/test_window_keybindings.py` — the number-key test now asserts
  `press_down` on `KEYDOWN`; added a `KEYUP` → `press_up` test.

---

## 8. Side effects & deviations from the plan

### 8.1 `press()` is a direct toggle, not a `press_down()` alias

§5.1 proposed `press() == press_down()`. That would **break the existing
behavior and tests**: `press_down()` alone neither sets `toggled` nor dispatches
(dispatch happens on release), so the window button click and the legacy
`test_footswitch_press_toggles_and_calls_refresh` suite would stop toggling.
`press()` is therefore kept as the direct visual toggle it always was. The
*toggle* itself is actually driven by the handler (`_fire_row` → `ParamEffect` /
`MidiCcEffect` / `RelayEffect`) once a real gesture routes through dispatch; the
mock's `press()` is a convenience for the click-only button path.

### 8.2 Number-key tap on an *unbound* footswitch no longer toggles

Before, a number key called `press()` → always toggled. Now a key is a
`press_down`/`press_up` cycle that dispatches a `PRESS` event; `_handle_footswitch`
only toggles when a binding row exists. For an **unbound** switch the key does
nothing to `toggled` — which is exactly what the real hardware does (an unbound
footswitch press is a no-op). The button retains the legacy always-toggle. This
is a deliberate, device-faithful asymmetry, not a bug.

### 8.3 The mock now requires a sink to dispatch

`_on_switch()` reads `self.sink` (a asserting property). Any external driver that
calls `press_down`/`press_up`/`poll` on an emulator mock **must** have a sink
assigned (`register_sink()` is always called before the loop in the emulator, so
runtime is unaffected). Tests exercising dispatch set `fs.sink`; the legacy
`press()` toggle path needs no sink and is unchanged.

### 8.4 More refresh calls per gesture

`press_down` and `press_up` each call `refresh_callback`, plus the handler's
`update_lcd_fs` on dispatch. A single click now refreshes more than the old single
`press()`. Cosmetic only (LCD is repaint-gated by the poll loop), no correctness
impact.

### 8.5 `toggled` is decoupled from the mock's held state

`press_state` (physical) and `toggled` (logical) are now distinct: a hold sets
`press_state` while `toggled` is only flipped by the handler on a fired row. This
matches the real `Footswitch`, where the detector owns `press_state` and the
handler owns `toggled`. Nothing in the emulator read `press_state` before, so no
consumer regresses.

### 8.6 Key-repeat is harmless

`pygame.key.set_repeat(300, 50)` emits repeated `KEYDOWN` while held; `press_down`
is idempotent when already `PRESSED` (only refreshes), so repeats neither reset
the hold timer nor double-fire. `KEYUP` fires once on real release.

---

## 9. The press-state resolver ("footswitch UI") — identify & alongside

### 9.1 What it is

The "footswitch UI" for the press-state resolver is the **`feat/footswitch-ui`**
feature branch (and its dependency **`fix/footswitch-just-chord-action`**, commit
`0e793be5`). It replaces the current timestamp+`WINDOW` resolver
(`pistomp/footswitch_chords.py` §5.4 index) with a **synchronous, hold-state**
resolver: chords are decided the moment the first member matures, keyed off
`fs.press_state` through a `LongpressMember` Protocol. `tick()`/`WINDOW`/timestamps
are gone; `poll_controls` calls `chord_helper.poll()` (release re-arm) instead of
`_tick_chords()`, and `_handle_footswitch` LONGPRESS no-row calls
`_fire_longpress_groups(fs)` → `chord_helper.observe(fs)` instead of
`chord_helper.observe(fs, timestamp)`.

### 9.2 Current main's resolver already works with the mock

The fix in §7 is built against `origin/main`'s resolver, and that path is fully
exercised end-to-end by the new chord test (§7.3): the mock emits `LONGPRESS`
`SwitchEvent`s → `_handle_footswitch` no-row → `observe(fs, timestamp)` →
`tick()`. So **on this branch, chords are now testable in the emulator** — the
core deliverable.

### 9.3 Can the press-state resolver be fixed *alongside*? — Partially, as a follow-up

The press-state resolver reads `fs.press_state`, which on `feat/footswitch-ui` is
a **read-only `@property` on `Footswitch`** that returns
`adc_switch.state` / `gpio_switch.state` — and `RELEASED` for a footswitch with
neither detector. Two concrete blockers for the emulator mock on that branch:

1. **`press_state` is unreportable.** The mock drives `_on_switch` directly and
   owns no detector, so the base property returns `RELEASED` always → `satisfied_by`
   is never true → chords never resolve for mocks (the repo itself flags this in
   the `0e793be5` commit message as *"mocks, externals read RELEASED — see issue
   #222"*).
2. **Instance-attribute collision.** The mock's `self.press_state = ...` (added
   here) would hit the read-only `@property` and raise `AttributeError` on
   instantiation → the emulator would crash if `feat/footswitch-ui` is merged
   without an accompanying mock change.

**Determination:** the *mechanical* wiring (mock `poll()` maturing the longpress,
`hardware.poll_controls` → `s.poll()`, `chord_helper.poll()` re-arm) is already
compatible — the mock's three-state `press_state` is exactly the value the
resolver keys off. The missing piece is *surfacing* that state to the resolver
on the other branch. That is a **separate, small change on `feat/footswitch-ui`**
(route the mock's own `press_state`, or give the mock a detector-like stub whose
`.state` the property reads), not something this branch can land without pulling
in the whole feature. It is the natural follow-up to this work and is tracked by
issue #222. This branch's fix is deliberately resolver-agnostic (§5.5) and does
not regress either resolver.
