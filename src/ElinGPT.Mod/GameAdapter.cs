using System;
using System.Collections.Generic;
using System.Linq;
using System.Runtime.CompilerServices;
using ElinGPT.Core;
using Newtonsoft.Json.Linq;
using UnityEngine;

namespace ElinGPT.Mod
{
    internal sealed class GameAdapter : IGameAdapter
    {
        private sealed class ContextHandle
        {
            public int X, Z, Index;
            public ActInput Input;
            public bool Walk;
            public string Signature = "";
            public string HeldSignature = "";
            public long Revision;
        }

        private readonly UiAdapter ui = new UiAdapter();
        private readonly Dictionary<string, ContextHandle> contexts = new Dictionary<string, ContextHandle>();
        private readonly Dictionary<string, JObject> knownEntities = new Dictionary<string, JObject>();
        private Chara? actionOwner;
        private AIAct? ownedGoal;
        private AIAct? initialUiGoal;
        private bool awaitingUiGoal;
        private Point? destination;
        private int firstFrame;
        private int epoch;
        private string generation = "";
        private bool actionStarted;
        public bool DecisionPause { get; private set; }
        public bool QuitRequested { get; private set; }
        public int Radius { get; set; } = 6;

        public void Invalidate()
        {
            epoch++;
            contexts.Clear();
            knownEntities.Clear();
            ui.Invalidate();
        }

        public UiKeyInput UiKeys => ui.Keys;
        public Vector3? UiPointerPosition => ui.PointerPosition;
        public void ApplyUiPointerContext() => ui.ApplyPointerContext();
        public void AdvanceUiPointerInput() => ui.AdvancePointerInput();
        public JObject ReadUi(JObject args) => ui.Read(args);

        public void MarkQuitting() { QuitRequested = true; }

        private bool Playing => EClass.core != null && EClass.core.IsGameStarted &&
            EClass.game != null && !EClass.game.isLoading && EClass.game.activeZone != null &&
            EClass.pc != null && EClass._map != null && EClass.scene != null;

        private static JObject Position(Point pos) => new JObject { ["x"] = pos.x, ["z"] = pos.z };
        private string Handle(Card card) => generation + ":" + card.uid;

        private JObject Entity(Card card, string location)
        {
            var entity = new JObject
            {
                ["handle"] = Handle(card),
                ["name"] = UiAdapter.Clean(card.Name),
                ["kind"] = card.isChara ? "character" : "item",
                ["location"] = location,
                ["position"] = Position(card.pos),
                ["direction"] = card.dir
            };
            if (card.isThing)
            {
                entity["count"] = card.Num;
                entity["identified"] = card.IsIdentified;
                entity["placement_state"] = card.placeState.ToString();
                if (location == "inventory") entity["equipped"] = card.Thing.isEquipped;
            }
            if (card is Chara chara) entity["hostile"] = chara.IsHostile(EClass.pc);
            knownEntities[Handle(card)] = entity;
            return entity;
        }

