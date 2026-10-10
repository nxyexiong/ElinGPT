# ElinGPT

A Steam Workshop-compatible Elin code mod that lets external agents observe
local terrain and player-visible entities and use normal gameplay actions while you watch.
The game bridge is local, disabled by default, and independent of any model
provider. This repository does not bundle or redistribute Elin game files.
Observation and control are strictly text-based: structured JSON comes directly
from game state and native UI text. There are no screenshot, image-analysis,
OCR or desktop-input interfaces.

## Components

| Source | Responsibility |
| --- | --- |
| `src\ElinGPT.Core` | Versioned JSON protocol, action lifecycle, revisions, ownership leases, deduplication, bounded main-thread queue and authenticated loopback TCP server |
| `src\ElinGPT.Mod` | BepInEx 6 plugin, textual Elin state/action/UI adapters, pause policy, spectator overlay and Workshop metadata |
| `src\elingpt` | Python bridge client, stdio MCP server, tool schemas and bounded chat-completions agent runner |

The mod targets .NET Framework 4.7.2 and references the game's existing loader,
Harmony, Unity and Newtonsoft assemblies. Build with .NET SDK 9+ and use
Python 3.10+. The connector has no runtime package dependencies.

## Build and install

Run these PowerShell commands from this folder:

```powershell
.\scripts\build.ps1
```

This builds **only** `dist\Mod_ElinGPT`; it does not install anything or launch
the game. To explicitly install, close Elin first:

```powershell
.\scripts\build.ps1 -Install
```

For another installation/version:

```powershell
.\scripts\build.ps1 -ElinGamePath 'D:\SteamLibrary\steamapps\common\Elin' -GameVersion '0.23.352' -Install
```

The default game path matches the installation used for development. The
manifest's default game version is `0.23.352`, identified in a live game run.
Set `GameVersion` to the version you actually confirm before publishing;
it is **not** the mod's semantic version.

Launch Elin through Steam (not the EXE directly) and enable the local mod in its
mod list. The guarded helper reuses an existing process rather than starting
a duplicate:

```powershell
.\scripts\launch.ps1
```

The launcher does not bypass Steam Cloud warnings, disable Cloud, or resolve
save conflicts. If Steam stalls before creating a game process, inspect
`logs\cloud_log.txt` under the Steam installation for **AppID 2135150** and
the current launch time. A successful sync log is not proof that the game
launched. During development, a normal Steam restart cleared a launch blockage
even though the latest sync had succeeded; the earlier file-list failures did
not include enough detail to establish their underlying cause. Do not delete
saves or `remotecache.vdf` as a generic workaround.

Use a disposable
or backed-up save. **F8** toggles the local bridge. **F9** immediately releases
agent control; keyboard presses, mouse clicks and scrolling also take over.
Mouse movement alone does not interrupt spectating.

