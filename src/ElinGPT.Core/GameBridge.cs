using System;
using System.Collections.Generic;
using System.Diagnostics;
using Newtonsoft.Json.Linq;

namespace ElinGPT.Core
{
    // All members are used exclusively by the Unity/main-thread request pump.
    public sealed class GameBridge
    {
        private sealed class Receipt
        {
            public string Fingerprint = "";
            public JObject Response = new JObject();
        }

        private readonly IGameAdapter adapter;
        private readonly Func<DateTime> clock;
        private readonly Action<Exception> report;
        private readonly Dictionary<string, Receipt> receipts = new Dictionary<string, Receipt>();
        private readonly Dictionary<string, JObject> actions = new Dictionary<string, JObject>();
        private WorldFrame frame = new WorldFrame();
        private string? owner;
        private string ownerName = "";
        private DateTime lease;
        private DateTime started;
        private DateTime progressed;
        private long progressTurn;
        private int startHp;
        private string? active;
        private bool sampled;
        private bool activeUi;
        private long revision;
        private string revisionChange = "initial";

        public string SessionId { get; } = Guid.NewGuid().ToString("N");
        public bool HasController => owner != null;
        public string ControllerName => ownerName;
        public string Summary { get; private set; } = "Local bridge ready";
        public int ActionTimeoutSeconds { get; set; } = 120;
        public int ActionIdleTimeoutSeconds { get; set; } = 20;
        public int LeaseSeconds { get; set; } = 30;
        public const int MaxReceipts = 8192;

        public GameBridge(IGameAdapter adapter, Action<Exception> report, Func<DateTime>? clock = null)
        {
            this.adapter = adapter;
            this.report = report;
            var elapsed = Stopwatch.StartNew();
            var origin = DateTime.UtcNow;
            this.clock = clock ?? (() => origin + elapsed.Elapsed);
        }

        public void Tick()
        {
            try
            {
                Refresh();
                if (owner != null && clock() > lease) Release("Controller heartbeat expired.");
                if (owner != null && !SupportsUi(frame.Status) &&
                    !(activeUi && frame.Status == "loading"))
                    Release("Game left the controllable state; control released.");
                if (active != null)
                {
                    if (frame.Turn != progressTurn)
                    {
                        progressTurn = frame.Turn;
                        progressed = clock();
                    }
                    if (adapter.QuitRequested)
                        Finish("completed", "Game accepted the native quit request.", false);
                    else if (clock() - started > TimeSpan.FromSeconds(ActionTimeoutSeconds))
                        Finish("interrupted", "Action time budget exceeded; mod-owned activity cancelled.", true);
                    else if (clock() - progressed > TimeSpan.FromSeconds(ActionIdleTimeoutSeconds))
                        Finish("interrupted", "Action made no turn progress within the idle budget; mod-owned activity cancelled.", true);
                    else if (!SupportsUi(frame.Status) && !(activeUi && frame.Status == "loading"))
                        Finish("interrupted", "Game is no longer in a playable state.", true);
                    else if (frame.Status == "playing" && frame.Hp < startHp)
                        Finish("interrupted", "Player took damage; action stopped at the safety boundary.", true);
                    else if (frame.Status == "loading")
                        SetStatus("running", "Waiting for the native load/revival transition.");
                    else if (frame.HasUI)
                    {
                        var outcome = activeUi ? adapter.PollAction(frame) : null;
                        if (outcome != null) Finish(outcome.Status, outcome.Message, false);
                        else SetStatus("awaiting_ui", "A menu or confirmation requires a choice.");
                    }
                    else
                    {
                        var outcome = adapter.PollAction(frame);
                        if (outcome != null) Finish(outcome.Status, outcome.Message, false);
                        else SetStatus("running", "Waiting for the normal game action to settle.");
                    }
                }
                adapter.SetDecisionPause(owner != null && frame.Status != "loading" &&
                    (active == null && frame.Status == "playing" && frame.Ready || frame.HasUI));
            }
            catch (Exception error)
            {
                report(error);
                Release("Game adapter error: " + error.Message);
            }
        }