        public WorldFrame Read()
        {
            var frame = new WorldFrame();
            knownEntities.Clear();
            if (!Playing)
            {
                frame.Generation = epoch + ":unavailable";
                frame.Status = EClass.core == null ? "initializing" :
                    EClass.game != null && EClass.game.isLoading ? "loading" : "title";
                var menu = ui.Read();
                frame.HasUI = menu["active"]!.Value<bool>();
                frame.Data = new JObject
                {
                    ["game_version"] = EClass.core == null ? null : EClass.core.version.GetText(),
                    ["ui"] = menu
                };
                frame.RevisionData = UiRevision.Create(frame.Data);
                return frame;
            }
            generation = epoch + ":" + RuntimeHelpers.GetHashCode(EClass.game) + ":" + EClass._zone.uid;
            var pc = EClass.pc;
            CaptureDeferredUiGoal();
            var uiState = ui.Read();
            bool hasUI = uiState["active"]!.Value<bool>();
            frame.Generation = generation;
            frame.Status = pc.isDead ? "dead" : "playing";
            frame.Ready = !pc.isDead && EClass.player.CanAcceptInput() &&
                EClass.player.renderExtraTime <= 0 && !Game.IsWaiting && EClass.scene.actionMode is AM_Adv &&
                !EClass.ui.IsPauseGame && !hasUI;
            frame.HasUI = hasUI;
            frame.Turn = pc.turn;
            frame.Hp = pc.hp;
            var entities = new JArray();
            foreach (var chara in EClass._map.charas)
            {
                if (chara == pc || pc.pos.Distance(chara.pos) > Radius || !EClass.player.CanSee(chara)) continue;
                var target = new PointTarget { mouse = false };
                target.pos.Set(chara.pos);
                if (!target.ShouldIgnore(chara)) entities.Add(Entity(chara, "map"));
                if (entities.Count >= 128) break;
            }
            foreach (var item in EClass._map.things)
            {
                if (entities.Count >= 192) break;
                if (pc.pos.Distance(item.pos) > Radius || !item.pos.IsValid || !pc.CanSeeSimple(item.pos) ||
                    !item.pos.cell.pcSync || !pc.CanSee(item) ||
                    item.isHidden || item.isMasked || item.isRoofItem) continue;
                entities.Add(Entity(item, "map"));
            }
            var inventory = new JArray();
            foreach (var item in pc.things.Take(128)) inventory.Add(Entity(item, "inventory"));
            var cells = new JArray();
            var groundObjects = new JArray();
            var regionMap = EClass._zone.IsRegion ? EClass.scene.elomap : null;
            for (int x = pc.pos.x - Radius; x <= pc.pos.x + Radius; x++)
                for (int z = pc.pos.z - Radius; z <= pc.pos.z + Radius; z++)
                {
                    var point = new Point(x, z);
                    if (!point.IsValid) continue;
                    var globalX = regionMap == null ? 0 : x + regionMap.minX;
                    var globalZ = regionMap == null ? 0 : z + regionMap.minY;
                    var terrainInfo = regionMap?.GetTileInfo(globalX, globalZ);
                    var site = regionMap?.GetZone(globalX, globalZ);
                    var groundObject = point.cell.sourceObj;
                    if (groundObject != null && groundObject.id != 0)
                        groundObjects.Add(new JObject
                        {
                            ["x"] = x,
                            ["z"] = z,
                            ["name"] = UiAdapter.Clean(groundObject.GetName()),
                            ["id"] = groundObject.id,
                            ["variant"] = point.cell.objDir,
                            ["tile_type"] = groundObject.tileType?.GetType().Name
                        });
                    cells.Add(new JObject
                    {
                        ["x"] = x,
                        ["z"] = z,
                        ["floor"] = point.cell.sourceFloor.id,
                        ["terrain_name"] = terrainInfo?.source == null ? null : UiAdapter.Clean(terrainInfo.name),
                        ["site_name"] = site != null && site.CanInspect ? UiAdapter.Clean(site.Name) : null,
                        ["block"] = point.cell.sourceBlock.id,
                        ["floor_variant"] = point.cell.floorDir,
                        ["block_variant"] = point.cell.blockDir,
                        ["block_type"] = point.cell.sourceBlock.tileType?.GetType().Name,
                        ["block_axis"] = PlacementText.WallAxis(point.cell.HasWallOrFence, point.cell.blockDir),
                        ["block_shape"] = point.cell.HasWallOrFence ? new JObject
                        {
                            ["full_wall"] = point.cell.fullWall,
                            ["cross_wall"] = point.cell.crossWall,
                            ["toggle_pillar"] = point.cell.isToggleWallPillar,
                            ["hide_pillar"] = point.cell.hidePillar
                        } : null,
                        ["has_door"] = point.cell.hasDoor,
                        ["object"] = point.cell.obj,
                        ["object_variant"] = point.cell.objDir,
                        ["blocked"] = point.IsBlocked,
                        ["water"] = point.IsWater
                    });
                }
            var conditions = new JArray();
            foreach (var condition in pc.conditions.Take(32))
                conditions.Add(new JObject
                {
                    ["name"] = UiAdapter.Clean(condition.GetText()),
                    ["phase"] = condition.GetPhase()
                });
            var messages = new JArray();
            foreach (var pair in EClass.game.log.dict.OrderBy(p => p.Key).Reverse().Take(20).Reverse())
                messages.Add(new JObject { ["index"] = pair.Key, ["text"] = UiAdapter.Clean(pair.Value.text, 600) });
            frame.Data = new JObject
            {
                ["game_version"] = EClass.core.version.GetText(),
                ["session"] = new JObject
                {
                    ["save_id"] = Game.id,
                    ["save_count"] = EClass.game.saveCount,
                    ["cloud"] = EClass.game.isCloud
                },
                ["turn"] = pc.turn,
                ["time"] = EClass.world.date.GetRaw(),
                ["zone"] = new JObject
                {
                    ["name"] = UiAdapter.Clean(EClass._zone.Name),
                    ["uid"] = EClass._zone.uid,
                    ["region"] = EClass._zone.IsRegion
                },
                ["player"] = new JObject
                {
                    ["name"] = UiAdapter.Clean(pc.Name),
                    ["position"] = Position(pc.pos),
                    ["direction"] = pc.dir,
                    ["hp"] = pc.hp,
                    ["max_hp"] = pc.MaxHP,
                    ["mana"] = pc.mana.value,
                    ["max_mana"] = pc.mana.max,
                    ["stamina"] = pc.stamina.value,
                    ["max_stamina"] = pc.stamina.max,
                    ["hunger"] = pc.hunger.value,
                    ["conditions"] = conditions
                },
                ["entities"] = entities,
                ["inventory"] = inventory,
                ["held_item"] = EClass.player.currentHotItem?.Thing is Thing held ? Entity(held, "held") : null,
                ["placement"] = HeldRecipe() is Recipe recipe ? Placement(recipe) : null,
                ["tiles"] = cells,
                ["ground_objects"] = groundObjects,
                ["messages"] = messages,
                ["ui"] = uiState,
                ["limits"] = new JObject
                {
                    ["radius"] = Radius, ["inventory"] = 128, ["entities"] = 192,
                    ["terrain_visibility_filtered"] = false
                },
                ["ready"] = frame.Ready
            };
            frame.RevisionData = UiRevision.Create(frame.Data);
            return frame;
        }