An enabled bridge writes `%LOCALAPPDATA%\ElinGPT\connection.json` with a random
pairing token and an automatically selected loopback port. The containing
directory is restricted to your Windows account using native Windows security
APIs (the game's Mono runtime lacks managed Windows identity/ACL support).
Keep this file private; never
put it in the Workshop package. The descriptor is removed when the bridge is
disabled normally. After a crash, close all game instances and remove only that
stale `connection.json` before enabling again.

While enabled, the plugin keeps Unity running in the background so a connector
can talk to the game when the terminal has focus. It restores the previous
background setting when disabled.

Configuration is stored by BepInEx in
`BepInEx\config\elingpt.agentbridge.cfg`. Observation/control does not read
rendered pixels and does not offer a capture setting or endpoint.

## Read state or expose MCP tools

No Python installation step is required:

```powershell
$env:PYTHONPATH = Join-Path $PWD 'src'
python -m elingpt observe
python -m elingpt mcp
```

The second command is a long-lived MCP stdio server, intended to be launched by
your agent client. It does not autonomously play and does not claim control
until an agent explicitly calls `elin_claim`.
The CLI `observe` command now prints the same compact full view used by agents.
Use `python -m elingpt observe --raw` only when you need the large, unchanged
native bridge snapshot for debugging.

### Built-in gameplay guide

MCP initialization and the standalone runner include concise game instructions.
`elin_ping` returns the guide on demand, and native bridge `ping` embeds the
same `gameplay_guide` for non-Python clients. The single source is
`src\elingpt\gameplay_guide.json`, bundled in the Python package and Core DLL.
It is not repeated in every observation. It covers coordinate spaces, world
actions versus UI input, turns, item carry/quick-use, backpack capacity, crafting,
equipment, survival and normal save/death flows.

**Map coordinates are not screen pixels:**

| Direction | `dx` | `dz` | Local isometric screen movement |
| --- | ---: | ---: | --- |
| North | 0 | +1 | Up-right |
| South | 0 | -1 | Down-left |
| East | +1 | 0 | Down-right |
| West | -1 | 0 | Up-left |

The overworld is top-down: east is right, north is up. On a local map,
screen-up is `(-1,+1)` and screen-left is `(-1,-1)`. Higher `z` means **north**,
not south. Native `EInput.UpdateAxis` maps Up to positive y; `AM_Adv.AxisMove`
adds that to map z. `Point.Position` projects local tiles using `(x+z,z-x)`;
`Point.PositionTopdown` uses `(x,z)`. The bridge's `step` adds dx/dz directly
to map coordinates, independent of the user's alternate keyboard-axis setting.
Local and overworld coordinates are separate; always check zone and arrival.

To register it with Copilot CLI, run from this project folder:

```powershell
$python = (Get-Command python).Source
$source = Join-Path $PWD 'src'
copilot mcp add elingpt --env "PYTHONPATH=$source" --env "LOCALAPPDATA=$env:LOCALAPPDATA" --env PYTHONUTF8=1 --timeout 30000 -- $python -m elingpt mcp
```

If an already-running Copilot session does not expose the new tools, run
`/restart` in that session to load the configuration. Use the MCP tools directly
for play rather than creating a new Python client script for each action.
`LOCALAPPDATA` lets the server find the private pairing descriptor; the pairing
token itself must never be copied into MCP configuration.

Example MCP client configuration (adapt the enclosing key to your client):

```json
{
  "mcpServers": {
    "elingpt": {
      "command": "python",
      "args": ["-m", "elingpt", "mcp"],
      "env": {
        "PYTHONPATH": "C:\\UserData\\workspace\\ElinGPT\\src"
      }
    }
  }
}
```

Available tools: `elin_ping`, `elin_observe`, `elin_claim`, `elin_release`,
`elin_inspect`, `elin_available_actions`, `elin_act`, `elin_action_status`,
`elin_ui_observe`, `elin_ui_act`, `elin_session_command`,
`elin_cancel`.
The two legacy UI aliases remain accepted. Add `mcp --compat-tools` to advertise
`elin_ui_choose` and `elin_ui_set_text` as well; normal clients need only 12 tools.

An agent should claim control, observe, discover context actions when needed,
submit **one** action with the current `session_id`/`expected_revision`, then
query its `action_id` only while it is still running. The action response already
contains fresh observation changes; do not request a redundant full snapshot
before every action. `awaiting_ui` is not failure and not a
reason to resubmit the world action. `stale_observation` means observe again.
Visible traversable local tiles also advertise `Walk here`: native pathfinding
to that tile, not teleportation or a sequence of scripted steps. It shares the
normal action ownership, interruption and timeout rules, and verifies arrival.
Stepping beyond a map edge uses the game's native border-exit checks and may
open its normal travel confirmation; resolve that UI before another world action.
Network errors with `outcome_unknown` must not be blindly retried.
MCP polls submitted actions, and explicit status queries, for up to ten seconds
internally and returns their status plus
fresh state, including deferred quit acknowledgement. If still `running`, poll
`action_status` rather than resubmitting. Explicit
`observe`/`ping` may reconnect after a lost connection or native quit; they never
replay a mutation or reacquire control. Other operations remain blocked until
that explicit read succeeds.
The UI adapter is a **generic runtime GameObject tree**, not a list of supported
screens or hand-written dialogue handlers. `ui_observe(view="tree")` returns canvas roots and
flat nodes connected by `parent_id`. Each node includes its hierarchy path,
component types, role, text/label, active/enabled/visible/interactable/occluded
state, focus, screen-space bounds, values/options and advertised operations.
Metadata comes directly from Unity objects, never rendered pixels.

Snapshots are bounded. When `truncated` is true, query `root_id`, `depth`,
`limit` and optionally `include_inactive` to inspect a relevant subtree.
MCP and model-runner observations use a compact `decision_nodes` projection:
visible text and advertised controls retain their IDs, while verbose component,
event and geometry metadata is omitted. Parent IDs refer to the full tree, not
necessarily another summary node. `ui_observe` defaults to a compact page of
exact visible text and controls. The full generic tree is an explicit debug
view and can still resolve those IDs.

The connector's `view_version: 2` is separate from the unchanged native bridge
protocol. A full observation uses lossless `palette_grid` terrain when smaller
than ordinary `columns`/`cells` rows. Its `origin` is `[x,z]`; `size` is
`[width,height]`; `rows[z-offset][x-offset]` indexes `palette`. A null index
represents an invalid/out-of-map cell, not an empty or unseen cell. Palette
values follow `columns` excluding `x`, `z` and fields supplied by `defaults`.
Inventory, entities, ground objects and messages can use `format: "table"`:
merge `defaults` with `columns` zipped to each row. Optional `missing`
`[row,column]` pairs distinguish absent fields from explicit null values.
Lists and row terrain remain valid when smaller. No values are inferred or
discarded to meet a budget. The Python reference decoder is
`elingpt.observation.apply_observation(previous, response)`.
Terrain includes every valid map cell in the square bounded by the player's
position plus/minus `limits.radius`. Character line-of-sight and `pcSync` do
not filter terrain: walls still shown on screen must not disappear from the
text observation just because the character cannot currently see behind them.
`limits.terrain_visibility_filtered` is false. This is a local map window,
not a camera viewport or a whole-zone dump. Character/item visibility checks
and native action targeting restrictions are unchanged.
`ground_objects` identifies vegetation and other terrain objects by position
and localized name within the same local window.
On the global map (`zone.region=true`), each tile also includes `terrain_name`
from the game's map tile and `site_name` for an inspectable location at that
position. A null `site_name` means no inspectable location is present there;
these fields are null on local maps.
Tiles include native `floor_variant`, `block_variant`, `object` and
`object_variant`, plus `block_type`, `block_axis`, wall/pillar `block_shape`
flags and `has_door`. Variants are
native cell direction/shape indices, **not** universal compass headings.
Wall/fence variants 0 and 1 extend along map x and z respectively; other shapes
have no single asserted axis. Variant **2** draws **both axis segments in a
fixed orientation**; it is **not a universal corner**. Setting all four corners
to 2 creates protruding arms. For an axis-aligned rectangle, the southeast corner
`(max x,min z)` uses 2, southwest uses 1, and northeast uses 0. The northwest
corner can remain empty: native `isWallEdge`, exposed as `wall_edge`, joins the
x-wall on its east with the z-wall on its south. Such a tile has `block=0` but
`blocked=true`; it is not an opening. Check segment connectivity rather than filling every
perimeter tile. `full_wall` is a separate renderer flag, not a corner indicator.
The player and visible entities include native `direction` and items
include `placement_state`; ground objects also expose their variant/type.
Terrain compaction retains all supplied tile fields, and variant-only changes
are included in terrain deltas. Decode the advertised representation and read
`terrain.columns`, not hard-coded indexes.

When a buildable item is held, `state.placement` describes its selected native
recipe, direction, tile type and rotation availability. `act(kind="rotate_held")`
cycles the native recipe without placing anything. Observe the result and
rediscover context actions before building. Build context actions include
`placement` metadata with the native recipe direction (doors can auto-align
to their supporting wall) and distinguish `place` from `rotate_existing_block`.
For existing-block rotation, `target_block_variant` is the current cell variant,
not a predicted result; observe the resulting tile after completion.
Hold a wall block and use the target's advertised **Rotate block** action
(`placement.operation="rotate_existing_block"`). The native cycle is
`0 -> 1 -> 2 -> 0`; wrapping from 2 to 0 also toggles `cross_wall`, which affects
material selection at dual-axis joins. Inspect both fields. `rotate_held` affects only the
placement preview, not existing walls. Remove an extra piece through normal
dismantling rather than preserving an incorrect wall count. Verify joins and
the doorway afterward; some wall faces must be approached from inside.
These actions still use the game's planner, legality checks, costs and goals;
there is no direct terrain-edit endpoint. Planning uses the requested visible
tile, not the spectator's mouse target, and discovery restores shared previews.
Scheduled held placement gets a separate native recipe so mouse previews
cannot change its selected direction. Replacing or rotating the held item
invalidates its context handles.
Inventory items include their native `equipped` flag. An equipped item's stored
position is not an inventory slot; do not infer equipment from that position.
Repeated action lists use `node.action_set` as an index into `ui.action_sets`.
Large UI summaries prioritize visible modal windows, tooltips, and floating
windows before HUD widgets, including short labels, instructions and costs.
Each page reports `page_id`, `offset`, `next_offset`, `total_nodes`, and
`summary_omitted`. Continue the **same query** with `offset=next_offset` and
`page_id`; changing UI/revision produces `stale_ui_page`, requiring a new first
page. Native `truncated` is separate: query a smaller subtree in that case.
Snapshots reserve a 4 KB UI page; standalone UI queries default to 8 KB and
accept `max_bytes` between 1 KB and 64 KB. An indivisible long dialogue may
exceed that budget and explicitly reports `budget_exceeded`; it is never
silently dropped. A full observation exceeding the 24 KB target reports
`budget.exceeded`. These are byte budgets, not claims about model-specific
token counts. Terrain and inventory are always lossless.

`observe(mode="auto")` is now the default. The first response is a full baseline;
subsequent observe/action/status responses normally contain `delta: true`,
`base_id`, `base_revision`, and a new `observation_id`. Apply a delta only to
its matching baseline. Optional `observe(since=observation_id)` returns a full
baseline if that cursor no longer matches the connector's per-session cache.
Use `observe(mode="full")` after losing conversation context. Reconnects, new
sessions and generation/zone changes also require a new baseline. Full here
means complete compact state, not the raw Unity tree.

Apply changed top-level fields and `state_delta` replacements; remove
`meta_removed` top-level fields and `state_removed` state fields.
`collections_delta` upserts/removes inventory/entities by `handle`,
ground objects by `(x,z)`, and messages by `index`. Upserts may themselves be
tables. Canonical collections and terrain cells sort by those keys, avoiding
repeated coordinate-order lists during movement.
`ui_delta.meta` contains **only changed** metadata; merge it rather than replacing
all metadata, then remove `ui_delta.meta_removed`. Upsert/remove nodes by ID,
retaining order unless `ui_delta.order` is supplied.
`terrain_delta` upserts rows keyed by the first two columns `(x,z)` and removes
listed coordinate pairs. Its `columns` describe those rows.
Removed coordinates have left the observation window; they do not indicate
destroyed walls. Clearing a wall in an observed cell updates its `block` to 0.
An unchanged observation is a small acknowledgement, not a repeated UI/map.
Cosmetic text/visibility from the spectator's world-mouseover hint does not
invalidate semantic revisions. Its live state remains observable, and all
target controls are still validated at execution time.
Fading message widgets and hidden layout nodes also do not invalidate decisions;
the actual message log, visible controls, values, and world state still do.
Visible generic item-grid buttons expose the card's native display name, including
in inventory, shops and ingredient selectors; unidentified items remain unidentified.
Hidden/inactive text and password/PIN field values are not returned.
`ui_act` targets an advertised `node_id` and performs `click`, `right_click`, `hover`,
`focus`, `submit`, `cancel`, `set_text`, `end_edit`, `set_value`, `scroll`,
`drag`, or a supported public UnityEvent invocation. Standard Unity event
interfaces and component validation are used; no arbitrary method execution
or scene-object edits are exposed.

Generic pointer operations also synchronize the game's own pointer-event
context, so custom controls that consult it receive the same target/hover state
as standard Unity handlers. Clicks deliver press/release on successive native
input frames, including the game's generic mouse-button state. `hover` can
reveal native textual tooltips. `ui.input_busy` includes queued input, and the
action remains pending until that input has been delivered.
Native jobs started by queued UI callbacks remain owned until they finish.
Visible modal layers and context menus pause for decisions; retained but hidden
windows do not stop crafting or other work in progress.
Visible progress windows also keep running while a mod-owned native job is active
and the game's layer declares `pauseGame=false`. `ui.processing` reports this;
`ui.pauses_game` preserves the native modal/context-menu pause signal. The same
window becomes a decision boundary when the job finishes. This follows the
generic layer contract, not a per-crafting-screen whitelist.
After a native overlay closes, noninteractive wake/load transitions can finish
before the idle decision pause is restored.
Inventory items use the game's **click-to-carry, then click-to-place** behavior;
`ui.drag_active` reports that native carry state. Do not assume that a control
advertising a Unity drag handler necessarily implements item transfer through
that handler. Observe the resulting state after every operation.
The generic `key` operation pulses one advertised UI navigation key inside
Elin's managed Unity input wrappers. It does not send OS keys, touch the CLI,
or contain per-dialogue patches. For example, explicit Enter can reveal/advance
a dialogue or confirm a text dialog, and Escape can dismiss a modal, according
to the game's normal input behavior. The agent reads the tree before choosing.
The tree also advertises the game's key bindings; letters/digits can invoke
ordinary bound shortcuts without a screen-specific implementation.
The native middle-click binding (normally `r`) operates on the last hovered
control through the shared `UIButton` contract, allowing item-action menus.
While controlled, native Unity mouse-position reads use the same in-process
pointer as UI events; no desktop cursor movement is sent. This keeps native
context menus from closing because of a different physical pointer position.
Entering/leaving a virtual pointer target delivers the corresponding ancestor
events. Opening a native middle-click context menu moves the virtual pointer
onto that menu without selecting an option, preserving its normal leave-to-close
behavior while an agent reads its choices.
The mod never automatically skips dialogue.
`ui_choose` and `ui_set_text` remain compatibility aliases for generic node
click/text operations; new clients should use `ui_act`.
`elin_session_command` accepts `save`, `pause_menu`, `return_to_title`, and
`quit`. Saving reports the native save ID/count and refuses a failed save.
In-game return/quit open the game's standard save-and-exit confirmation;
the agent must choose deliberately. Native quit is acknowledged with
`quitting: true`, after which the connector stops issuing requests.
Generic UI operations can access the controls present in the actual UI,
including destructive save-management controls. Native confirmations are
preserved; the agent must not delete/overwrite saves without explicit approval.

Death interrupts the action that caused it but retains control of the native
death UI. An agent can also explicitly claim an already-open death dialog,
edit the last-words field, and choose the game's normal revival/continue
options. `status` remains `dead` and `ready` remains false until revival;
world actions, context discovery and session commands are blocked while dead.
Death/revival animations without UI are allowed to advance, and UI-driven
loading preserves ownership without treating a loading overlay as a decision.
Human takeover, disconnect, lease expiry and action time budgets still apply.
No automatic revival or save reload is performed by the bridge.

## Watch an autonomous agent

Choose a model endpoint that supports OpenAI-style chat-completions tool calls.
Example for a local server:

```powershell
$env:PYTHONPATH = Join-Path $PWD 'src'
python -m elingpt run --endpoint 'http://127.0.0.1:1234/v1' --model 'your-tool-capable-model' --objective 'Explore the nearby area safely, then stop.' --max-steps 30 --max-seconds 600
```

For a remote HTTPS endpoint, add `--allow-remote` to explicitly consent to
sending observations (including game text and character names) to that provider.
Set its credential in an environment variable, default `OPENAI_API_KEY`; use
`--key-env` for another variable. Keys remain outside the mod and are not logged.
No model requests are made by building or checking this repository.

The runner supplies one current compact state and reuses action-result snapshots.
It retains at most four recent tool-call turns within a 16 KB history target,
without replaying full snapshots inside those turns. The latest tool-call/result
group remains intact even if unusually large. The objective remains in the
system prompt. It allows at most one mutation per model response, maintains the
ownership heartbeat while the model is thinking, and stops on takeover,
acknowledged quit, connection loss or budgets.
It uses the same UI tree/operations in title menus, native death/revival dialogs,
gameplay and custom panels, waiting for death transitions that have no UI.
An optional `--transcript 'run.jsonl'` creates a new local trace; it contains game
observations and must not be published accidentally. An installed mod alone
does not install an agent or a model.

Both `mcp` and `run` accept `--metrics 'metrics.jsonl'` to create a new local
payload-accounting file. It contains tool names, byte counts, component byte
counts and timings, never game text or tool arguments. Runner entries include
provider-reported prompt/completion token usage when supplied. No tokenizer or
model dependency is required; byte counts are not advertised as exact tokens.
`elin_ping` also exposes in-memory connector call/byte totals. Metrics and
transcripts are ignored by Git; existing files are not overwritten.

## Supported first-version behavior

- Player HP/mana/stamina/hunger/conditions, position, bounded local terrain,
  visible nearby entities, top-level inventory and recent messages.
- Wait/search, one-tile steps, native discovered context actions, and opening
  inventory/character/abilities/journal through the game's normal UI functions.
- Context discovery covers native planner input modes, including held-item
  placement and collection. Handles retain their original input mode for
  revalidation; `held_item` identifies the player's currently selected item.
- Native save, return-to-title, quit, main-menu and death/revival UI navigation.
- Generic UI hierarchy, component state and Unity event operations, including
  text input, selection, scroll, drag/drop, focus and navigation keys.
  Native callbacks preserve purchases, costs and confirmation flows.
- Exclusive controller, pause between decisions, 30-second renewable lease,
  20-second no-turn-progress timeout, 120-second hard action budget, and
  interruption on damage or load/zone transition. Ongoing native jobs are not
  paused merely because a substep temporarily accepts input.

This is **not** a guarantee that every custom UI control implements standard
Unity event contracts. Unsupported event argument types are reported explicitly,
and hidden/disabled/occluded nodes cannot be manipulated. World build-designation
tools, minigame-specific actions and multi-tile movement macros still need
gameplay APIs. `inspect` returns the same safe entity summary, not
hidden statistics. Terrain outside the local observation window is omitted.
Long contextual tasks may require incremental commands
or additional gameplay bindings.

The installed mod has been exercised in Elin 0.23.352 Patch 1: generic modal and
title/load navigation, native name editing with Enter/Escape, subtree queries,
inventory item tooltips through hover, reversible settings-slider/dropdown
changes, focus, slider drag and native scroll, native inventory pickup/placement
with exact slot/count restoration, a real one-turn wait and exactly one
adjacent movement, and native saving/return-to-title/quit. The test character
was disposable; existing saves were not played or changed. No screenshots are
used. Standard-handler coverage is not a guarantee for every custom control,
and unusual custom controls still need broader scenario coverage.

Native death handling has also been exercised in the disposable save: the
fatal combat action was interrupted without losing ownership, a new controller
could claim the death dialog after disconnect, last words were edited and
submitted, and the native **Crawl up** choice returned to gameplay. A world
action was rejected while dead, accepted after revival, and the revived
character was saved through the native save command.

The ongoing disposable-save main-quest playthrough has completed **New Home**
and **Essential Equipment**, including native item-menu Grab, box placement,
shared-container permission changes and quest reporting. **Way of Crafter**
is in progress; the dog-rescue objective has not yet been reached. Native tree
and vine harvesting, multi-turn walking, workbench construction/placement,
string crafting and torch crafting have now been exercised through the bridge.
Crafting from a hidden retained window completes without freezing. The damage
safety boundary also stopped an overexerted crafting action after the torch was
produced. The torch has been equipped and reported; the current objective is
building the first house. Rest/sleep has also been exercised, exposing a final
wake-up pause that is now covered by a transition regression.

## Workshop publishing

`dist\Mod_ElinGPT` contains only `ElinGPT.Mod.dll`, `ElinGPT.Core.dll`,
`package.xml` and a generated `preview.jpg`. The package starts as **Unlisted**.
After a live smoke test, install it under `Elin\Package\Mod_ElinGPT` and publish
through Elin's Mod Viewer. Keep the package ID stable on updates. Do not add
game assemblies, tokens, logs, transcripts or provider credentials.

The Workshop package installs the game-facing bridge. Distribute this connector
repository separately; Workshop subscription cannot configure an agent client.
This tooling does not publish to Steam automatically.

## Checks

```powershell
.\scripts\check.ps1
```

This compiles the actual mod, runs .NET controller/transport checks, and runs
Python unit tests plus cross-language integration using a fake game adapter.
It does **not** launch Elin or touch saves. `ElinGamePath` is configurable here
as well. Tests use temporary pairing descriptors that are cleaned up afterward.

With Elin already open, repeatable live UI checks are separate and read-only
by default:

```powershell
python .\tests\live_smoke.py
python .\tests\live_smoke.py --settings-actions
python .\tests\live_smoke.py --inventory-save world_2
```

The settings command permits temporary title-screen settings changes and checks
that their values, scroll position and title menu are restored. The inventory
command requires an **already-loaded, explicitly named disposable save** with
the test fixture (five occupied main-inventory slots followed by an empty slot).
It moves one item and restores its slot/count in a `finally` block. These checks
never load a save, save to disk, start a character, skip story dialogue, or launch
a model. Tree checks reject duplicate IDs/roots, broken parent/depth links and
exposed sensitive values.

Compact-protocol checks are also read-only by default:

```powershell
python .\tests\live_tokens.py
python .\tests\live_tokens.py --exercise-save world_2
python .\tests\live_tokens.py --exercise-save world_2 --reference-tokenizer o200k_base
```

The opt-in exercise requires the named disposable save already loaded in Meadow,
full HP, sufficient stamina, no nearby hostiles and a clear adjacent tile. It
uses MCP requests through the current connector to step out and back, toggle
the native inventory, traverse its compact UI pages, and verify the restored
position, HP, inventory records and window state. Every observed result is
decoded and compared with stable native state. It does not load/save a world,
start a model or enter a dungeon. Release any existing controller explicitly
before running it; the check never steals a lease. The optional reference
tokenizer requires an already-installed `tiktoken`; it is not a connector
runtime dependency or a claim about the model's billing tokenizer.

The wire protocol is newline-delimited JSON over IPv4 loopback TCP, not an
HTTP/browser endpoint. Every request includes `id`, `token`, `method` and object
`params`; responses contain matching `id` and either `result` or explicit
`error`. Requests are limited to 64 KiB, responses to 8 MiB, clients to eight,
and the main-thread queue to 64. Mutation receipts are retained for the bridge
session (8192 maximum) rather than evicted into unsafe duplicate execution.
