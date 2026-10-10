using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net.Sockets;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using ElinGPT.Core;
using Newtonsoft.Json.Linq;

namespace ElinGPT.Tests
{
    internal sealed class FakeGame : IGameAdapter
    {
        public int Executions, Cancellations, Turn;
        public int Hp = 100;
        public bool Ready = true, Menu, Pause, Complete, ThrowDuringExecute;
        public bool QuitRequested { get; private set; }
        public int Saves, TextEdits, UiExecutions, SessionCommands;
        public Action<JObject>? UiAction;
        public bool GeometryOnly;
        public int Geometry;
        public string Generation = "game:zone:1";
        public string Status = "playing";
        public int ThreadId = Environment.CurrentManagedThreadId;
        private void CheckThread()
        {
            if (Environment.CurrentManagedThreadId != ThreadId) throw new Exception("Game access escaped the main thread");
        }
        public WorldFrame Read()
        {
            CheckThread();
            return new WorldFrame
            {
                Generation = Generation,
                Status = Status,
                Ready = Ready,
                HasUI = Menu,
                Turn = Turn,
                Hp = Hp,
                RevisionData = GeometryOnly ? new JObject { ["stable"] = true } : null,
                Data = new JObject
                {
                    ["turn"] = Turn,
                    ["hp"] = Hp,
                    ["geometry"] = Geometry,
                    ["player"] = new JObject { ["name"] = "Test\u300c\u03bb\u300d", ["position"] = new JObject { ["x"] = 5, ["z"] = 5 } },
                    ["ui"] = new JObject
                    {
                        ["active"] = Menu,
                        ["format"] = "gameobject_tree",
                        ["roots"] = new JArray("canvas"),
                        ["nodes"] = new JArray(new JObject { ["id"] = "canvas", ["parent_id"] = null, ["role"] = "canvas" })
                    }
                }
            };
        }
        public JObject Inspect(JObject args) { CheckThread(); return new JObject { ["name"] = "Test item" }; }
        public JObject ReadUi(JObject args) { CheckThread(); return (JObject)Read().Data["ui"]!.DeepClone(); }
        public JArray AvailableActions(JObject args, long revision)
        {
            CheckThread();
            return new JArray(new JObject { ["id"] = "context-1", ["label"] = "Talk" });
        }
        public void Execute(JObject args, long revision)
        {
            CheckThread();
            Executions++;
            Ready = false;
            Turn++;
            if (ThrowDuringExecute) throw new InvalidOperationException("Adapter failed after starting");
        }
        public void ChooseUI(JObject args)
        {
            CheckThread();
            UiExecutions++;
            if (UiAction != null) UiAction(args);
            else { Menu = false; Ready = true; Complete = true; }
        }
        public void RequestQuit() { QuitRequested = true; }
        public JObject SetUiText(JObject args)
        {
            CheckThread();
            TextEdits++;
            return new JObject { ["status"] = "updated", ["value"] = args["value"] };
        }
        public JObject SessionCommand(JObject args)
        {
            CheckThread();
            SessionCommands++;
            string command = Protocol.Text(args, "command");
            if (command == "save")
            {
                if (Status != "playing") throw new BridgeException("not_playing", "No game to save.");
                Saves++;
                return new JObject { ["status"] = "saved", ["save_count"] = Saves };
            }
            if (command == "quit" && Status == "title")
            {
                QuitRequested = true;
                return new JObject { ["status"] = "quitting", ["quitting"] = true };
            }
            Menu = true;
            return new JObject { ["status"] = "awaiting_confirmation" };
        }
        public ActionOutcome? PollAction(WorldFrame frame)
        {
            CheckThread();
            if (!Complete) return null;
            Ready = Status == "playing" && !Menu;
            return new ActionOutcome("completed", "Test action completed");
        }
        public void CancelOwnedAction() { CheckThread(); Cancellations++; Ready = Status == "playing"; }
        public void SetDecisionPause(bool paused) { CheckThread(); Pause = paused; }
    }