        public JObject Inspect(JObject args)
        {
            string handle = Protocol.Text(args, "handle", 256);
            if (!knownEntities.TryGetValue(handle, out var entity))
                throw new BridgeException("unknown_entity", "Entity is not currently visible or in the accessible player inventory.");
            return (JObject)entity.DeepClone();
        }

        private ActPlan Plan(int x, int z, ActInput input, bool requireVisible = true)
        {
            if (!Playing) throw new BridgeException("not_playing", "No loaded game.");
            var point = new Point(x, z);
            if (!point.IsValid || EClass.pc.pos.Distance(point) > Radius)
                throw new BridgeException("invalid_target", "Target is outside the local map/action radius.");
            if (requireVisible && (!EClass.pc.CanSeeSimple(point) || !point.cell.pcSync))
                throw new BridgeException("unseen_target", "Action planning is restricted to currently visible tiles.");
            var target = new PointTarget { mouse = false };
            target.Update(point);
            var plan = new ActPlan { input = input };
            var cc = Act.CC;
            var tc = Act.TC;
            var tp = new Point(Act.TP);
            var mouseTarget = EClass.scene.mouseTarget;
            try
            {
                // Held-item planners consult the global target as well as their argument.
                EClass.scene.mouseTarget = target;
                plan._Update(target);
            }
            finally
            {
                EClass.scene.mouseTarget = mouseTarget;
                Act.CC = cc; Act.TC = tc; Act.TP.Set(tp);
            }
            return plan;
        }