        private void Refresh()
        {
            var next = adapter.Read();
            bool changed = !sampled || next.Generation != frame.Generation || next.Status != frame.Status ||
                next.Ready != frame.Ready || next.HasUI != frame.HasUI ||
                !JToken.DeepEquals(next.RevisionData ?? next.Data, frame.RevisionData ?? frame.Data);
            if (sampled && frame.Status == "playing" && next.Status == "dead" && active != null)
                Finish("interrupted", "Player died; use the native death UI to continue.", true);
            else if (sampled && next.Generation != frame.Generation && active != null)
            {
                if (activeUi && (SupportsUi(next.Status) || next.Status == "loading"))
                    startHp = next.Hp;
                else Finish("interrupted", "Load/zone transition invalidated the action.", true);
            }
            if (changed)
                revisionChange = next.Generation != frame.Generation ? "generation" :
                    next.Status != frame.Status ? "status" : next.Ready != frame.Ready ? "ready" :
                    next.HasUI != frame.HasUI ? "ui.active" :
                    FirstChange(frame.RevisionData ?? frame.Data, next.RevisionData ?? next.Data, "state");
            frame = next;
            sampled = true;
            if (changed) revision++;
        }

        private static string FirstChange(JToken? before, JToken? after, string path)
        {
            if (before is JObject left && after is JObject right)
            {
                foreach (var property in right.Properties())
                    if (!JToken.DeepEquals(left[property.Name], property.Value))
                        return FirstChange(left[property.Name], property.Value, path + "." + property.Name);
                foreach (var property in left.Properties())
                    if (right.Property(property.Name) == null) return path + "." + property.Name;
            }
            else if (before is JArray oldItems && after is JArray newItems)
            {
                if (oldItems.Count != newItems.Count) return path + ".length";
                for (int i = 0; i < oldItems.Count; i++)
                    if (!JToken.DeepEquals(oldItems[i], newItems[i]))
                        return FirstChange(oldItems[i], newItems[i], path + "[" + i + "]");
            }
            return path;
        }

        public JObject Handle(string client, JObject request)
        {
            string? id = null;
            try
            {
                id = Protocol.Text(request, "id", 128);
                string method = Protocol.Text(request, "method", 64);
                var args = Protocol.Object(request, "params");
                bool mutation = method == "claim" || method == "release" || method == "act" ||
                    method == "ui_act" || method == "ui_choose" || method == "ui_set_text" || method == "session_command" || method == "cancel";
                string key = client + ":" + id;
                string fingerprint = Protocol.Fingerprint(method, args);
                if (mutation && receipts.TryGetValue(key, out var prior))
                {
                    if (prior.Fingerprint != fingerprint)
                        throw new BridgeException("request_id_conflict", "A request ID cannot be reused with different arguments.");
                    return (JObject)prior.Response.DeepClone();
                }
                if (mutation && receipts.Count >= MaxReceipts)
                    throw new BridgeException("request_limit", "Mutation receipt budget exhausted; restart the bridge before further play.");
                Refresh();
                if (owner == client) lease = clock().AddSeconds(LeaseSeconds);
                JObject response;
                try { response = Protocol.Success(id, Dispatch(client, method, args)); }
                catch (BridgeException error) { response = Protocol.Error(id, error.Code, error.Message); }
                catch (Exception error)
                {
                    report(error);
                    if (owner == client) Release("Game adapter failed during request execution.");
                    response = Protocol.Error(id, "internal_error", "Game adapter failed; control released. Consult the BepInEx log.");
                }
                if (mutation) receipts.Add(key, new Receipt { Fingerprint = fingerprint, Response = (JObject)response.DeepClone() });
                return response;
            }
            catch (BridgeException error) { return Protocol.Error(id, error.Code, error.Message); }
            catch (Exception error)
            {
                report(error);
                if (owner == client) Release("Game adapter failed during a request.");
                return Protocol.Error(id, "internal_error", "Game adapter failed; control released. Consult the BepInEx log.");
            }
        }