    internal static class Program
    {
        private static int checks;
        private static void Check(bool condition, string message)
        {
            if (!condition) throw new Exception(message);
            checks++;
        }
        private static JObject Request(string method, JObject? args = null, string? id = null) =>
            new JObject { ["id"] = id ?? Guid.NewGuid().ToString("N"), ["method"] = method, ["params"] = args ?? new JObject() };
        private static JObject Send(GameBridge bridge, string method, JObject? args = null, string client = "agent", string? id = null) =>
            bridge.Handle(client, Request(method, args, id));
        private static JObject Result(JObject response) =>
            response["result"] as JObject ?? throw new Exception("Unexpected error: " + response);
        private static string? Error(JObject response) => response["error"]?["code"]?.Value<string>();
        private static JObject Revision(GameBridge bridge) =>
            new JObject { ["session_id"] = bridge.SessionId, ["expected_revision"] = Result(Send(bridge, "observe"))["revision"] };
        private static JObject Act(GameBridge bridge)
        {
            var args = Revision(bridge); args["kind"] = "wait"; return args;
        }
        private static GameBridge Make(FakeGame game, Func<DateTime>? clock = null, List<Exception>? errors = null) =>
            new GameBridge(game, e => { if (errors != null) errors.Add(e); else throw new Exception("Unexpected adapter error", e); }, clock);

        private static void ControllerChecks()
        {
            var game = new FakeGame();
            var now = DateTime.UtcNow;
            var bridge = Make(game, () => now);
            bridge.Tick();
            Check(Error(Send(bridge, "act", Act(bridge))) == "not_controller", "Unowned action must fail");
            Check(Result(Send(bridge, "claim", new JObject { ["name"] = "Watcher" }))["controller"]!.Value<string>() == "Watcher",
                "Claim control");
            Check(game.Pause, "Claim pauses at decision boundary");
            Check(Error(Send(bridge, "claim", new JObject { ["name"] = "Other" }, "other")) == "control_busy", "Exclusive owner");
            var args = Act(bridge);
            var start = Send(bridge, "act", args, id: "one");
            string action = Result(start)["action"]!["action_id"]!.Value<string>()!;
            Check(game.Executions == 1, "Action executed once");
            Check(JToken.DeepEquals(start, Send(bridge, "act", args, id: "one")), "Duplicate has identical receipt");
            var reordered = new JObject
            {
                ["kind"] = "wait",
                ["expected_revision"] = args["expected_revision"],
                ["session_id"] = args["session_id"]
            };
            Check(JToken.DeepEquals(start, Send(bridge, "act", reordered, id: "one")), "Property order does not change request identity");
            Check(game.Executions == 1, "Duplicate did not execute");
            var changed = (JObject)args.DeepClone(); changed["kind"] = "step";
            Check(Error(Send(bridge, "act", changed, id: "one")) == "request_id_conflict", "Conflicting request ID rejected");
            Check(Error(Send(bridge, "act", Act(bridge))) == "action_busy", "No overlapping world actions");
            game.Ready = true;
            bridge.Tick();
            Check(!game.Pause, "A running job is not paused at an intermediate input-ready frame");
            game.Ready = false;
            game.Menu = true; bridge.Tick();
            Check(Result(Send(bridge, "action_status", new JObject { ["action_id"] = action }))["action"]!["status"]!.Value<string>() ==
                "awaiting_ui", "Action waits for UI rather than claiming completion");
            Check(game.Pause, "A native UI decision still pauses a running job");
            var choose = Revision(bridge); choose["choice_id"] = "yes";
            Check(Result(Send(bridge, "ui_choose", choose))["action"]!["action_id"]!.Value<string>() == action, "UI resumes same action");
            bridge.Tick();
            Check(Result(Send(bridge, "action_status", new JObject { ["action_id"] = action }))["action"]!["status"]!.Value<string>() ==
                "completed", "Action completion observed");
            Check(game.Pause, "Pause restored at completion");
            game.Ready = false;
            bridge.Tick();
            Check(!game.Pause, "A closing native overlay can finish its wake/load transition after action completion");
            game.Menu = true;
            bridge.Tick();
            Check(game.Pause, "A subsequent UI decision still pauses without an active action");
            game.Menu = false;
            game.Ready = true;
            bridge.Tick();
            Check(game.Pause, "Transition completion restores the idle decision pause");
            var stale = Act(bridge); game.Hp--;
            Check(Error(Send(bridge, "act", stale)) == "stale_observation", "Stale revision rejected");
            Result(Send(bridge, "act", Act(bridge))); game.Hp--; bridge.Tick();
            Check(game.Cancellations == 1, "Damage cancels owned activity");
            game.Complete = false; game.Ready = true;
            Result(Send(bridge, "act", Act(bridge))); game.Generation = "new-zone"; bridge.Tick();
            Check(game.Cancellations == 2, "Transition cancels owned activity");
            game.Ready = true;
            Result(Send(bridge, "act", Act(bridge))); bridge.Tick(); now = now.AddSeconds(21); bridge.Tick();
            Check(game.Cancellations == 3, "Action time budget is enforced");
            now = now.AddSeconds(31); bridge.Tick();
            Check(!bridge.HasController && !game.Pause, "Lease expires and releases pause");
            Result(Send(bridge, "claim", new JObject { ["name"] = "Watcher" }));
            bridge.Disconnect("other"); Check(bridge.HasController, "Observer disconnect does not affect owner");
            bridge.Disconnect("agent"); Check(!bridge.HasController, "Owner disconnect releases control");
            Result(Send(bridge, "claim", new JObject { ["name"] = "Watcher" }));
            game.Status = "unavailable"; bridge.Tick();
            Check(!bridge.HasController && !game.Pause, "Unavailable game releases ownership and agent pause");
        }