        private static Recipe? HeldRecipe() =>
            EClass.player.currentHotItem is HotItemHeld hot && hot.Thing != null &&
            ReferenceEquals(hot.Thing, EClass.pc.held) ? HotItemHeld.recipe : null;

        private static JObject Placement(Recipe recipe) => new JObject
        {
            ["recipe"] = recipe.id,
            ["direction"] = recipe._dir,
            ["tile_type"] = recipe.tileType.GetType().Name,
            ["axis"] = PlacementText.WallAxis(recipe.IsWallOrFence, recipe._dir),
            ["can_rotate"] = recipe.CanRotate,
            ["rotate_action"] = recipe.CanRotate ? "rotate_held" : null
        };

        private static string HeldSignature() =>
            EClass.player.currentHotItem?.Thing?.uid + ":" + EClass.pc.held?.uid + ":" +
            (HeldRecipe() is Recipe recipe ? Protocol.Serialize(Placement(recipe)) : "");

        private static string Signature(ActPlan.Item item) =>
            item.act.GetType().FullName + ":" + item.act.ID + ":" + item.tc?.uid + ":" +
            UiAdapter.Clean(item.GetTextContext(true)) +
            (item.act is TaskBuild build ? ":" + Protocol.Serialize(Placement(build.recipe)) + ":" + build.held?.uid : "");

        public JArray AvailableActions(JObject args, long revision)
        {
            if (!Playing || EClass.pc.isDead) throw new BridgeException("not_ready", "Wait until the player can act.");
            // Native planning updates shared build previews, including door auto-alignment.
            // Discovery must not change the selected orientation or the spectator's preview.
            var recipe = HeldRecipe();
            int direction = recipe?._dir ?? 0;
            var task = HotItemHeld.taskBuild;
            var mode = ActionMode.Build;
            var mold = mode.mold;
            var buildRecipe = mode.recipe;
            int buildDirection = buildRecipe?._dir ?? 0;
            int altitude = mode.altitude, bridgeHeight = mode.bridgeHeight;
            string heldSignature = HeldSignature();
            try { return DiscoverActions(args, revision, heldSignature); }
            finally
            {
                HotItemHeld.taskBuild = task;
                mode.mold = mold;
                mode.recipe = buildRecipe;
                mode.altitude = altitude;
                mode.bridgeHeight = bridgeHeight;
                if (recipe != null && recipe._dir != direction) recipe.SetDir(direction);
                if (buildRecipe != null && buildRecipe._dir != buildDirection) buildRecipe.SetDir(buildDirection);
            }
        }

        private JArray DiscoverActions(JObject args, long revision, string heldSignature)
        {
            if (!Playing || !EClass.player.CanAcceptInput()) throw new BridgeException("not_ready", "Wait until the player can act.");
            int x = (int)Protocol.Integer(args, "x", 0, EClass._map.Size - 1);
            int z = (int)Protocol.Integer(args, "z", 0, EClass._map.Size - 1);
            var point = new Point(x, z);
            if (!point.IsValid || !EClass.pc.CanSeeSimple(point) || !point.cell.pcSync)
                throw new BridgeException("unseen_target", "Action discovery is restricted to currently visible tiles.");
            if (contexts.Count >= 512) contexts.Clear();
            var result = new JArray();
            var signatures = new HashSet<string>();
            foreach (ActInput input in new[] { ActInput.AllAction }.Concat(Enum.GetValues(typeof(ActInput)).Cast<ActInput>()).Distinct())
            {
                var plan = Plan(x, z, input);
                for (int index = 0; index < plan.list.Count && result.Count < 32; index++)
                {
                    var item = plan.list[index];
                    string signature = Signature(item);
                    if (item.HideHint || !signatures.Add(signature)) continue;
                    string handle = Guid.NewGuid().ToString("N");
                    contexts[handle] = new ContextHandle
                    {
                        X = x,
                        Z = z,
                        Index = index,
                        Input = input,
                        Signature = signature,
                        HeldSignature = heldSignature,
                        Revision = revision
                    };
                    var action = new JObject
                    {
                        ["id"] = handle,
                        ["label"] = UiAdapter.Clean(item.GetTextContext(true)),
                        ["kind"] = "context",
                        ["input"] = input.ToString(),
                        ["x"] = x,
                        ["z"] = z
                    };
                    if (item.act is TaskBuild build)
                    {
                        build.owner = EClass.pc;
                        var placement = Placement(build.recipe);
                        bool rotatesBlock = build.CanRotateBlock();
                        placement["operation"] = rotatesBlock ? "rotate_existing_block" : "place";
                        if (rotatesBlock) placement["target_block_variant"] = point.cell.blockDir;
                        action["placement"] = placement;
                    }
                    result.Add(action);
                }
            }
            if (point.IsInBounds && !point.Equals(EClass.pc.pos) && EClass.pc.CanMoveTo(point) &&
                !EClass.pc.IsEnemyOnPath(point, cancelAI: false))
            {
                string handle = Guid.NewGuid().ToString("N");
                contexts[handle] = new ContextHandle { X = x, Z = z, Walk = true, Revision = revision };
                result.Add(new JObject
                {
                    ["id"] = handle, ["label"] = "Walk here", ["kind"] = "context",
                    ["x"] = x, ["z"] = z
                });
            }
            return result;
        }