        private JToken Dispatch(string client, string method, JObject args)
        {
            switch (method)
            {
                case "ping":
                    return new JObject { ["schema_version"] = Protocol.SchemaVersion, ["session_id"] = SessionId,
                        ["gameplay_guide"] = GameplayGuide.Read(),
                        ["capabilities"] = new JArray("observe", "inspect", "available_actions", "step", "wait", "context", "rotate_held", "cell_variants",
                            "open_ui", "ui_tree", "ui_act", "ui_choose", "ui_set_text", "session_command", "title_navigation", "death_navigation",
                            "control_lease", "request_deduplication") };
                case "observe": return Snapshot();
                case "heartbeat":
                    return new JObject { ["owns_control"] = owner == client, ["lease_seconds"] = LeaseSeconds };
                case "claim":
                    if (owner != null && owner != client) throw new BridgeException("control_busy", "Another client controls the game.");
                    if (!SupportsUi(frame.Status) || frame.Status == "dead" && !frame.HasUI || (!frame.Ready && !frame.HasUI))
                        throw new BridgeException("not_ready", "Claim control at a game, title-menu or death-dialog decision boundary.");
                    ownerName = Protocol.Text(args, "name", 60);
                    owner = client;
                    lease = clock().AddSeconds(LeaseSeconds);
                    adapter.SetDecisionPause(true);
                    Summary = ownerName + " connected";
                    return ControlStatus();
                case "release":
                    RequireOwner(client);
                    Release("Controller released control.");
                    return ControlStatus();
                case "inspect": return adapter.Inspect(args);
                case "available_actions":
                    RequireRevision(args);
                    if (frame.Status != "playing")
                        throw new BridgeException("not_ready", "World action discovery requires a living player in a loaded game.");
                    return new JObject { ["revision"] = revision, ["actions"] = adapter.AvailableActions(args, revision) };
                case "ui_observe": return new JObject { ["revision"] = revision, ["ui"] = adapter.ReadUi(args) };
                case "action_status":
                    string actionId = Protocol.Text(args, "action_id", 128);
                    if (!actions.TryGetValue(actionId, out var record))
                        throw new BridgeException("unknown_action", "This bridge session has no such action.");
                    return new JObject { ["action"] = record.DeepClone(), ["observation"] = Snapshot(),
                        ["quitting"] = adapter.QuitRequested };
                case "act":
                    RequireOwner(client);
                    RequireRevision(args);
                    if (active != null) throw new BridgeException("action_busy", "Complete or cancel the current action first.");
                    if (frame.Status != "playing" || !frame.Ready || frame.HasUI)
                        throw new BridgeException("not_ready", "The game is not ready for a world action.");
                    return Start(args, false);
                case "ui_act":
                case "ui_choose":
                    RequireOwner(client);
                    RequireRevision(args);
                    RequireUiState();
                    if (active != null && !activeUi && !frame.HasUI)
                        throw new BridgeException("action_busy", "A world action is still running.");
                    return Start(args, true);
                case "ui_set_text":
                    RequireOwner(client);
                    RequireRevision(args);
                    RequireUiState();
                    var edited = adapter.SetUiText(args);
                    Refresh();
                    edited["revision"] = revision;
                    return edited;
                case "session_command":
                    RequireOwner(client);
                    RequireRevision(args);
                    if (frame.Status == "dead")
                        throw new BridgeException("not_ready", "Complete the native death/revival UI before issuing session commands.");
                    if (active != null) throw new BridgeException("action_busy", "Cancel/complete the active action before a session command.");
                    var sessionResult = adapter.SessionCommand(args);
                    Summary = "Session command: " + Protocol.Text(args, "command", 32);
                    return sessionResult;
                case "cancel":
                    RequireOwner(client);
                    if (active != null) Finish("interrupted", "Cancelled by controller.", true);
                    return ControlStatus();
                default: throw new BridgeException("unknown_method", "Unknown bridge method: " + method);
            }
        }