        private static void DeathChecks()
        {
            foreach (bool fatalUi in new[] { false, true })
            {
                var game = new FakeGame();
                var bridge = Make(game);
                Result(Send(bridge, "claim", new JObject { ["name"] = "Survivor" }));
                var start = Act(bridge);
                if (fatalUi) { start["operation"] = "right_click"; start["node_id"] = "item"; }
                string fatal = Result(Send(bridge, fatalUi ? "ui_act" : "act", start))["action"]!["action_id"]!.Value<string>()!;
                var oldRevision = Revision(bridge);
                game.Status = "dead"; game.Ready = false; game.Menu = false; game.Hp = -2;
                bridge.Tick();
                var death = Result(Send(bridge, "action_status", new JObject { ["action_id"] = fatal }));
                Check(bridge.HasController && !game.Pause, "Death keeps ownership while native death animation advances");
                Check(death["action"]!["status"]!.Value<string>() == "interrupted" && game.Cancellations == 1,
                    "Death interrupts the action that caused it, including a gameplay UI action");
                Check(death["observation"]!["status"]!.Value<string>() == "dead" &&
                    !death["observation"]!["ready"]!.Value<bool>(), "Death remains distinct from playable world state");
                Check(Error(Send(bridge, "ui_choose", oldRevision)) == "stale_observation",
                    "Pre-death observations cannot control death UI");
                Check(Error(Send(bridge, "ui_act", Revision(bridge))) == "not_ready",
                    "No UI input during a death transition without a dialog");
                Check(Error(Send(bridge, "ui_set_text", Revision(bridge))) == "not_ready",
                    "No text input during a death transition without a dialog");
                bridge.Tick();
                Check(game.Cancellations == 1, "Death cancels the previous owned action only once");

                game.Menu = true;
                bridge.Tick();
                Check(game.Pause, "Native death dialog is a paused decision boundary");
                foreach (string kind in new[] { "wait", "step", "context", "rotate_held", "open_ui" })
                {
                    var args = Revision(bridge); args["kind"] = kind;
                    Check(Error(Send(bridge, "act", args)) == "not_ready", "World actions stay blocked while dead: " + kind);
                }
                Check(Error(Send(bridge, "available_actions", Revision(bridge))) == "not_ready",
                    "Death cannot discover world actions");
                foreach (string command in new[] { "save", "pause_menu", "return_to_title", "quit" })
                {
                    var args = Revision(bridge); args["command"] = command;
                    Check(Error(Send(bridge, "session_command", args)) == "not_ready",
                        "Session commands cannot bypass the native death flow: " + command);
                }
                Check(game.SessionCommands == 0, "No native session mutation was attempted while dead");
                Check(Error(Send(bridge, "ui_act", Revision(bridge), "observer")) == "not_controller",
                    "Death dialog still requires the exclusive owner");
                var edit = Revision(bridge); edit["field_id"] = "last-words"; edit["value"] = "Try again.";
                Result(Send(bridge, "ui_set_text", edit));
                Check(game.TextEdits == 1, "Native last-words field can be edited while dead");

                game.UiAction = args => { game.Menu = true; game.Ready = false; game.Complete = true; };
                var submit = Revision(bridge); submit["operation"] = "key"; submit["key"] = "enter";
                var submitted = Send(bridge, "ui_act", submit, id: "last-words");
                string dialog = Result(submitted)["action"]!["action_id"]!.Value<string>()!;
                Check(JToken.DeepEquals(submitted, Send(bridge, "ui_act", submit, id: "last-words")),
                    "Death UI mutations have the same deduplication guarantees");
                Check(game.UiExecutions == (fatalUi ? 2 : 1), "Last-words submission was delivered only once");
                game.Generation = "death-choices";
                bridge.Tick();
                Check(Result(Send(bridge, "action_status", new JObject { ["action_id"] = dialog }))["action"]!["status"]!.Value<string>() ==
                    "completed" && bridge.HasController && game.Pause, "UI action completes at the next death dialog without releasing control");

                game.UiAction = args => { game.Menu = false; game.Ready = false; game.Complete = false; };
                var choose = Revision(bridge); choose["choice_id"] = "revive";
                string revival = Result(Send(bridge, "ui_choose", choose))["action"]!["action_id"]!.Value<string>()!;
                bridge.Tick();
                Check(bridge.HasController && !game.Pause && game.Cancellations == 1,
                    "Revival can advance through dead frames with no dialog");
                game.Status = "loading"; game.Generation = "revival-loading"; game.Menu = true; game.Complete = true;
                bridge.Tick();
                Check(bridge.HasController && !game.Pause && game.Cancellations == 1,
                    "Native revival loading is not cancelled or decision-paused");
                Check(Result(Send(bridge, "action_status", new JObject { ["action_id"] = revival }))["action"]!["status"]!.Value<string>() ==
                    "running", "Loading overlay cannot prematurely complete a revival UI action");
                game.Status = fatalUi ? "title" : "playing"; game.Generation = "revived";
                game.Hp = 15; game.Menu = fatalUi; game.Ready = !fatalUi;
                bridge.Tick();
                Check(bridge.HasController && game.Pause && game.Cancellations == 1,
                    "Native death flow can finish at either gameplay or title without losing ownership");
                Check(Result(Send(bridge, "action_status", new JObject { ["action_id"] = revival }))["action"]!["status"]!.Value<string>() ==
                    "completed", "Revival UI action completes in the resulting generation");
                if (!fatalUi)
                {
                    game.Complete = false;
                    Result(Send(bridge, "act", Act(bridge)));
                    Check(game.Executions == 2, "World actions resume only after the player is alive and ready");
                    game.Hp--; bridge.Tick();
                    Check(game.Cancellations == 2, "Damage interruption is still enforced after revival");
                }
            }

            var dead = new FakeGame { Status = "dead", Ready = false, Menu = false, Hp = -2 };
            var now = DateTime.UtcNow;
            var recovery = Make(dead, () => now);
            Check(Result(Send(recovery, "ping"))["capabilities"]!.Values<string>().Contains("death_navigation"),
                "Native death UI support is advertised to clients");
            Check(Error(Send(recovery, "claim", new JObject { ["name"] = "Recovery" })) == "not_ready",
                "New ownership waits for a native death dialog");
            dead.Menu = true;
            Result(Send(recovery, "claim", new JObject { ["name"] = "Recovery" }));
            recovery.Tick();
            Check(recovery.HasController && dead.Pause, "An already-dead game can be claimed at its dialog");
            dead.Generation = "next-death-dialog"; recovery.Tick();
            Check(recovery.HasController, "Death dialog generation changes retain idle ownership");
            recovery.Release("Human input took over.");
            Check(!recovery.HasController && !dead.Pause, "Human takeover still releases death dialog control");
            Result(Send(recovery, "claim", new JObject { ["name"] = "Recovery" }));
            now = now.AddSeconds(31); recovery.Tick();
            Check(!recovery.HasController && !dead.Pause, "Death dialog ownership still expires without heartbeats");
        }