        private void WalkTo(Point point)
        {
            if (!point.IsValid || !point.IsInBounds || EClass.pc.pos.Distance(point) > Radius ||
                !EClass.pc.CanSeeSimple(point) || !point.cell.pcSync)
                throw new BridgeException("unseen_target", "Walking is restricted to currently visible local tiles.");
            if (ActionMode.Adv.IsEntangled() || EClass.player.TooHeavyToMove() ||
                !EClass.pc.CanMoveTo(point) || EClass.pc.IsEnemyOnPath(point, cancelAI: false))
                throw new BridgeException("movement_blocked", "No legal walk to this tile.");
            destination = point.Copy();
            EClass.pc.SetAIImmediate(new AI_Goto(point, 0));
        }

        private void Perform(ActPlan.Item item)
        {
            var oldTarget = EClass.scene.mouseTarget;
            var target = new PointTarget { mouse = false };
            target.Update(item.pos);
            EClass.scene.mouseTarget = target;
            try
            {
                if (item.act is TaskBuild build && !build.CanRotateBlock() && build.held is Thing &&
                    ReferenceEquals(build.recipe, HeldRecipe()))
                {
                    // Preserve the native recipe (including block type) while isolating its mutable direction.
                    build.recipe = (Recipe)typeof(object).GetMethod("MemberwiseClone",
                        System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic)!
                        .Invoke(build.recipe, null)!;
                }
                if (!item.act.CanPerform(EClass.pc, item.tc, item.pos))
                    throw new BridgeException("illegal_action", "Game rejected the action on revalidation.");
                if (item.act is AIAct ai) ai.owner = EClass.pc;
                if (item.Perform()) EClass.player.EndTurn(consume: false);
            }
            finally { EClass.scene.mouseTarget = oldTarget; }
        }