        private JObject Start(JObject args, bool ui)
        {
            if (actions.Count >= MaxReceipts) throw new BridgeException("action_limit", "Action history budget exhausted.");
            string actionId = Guid.NewGuid().ToString("N");
            if (ui && active != null) actionId = active;
            else
                actions.Add(actionId, new JObject { ["action_id"] = actionId, ["status"] = "accepted",
                    ["start_revision"] = revision, ["start_turn"] = frame.Turn });
            string? previousActive = active;
            active = actionId;
            activeUi = ui;
            try
            {
                if (ui) adapter.ChooseUI(args);
                else adapter.Execute(args, revision);
            }
            catch (BridgeException error)
            {
                if (previousActive == actionId) active = previousActive;
                else
                {
                    actions[actionId]["status"] = "rejected";
                    actions[actionId]["message"] = error.Message;
                    active = previousActive;
                }
                throw;
            }
            started = clock();
            progressed = started;
            progressTurn = frame.Turn;
            startHp = frame.Hp;
            if (adapter.QuitRequested)
            {
                SetStatus("completed", "Game accepted the native quit request.");
                var quitResult = new JObject { ["action"] = actions[actionId].DeepClone(), ["quitting"] = true };
                active = null;
                return quitResult;
            }
            string description = args["reason"]?.Type == JTokenType.String ? args["reason"]!.Value<string>()! : "";
            if (description.Length > 180) description = description.Substring(0, 180);
            Summary = ui ? "Choosing a menu control" : Protocol.Text(args, "kind", 32) + " " + description;
            SetStatus("running", Summary);
            adapter.SetDecisionPause(false);
            return new JObject { ["action"] = actions[actionId].DeepClone() };
        }

        private void RequireOwner(string client)
        {
            if (owner != client) throw new BridgeException("not_controller", "Claim control before changing the game.");
        }

        private static bool SupportsUi(string status) =>
            status == "playing" || status == "title" || status == "dead";

        private void RequireUiState()
        {
            if (!SupportsUi(frame.Status) || frame.Status == "dead" && !frame.HasUI)
                throw new BridgeException("not_ready", "Wait for a game, title-menu or death-dialog UI decision boundary.");
        }

        private void RequireRevision(JObject args)
        {
            if (Protocol.Text(args, "session_id", 128) != SessionId)
                throw new BridgeException("stale_observation", "Observe again; the game session changed.");
            long expected = Protocol.Integer(args, "expected_revision", 0, long.MaxValue);
            if (expected != revision)
                throw new BridgeException("stale_observation", "Observe again; revision " + expected +
                    " became " + revision + " (" + revisionChange + ").");
        }

        private void SetStatus(string status, string message)
        {
            if (active == null) return;
            actions[active]["status"] = status;
            actions[active]["message"] = message;
            actions[active]["revision"] = revision;
            actions[active]["turn"] = frame.Turn;
        }

        private void Finish(string status, string message, bool cancel)
        {
            if (cancel) adapter.CancelOwnedAction();
            SetStatus(status, message);
            Summary = message;
            active = null;
            activeUi = false;
        }

        public void Disconnect(string client)
        {
            if (owner == client) Release("Controller disconnected.");
        }

        public void Release(string message)
        {
            try
            {
                if (active != null) Finish("interrupted", message, true);
                else adapter.CancelOwnedAction();
            }
            finally
            {
                owner = null;
                ownerName = "";
                active = null;
                activeUi = false;
                Summary = message;
                adapter.SetDecisionPause(false);
            }
        }

        private JObject Snapshot() => new JObject
        {
            ["schema_version"] = Protocol.SchemaVersion, ["session_id"] = SessionId, ["revision"] = revision,
            ["generation"] = frame.Generation, ["status"] = frame.Status, ["ready"] = frame.Ready,
            ["controller"] = owner == null ? null : ownerName, ["active_action"] = active,
            ["state"] = frame.Data.DeepClone()
        };

        private JObject ControlStatus() => new JObject
        {
            ["session_id"] = SessionId, ["revision"] = revision, ["status"] = frame.Status,
            ["ready"] = frame.Ready, ["controller"] = owner == null ? null : ownerName, ["active_action"] = active
        };
    }
}