        private static void FailureChecks()
        {
            var errors = new List<Exception>();
            var game = new FakeGame();
            var bridge = Make(game, errors: errors);
            var bad = Request("observe"); bad["params"] = new JArray();
            Check(Error(bridge.Handle("agent", bad)) == "invalid_argument", "Object parameters required");
            Check(Error(Send(bridge, "unknown")) == "unknown_method", "Unknown methods surfaced");
            Check(Error(Send(bridge, "capture")) == "unknown_method", "Image capture is not part of the text-only bridge");
            Result(Send(bridge, "claim", new JObject { ["name"] = "Watcher" }));
            game.ThrowDuringExecute = true;
            var args = Act(bridge);
            var response = Send(bridge, "act", args, id: "failed-side-effect");
            Check(Error(response) == "internal_error" && errors.Count == 1, "Adapter exception is reported");
            Check(!bridge.HasController && game.Cancellations == 1, "Partial execution fails closed");
            Check(JToken.DeepEquals(response, Send(bridge, "act", args, id: "failed-side-effect")),
                "Failed side effect has durable deduplication receipt");
            Check(game.Executions == 1, "Failure cannot be retried into a duplicate action");
            bool invalid = false;
            try { Protocol.Parse("{\"id\":1,\"id\":2}"); }
            catch (Newtonsoft.Json.JsonException) { invalid = true; }
            Check(invalid, "Duplicate JSON properties are refused");
            invalid = false;
            try { Protocol.Integer(new JObject { ["x"] = true }, "x", 0, 1); }
            catch (BridgeException) { invalid = true; }
            Check(invalid, "Booleans are not accepted as integer arguments");
        }