        public void Execute(JObject args, long revision)
        {
            if (!Playing || EClass.pc.isDead) throw new BridgeException("not_ready", "Wait until the player can act.");
            awaitingUiGoal = false;
            string kind = Protocol.Text(args, "kind", 32);
            destination = null;
            actionOwner = EClass.pc;
            firstFrame = Time.frameCount;
            try
            {
                switch (kind)
                {
                    case "wait":
                        if (!ACT.Wait.Perform(EClass.pc)) throw new BridgeException("illegal_action", "Wait action was rejected.");
                        EClass.player.EndTurn(consume: false);
                        break;
                    case "step":
                        int dx = (int)Protocol.Integer(args, "dx", -1, 1);
                        int dz = (int)Protocol.Integer(args, "dz", -1, 1);
                        if (dx == 0 && dz == 0) throw new BridgeException("invalid_argument", "Use wait for a zero-length step.");
                        if (ActionMode.Adv.IsEntangled() || EClass.player.TooHeavyToMove())
                            throw new BridgeException("movement_blocked", "Player is entangled or encumbered.");
                        var point = new Point(EClass.pc.pos.x + dx, EClass.pc.pos.z + dz);
                        if (!point.IsInBounds)
                        {
                            if (!EClass.pc.HasNoGoal || !EClass.player.CanExitBorder(point))
                                throw new BridgeException("movement_blocked", "Game does not permit exiting this zone.");
                            EClass.player.ExitBorder(null);
                            break;
                        }
                        var stepPlan = Plan(point.x, point.z, ActInput.Key, requireVisible: false);
                        if (stepPlan.HasAct) Perform(stepPlan.list[0]);
                        else
                        {
                            if (!EClass.pc.CanMoveTo(point) || EClass.pc.IsEnemyOnPath(point, cancelAI: false))
                                throw new BridgeException("movement_blocked", "No legal step to this tile.");
                            destination = point.Copy();
                            EClass.pc.SetAIImmediate(new AI_Goto(point, 0));
                        }
                        break;
                    case "context":
                        string handle = Protocol.Text(args, "action_handle", 128);
                        if (!contexts.TryGetValue(handle, out var context) || context.Revision != revision)
                            throw new BridgeException("stale_action", "Discover actions again at the current revision.");
                        if (context.Walk)
                        {
                            WalkTo(new Point(context.X, context.Z));
                            break;
                        }
                        if (context.HeldSignature != HeldSignature())
                            throw new BridgeException("stale_action", "The held item or placement orientation changed; discover actions again.");
                        var plan = Plan(context.X, context.Z, context.Input);
                        if (context.Index >= plan.list.Count || Signature(plan.list[context.Index]) != context.Signature)
                            throw new BridgeException("stale_action", "The target's context actions changed.");
                        Perform(plan.list[context.Index]);
                        break;
                    case "rotate_held":
                        var heldRecipe = HeldRecipe();
                        if (heldRecipe == null || !heldRecipe.CanRotate)
                            throw new BridgeException("illegal_action", "The currently held placement recipe cannot rotate.");
                        heldRecipe.Rotate();
                        break;
                    case "open_ui":
                        string menu = Protocol.Text(args, "menu", 32);
                        CoreConfig.GameFunc function;
                        switch (menu)
                        {
                            case "inventory": function = CoreConfig.GameFunc.ShowInv; break;
                            case "character": function = CoreConfig.GameFunc.ShowChara; break;
                            case "abilities": function = CoreConfig.GameFunc.ShowAbility; break;
                            case "journal": function = CoreConfig.GameFunc.ShowJournal; break;
                            default: throw new BridgeException("unsupported_menu", "Supported menus: inventory, character, abilities, journal.");
                        }
                        EClass.scene.actionMode.DoFunc(function);
                        break;
                    default: throw new BridgeException("unsupported_action", "Supported actions: wait, step, context, rotate_held, open_ui.");
                }
            }
            finally { ownedGoal = EClass.pc.isDead || EClass.pc.HasNoGoal ? null : EClass.pc.ai; }
            actionStarted = true;
        }

        public void ChooseUI(JObject args)
        {
            actionOwner = Playing ? EClass.pc : null;
            initialUiGoal = Playing ? EClass.pc.ai : null;
            // Native death/revival goals must not be adopted as cancellable gameplay work.
            awaitingUiGoal = Playing && !EClass.pc.isDead;
            destination = null;
            firstFrame = Time.frameCount;
            try { ui.Act(args); }
            finally
            {
                if (awaitingUiGoal && Playing && !EClass.pc.isDead && ReferenceEquals(actionOwner, EClass.pc) && !EClass.pc.HasNoGoal &&
                    !ReferenceEquals(EClass.pc.ai, initialUiGoal)) ownedGoal = EClass.pc.ai;
            }
            actionStarted = true;
        }

        private void CaptureDeferredUiGoal()
        {
            if (!actionStarted || !awaitingUiGoal || !Playing || EClass.pc.isDead || !ReferenceEquals(actionOwner, EClass.pc)) return;
            if (!EClass.pc.HasNoGoal && !ReferenceEquals(EClass.pc.ai, initialUiGoal))
                ownedGoal = EClass.pc.ai;
        }

        public JObject SetUiText(JObject args)
        {
            var command = new JObject
            {
                ["operation"] = "set_text",
                ["node_id"] = args["field_id"],
                ["value"] = args["value"]
            };
            ui.Act(command);
            return new JObject { ["status"] = "updated", ["node_id"] = args["field_id"] };
        }

        public JObject SessionCommand(JObject args)
        {
            string command = Protocol.Text(args, "command", 32);
            switch (command)
            {
                case "save":
                    if (!Playing) throw new BridgeException("not_playing", "Load a game before saving.");
                    int previousCount = EClass.game.saveCount;
                    if (!EClass.game.Save() || EClass.game.saveCount != previousCount + 1)
                        throw new BridgeException("save_failed", "The native game save failed or did not complete.");
                    return new JObject
                    {
                        ["command"] = command,
                        ["status"] = "saved",
                        ["save_id"] = Game.id,
                        ["save_count"] = EClass.game.saveCount
                    };
                case "return_to_title":
                    if (!Playing) throw new BridgeException("not_playing", "Already outside a loaded game.");
                    EClass.game.GotoTitle();
                    return new JObject
                    {
                        ["command"] = command,
                        ["status"] = "awaiting_confirmation",
                        ["saves_on_confirm"] = true
                    };
                case "quit":
                    if (Playing)
                    {
                        EClass.game.Quit();
                        return new JObject
                        {
                            ["command"] = command,
                            ["status"] = "awaiting_confirmation",
                            ["saves_on_confirm"] = true
                        };
                    }
                    if (EClass.core == null || EClass.core.IsGameStarted || EClass.game?.isLoading == true)
                        throw new BridgeException("not_ready", "Wait for loading to finish before quitting.");
                    EClass.core.Quit();
                    return new JObject { ["command"] = command, ["status"] = "quitting", ["quitting"] = true };
                case "pause_menu":
                    if (!Playing) throw new BridgeException("not_playing", "No loaded game has a pause menu.");
                    EClass.ui.AddLayer<LayerPauseMenu>();
                    return new JObject { ["command"] = command, ["status"] = "menu_open" };
                default: throw new BridgeException("unsupported_command", "Supported session commands: save, return_to_title, quit, pause_menu.");
            }
        }

        public ActionOutcome? PollAction(WorldFrame frame)
        {
            CaptureDeferredUiGoal();
            if (!actionStarted || Time.frameCount <= firstFrame + 1 || ui.HasQueuedInput) return null;
            bool runningGoal = Playing && ownedGoal != null && ReferenceEquals(EClass.pc.ai, ownedGoal) && ownedGoal.IsRunning;
            if (runningGoal) return null;
            if (!frame.Ready && frame.Status != "title" && !(frame.HasUI && !runningGoal)) return null;
            actionStarted = false;
            awaitingUiGoal = false;
            ownedGoal = null;
            if (destination != null && !EClass.pc.pos.Equals(destination))
                return new ActionOutcome("interrupted", "Movement ended before the requested tile was reached.");
            return new ActionOutcome("completed", "Game returned to a playable decision boundary.");
        }

        public void CancelOwnedAction()
        {
            CaptureDeferredUiGoal();
            try
            {
                if (actionOwner != null && ownedGoal != null && ReferenceEquals(actionOwner.ai, ownedGoal))
                {
                    ownedGoal.Cancel();
                    if (ReferenceEquals(actionOwner.ai, ownedGoal)) actionOwner.SetNoGoal();
                }
            }
            finally
            {
                ui.CancelQueuedInput();
                actionStarted = false;
                awaitingUiGoal = false;
                initialUiGoal = null;
                ownedGoal = null;
                actionOwner = null;
                destination = null;
            }
        }

        public void SetDecisionPause(bool paused) { DecisionPause = paused; }

    }
}