        private static void PumpChecks()
        {
            var game = new FakeGame();
            var pump = new RequestPump(Make(game));
            var args = new JObject { ["name"] = "Watcher" };
            var cancelled = pump.Submit("agent", Request("claim", args));
            Check(cancelled.CancelBeforeStart(), "Queued timeout can cancel before execution");
            pump.Tick(); Check(!pump.Bridge.HasController, "Cancelled queue entry does not execute");
            var pending = pump.Submit("gone", Request("claim", args));
            pump.Disconnected("gone"); pump.Tick();
            Check(Error(pending.Completion.Task.Result) == "disconnected", "Dead connection cannot acquire control from queued request");
            for (int i = 0; i < 64; i++) pump.Submit("agent", Request("observe"));
            bool full = false;
            try { pump.Submit("agent", Request("observe")); } catch (BridgeException e) { full = e.Code == "queue_full"; }
            Check(full, "Queue bound enforced");
            pump.Close();
            bool closed = false;
            try { pump.Submit("agent", Request("claim", args)); } catch (BridgeException e) { closed = e.Code == "disabled"; }
            Check(closed, "Shutdown rejects new work");
        }

        private static void SessionChecks()
        {
            var game = new FakeGame { Status = "title", Ready = false, Menu = true };
            var bridge = Make(game);
            Result(Send(bridge, "claim", new JObject { ["name"] = "Menu agent" }));
            Check(bridge.HasController, "Controller can claim the title menu");
            var edit = Revision(bridge); edit["field_id"] = "name"; edit["value"] = "Character";
            Check(Result(Send(bridge, "ui_set_text", edit))["status"]!.Value<string>() == "updated" &&
                game.TextEdits == 1, "Text UI edits are available in title/creation menus");
            Check(Result(Send(bridge, "ui_observe", new JObject { ["root_id"] = "canvas" }))["ui"]!["format"]!.Value<string>() ==
                "gameobject_tree", "Generic UI tree queries use the game adapter");
            var save = Revision(bridge); save["command"] = "save";
            Check(Error(Send(bridge, "session_command", save)) == "not_playing", "Cannot save an unloaded title state");
            var choice = Revision(bridge); choice["choice_id"] = "load";
            string action = Result(Send(bridge, "ui_choose", choice))["action"]!["action_id"]!.Value<string>()!;
            game.Generation = "loaded-save"; game.Status = "playing"; game.Ready = true; game.Menu = false;
            bridge.Tick();
            Check(bridge.HasController && game.Cancellations == 0, "UI-driven load preserves ownership without cancelling native load goals");
            Check(Result(Send(bridge, "action_status", new JObject { ["action_id"] = action }))["action"]!["status"]!.Value<string>() ==
                "completed", "UI load can complete in the new game generation");
            save = Revision(bridge); save["command"] = "save";
            var saved = Send(bridge, "session_command", save, id: "save-once");
            Check(Result(saved)["save_count"]!.Value<int>() == 1, "Native save command returns actual completion");
            Send(bridge, "session_command", save, id: "save-once");
            Check(game.Saves == 1, "Saving is deduplicated like other mutations");
            game.Status = "title"; game.Menu = true; game.Ready = false; bridge.Tick();
            Check(bridge.HasController, "Returning to title retains menu control");
            var quit = Revision(bridge); quit["command"] = "quit";
            Check(Result(Send(bridge, "session_command", quit))["quitting"]!.Value<bool>(), "Title quit is explicitly acknowledged");
        }

        private static void GeometryChecks()
        {
            var game = new FakeGame { GeometryOnly = true };
            var bridge = Make(game);
            var before = Result(Send(bridge, "observe"));
            game.Geometry++;
            var after = Result(Send(bridge, "observe"));
            Check(before["revision"]!.Value<long>() == after["revision"]!.Value<long>(),
                "Cosmetic UI geometry changes do not invalidate semantic revisions");
            Check(after["state"]!["geometry"]!.Value<int>() == 1, "Observations still include the current geometry");
            var state = JObject.Parse(@"{'ui':{'nodes':[
                {'id':'hover','pointer_hint':true,'text':'stone','visible':true},
                {'id':'dialog','text':'Accept quest?','bounds':{'x':10},'actions':['click']}
            ]},'turn':1}");
            var stable = ElinGPT.Mod.UiRevision.Create(state);
            state["ui"]!["nodes"]![0]!["text"] = "soil";
            state["ui"]!["nodes"]![0]!["visible"] = false;
            state["ui"]!["nodes"]![1]!["bounds"]!["x"] = 20;
            Check(JToken.DeepEquals(stable, ElinGPT.Mod.UiRevision.Create(state)),
                "Spectator hover text/visibility and geometry do not invalidate decisions");
            Check(state["ui"]!["nodes"]![0]!["text"]!.Value<string>() == "soil",
                "Revision projection preserves the original hover observation");
            state["ui"]!["nodes"]![1]!["text"] = "Decline quest?";
            Check(!JToken.DeepEquals(stable, ElinGPT.Mod.UiRevision.Create(state)),
                "Actual dialogue text still invalidates decisions");
            state["turn"] = 2;
            Check(ElinGPT.Mod.UiRevision.Create(state)["turn"]!.Value<int>() == 2,
                "World state remains part of the semantic revision");
        }

        private static void DeferredQuitChecks()
        {
            var game = new FakeGame { Status = "title", Menu = true, Ready = false };
            var bridge = Make(game);
            Result(Send(bridge, "claim", new JObject { ["name"] = "UI agent" }));
            var args = Revision(bridge); args["operation"] = "click"; args["node_id"] = "quit-confirm";
            string id = Result(Send(bridge, "ui_act", args))["action"]!["action_id"]!.Value<string>()!;
            game.RequestQuit();
            bridge.Tick();
            var status = Result(Send(bridge, "action_status", new JObject { ["action_id"] = id }));
            Check(status["quitting"]!.Value<bool>() && status["action"]!["status"]!.Value<string>() == "completed",
                "Deferred native UI quit exposes terminal acknowledgement before transport teardown");
        }

        private static void ProgressBudgetChecks()
        {
            var game = new FakeGame();
            var now = DateTime.UtcNow;
            var bridge = Make(game, () => now);
            Result(Send(bridge, "claim", new JObject { ["name"] = "Slow character" }));
            Result(Send(bridge, "act", Act(bridge)));
            bridge.Tick();
            for (int i = 0; i < 8; i++)
            {
                now = now.AddSeconds(15);
                game.Turn++;
                Result(Send(bridge, "heartbeat"));
                bridge.Tick();
            }
            Check(game.Cancellations == 0, "Turn progress renews the idle budget without an arbitrary 20-second cutoff");
            now = now.AddSeconds(1);
            game.Turn++;
            bridge.Tick();
            Check(game.Cancellations == 1, "Continuous progress does not bypass the hard action time cap");
        }

        private static void TransportChecks()
        {
            var game = new FakeGame();
            var pump = new RequestPump(Make(game));
            var errors = new List<Exception>();
            using (var server = new TcpBridgeServer(pump, 0, error => { lock (errors) errors.Add(error); }))
            {
                var request = Request("ping");
                request["token"] = server.Token;
                var task = Task.Run(() =>
                {
                    using (var client = new TcpClient("127.0.0.1", server.Port))
                    using (var stream = client.GetStream())
                    using (var reader = new StreamReader(stream))
                    {
                        byte[] payload = Encoding.UTF8.GetBytes(Protocol.Serialize(request) + "\n");
                        stream.Write(payload, 0, payload.Length);
                        return Protocol.Parse(reader.ReadLine()!);
                    }
                });
                var deadline = DateTime.UtcNow.AddSeconds(5);
                while (!task.IsCompleted && DateTime.UtcNow < deadline) { pump.Tick(); Thread.Sleep(5); }
                Check(task.IsCompleted && Result(task.Result)["schema_version"]!.Value<string>() == "1", "Actual TCP ping/main-thread dispatch works");
                using (var client = new TcpClient("127.0.0.1", server.Port))
                using (var stream = client.GetStream())
                using (var reader = new StreamReader(stream))
                {
                    byte[] payload = Encoding.UTF8.GetBytes("{\"id\":\"bad\",\"token\":\"wrong\",\"method\":\"claim\",\"params\":{}}\n");
                    stream.Write(payload, 0, payload.Length);
                    Check(Error(Protocol.Parse(reader.ReadLine()!)) == "unauthorized", "TCP authentication rejects invalid tokens");
                }
            }
            pump.Tick();
            pump.Close();
        }

        private static void CredentialChecks()
        {
            if (!OperatingSystem.IsWindows()) return;
            string path = Path.Combine(Path.GetTempPath(), "ElinGPT-permissions-" + Guid.NewGuid().ToString("N"));
            try
            {
                ElinGPT.Mod.CredentialDirectory.CreatePrivate(path);
                var security = new DirectoryInfo(path).GetAccessControl();
                Check(security.AreAccessRulesProtected, "Credential directory does not inherit other users' access");
                var rules = security.GetAccessRules(true, true, typeof(SecurityIdentifier));
                var user = WindowsIdentity.GetCurrent().User ?? throw new Exception("No test user SID");
                Check(rules.Count == 1 && rules[0] is FileSystemAccessRule directoryRule && directoryRule.IdentityReference.Equals(user),
                    "Credential directory grants only the current user");
                string file = Path.Combine(path, "connection.json");
                File.WriteAllText(file, "{}");
                var fileRules = new FileInfo(file).GetAccessControl().GetAccessRules(true, true, typeof(SecurityIdentifier));
                Check(fileRules.Count == 1 && fileRules[0] is FileSystemAccessRule fileRule && fileRule.IdentityReference.Equals(user),
                    "Credential file inherits only the current user's grant");
            }
            finally { if (Directory.Exists(path)) Directory.Delete(path, true); }
        }

        private static void MockServer()
        {
            var game = new FakeGame();
            var pump = new RequestPump(Make(game, errors: new List<Exception>()));
            using (var server = new TcpBridgeServer(pump, 0, error => Console.Error.WriteLine(error.Message)))
            {
                Console.WriteLine(Protocol.Serialize(new JObject
                {
                    ["schema_version"] = "1",
                    ["host"] = "127.0.0.1",
                    ["port"] = server.Port,
                    ["token"] = server.Token,
                    ["session_id"] = pump.Bridge.SessionId
                }));
                Console.Out.Flush();
                var input = Task.Run(() => Console.ReadLine());
                while (!input.IsCompleted)
                {
                    game.Complete = true;
                    pump.Tick();
                    Thread.Sleep(10);
                }
                pump.Close();
            }
        }

        private static void PlacementChecks()
        {
            Check(ElinGPT.Mod.PlacementText.WallAxis(true, 0) == "x", "Wall variant zero extends along x");
            Check(ElinGPT.Mod.PlacementText.WallAxis(true, 1) == "z", "Wall variant one extends along z");
            foreach (int variant in new[] { -1, 2, 3, 7, 255 })
                Check(ElinGPT.Mod.PlacementText.WallAxis(true, variant) == null, "Other wall shapes are not invented compass headings");
            Check(ElinGPT.Mod.PlacementText.WallAxis(false, 0) == null, "Non-wall variants do not imply a wall axis");
            var state = new JObject
            {
                ["tiles"] = new JArray(new JObject { ["x"] = 1, ["z"] = 2, ["block"] = 5, ["block_variant"] = 0 }),
                ["placement"] = new JObject { ["recipe"] = "wall", ["direction"] = 0 },
                ["entities"] = new JArray(new JObject { ["handle"] = "door", ["direction"] = 0 })
            };
            var baseline = ElinGPT.Mod.UiRevision.Create(state);
            foreach (string path in new[] { "tiles[0].block_variant", "placement.direction", "entities[0].direction" })
            {
                state.SelectToken(path)!.Replace(new JValue(1));
                Check(!JToken.DeepEquals(baseline, ElinGPT.Mod.UiRevision.Create(state)),
                    "Cell variants, held orientation and entity facing are semantic revision data");
                state.SelectToken(path)!.Replace(new JValue(0));
            }
            var game = new FakeGame();
            var bridge = Make(game);
            Check(Result(Send(bridge, "ping"))["capabilities"]!.Values<string>().Contains("rotate_held"),
                "Native held rotation is discoverable in capabilities");
            Result(Send(bridge, "claim", new JObject { ["name"] = "Builder" }));
            var args = Revision(bridge);
            args["kind"] = "rotate_held";
            Result(Send(bridge, "act", args));
            bridge.Tick();
            Check(game.Executions == 1, "Held rotation uses the normal owned action lifecycle");
        }

        public static int Main(string[] args)
        {
            if (args.Length == 1 && args[0] == "--mock-server") { MockServer(); return 0; }
            try
            {
                if (args.Length == 1 && args[0] == "--placement")
                {
                    PlacementChecks();
                    Console.WriteLine($"Passed {checks} placement checks.");
                    return 0;
                }
                ControllerChecks(); DeathChecks(); FailureChecks(); PumpChecks(); SessionChecks(); GeometryChecks(); DeferredQuitChecks(); ProgressBudgetChecks(); PlacementChecks(); TransportChecks(); CredentialChecks();
                Console.WriteLine($"Passed {checks} core/transport checks.");
                return 0;
            }
            catch (Exception error) { Console.Error.WriteLine(error); return 1; }
        }
    }
}
