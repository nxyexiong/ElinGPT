using System;
using System.Diagnostics;
using System.IO;
using BepInEx;
using BepInEx.Configuration;
using ElinGPT.Core;
using HarmonyLib;
using Newtonsoft.Json.Linq;
using UnityEngine;
using UnityEngine.EventSystems;

namespace ElinGPT.Mod
{
    [BepInPlugin(Id, "ElinGPT", "0.1.0")]
    public sealed class Plugin : BaseUnityPlugin
    {
        public const string Id = "elingpt.agentbridge";
        internal static Plugin? Instance { get; private set; }
        private ConfigEntry<bool> bridgeEnabled = null!;
        private ConfigEntry<int> port = null!;
        private ConfigEntry<int> radius = null!;
        private Harmony? harmony;
        private GameAdapter? adapter;
        private RequestPump? pump;
        private TcpBridgeServer? server;
        private float nextTick;
        private string descriptor = "";
        private string errorText = "";
        private bool? previousBackground;
        private bool quitPending;
        private bool allowQuit;
        private float quitAt;

        private void Awake()
        {
            Instance = this;
            bridgeEnabled = Config.Bind("Bridge", "Enabled", false, "Opt in to the local bridge. Toggle with F8.");
            port = Config.Bind("Bridge", "Port", 0, "Loopback port; 0 selects a free port.");
            radius = Config.Bind("Bridge", "ObservationRadius", 6, "Visible local tile radius, clamped to 2..10.");
            descriptor = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "ElinGPT", "connection.json");
            harmony = new Harmony(Id);
            harmony.PatchAll(typeof(Plugin).Assembly);
            if (bridgeEnabled.Value) Enable();
            Logger.LogInfo("ElinGPT loaded. F8 toggles the bridge; F9 immediately takes over.");
        }

        private void Enable()
        {
            if (server != null) return;
            try
            {
                adapter = new GameAdapter { Radius = Mathf.Clamp(radius.Value, 2, 10) };
                pump = new RequestPump(new GameBridge(adapter, Report));
                server = new TcpBridgeServer(pump, port.Value, Report);
                string directory = Path.GetDirectoryName(descriptor)!;
                CredentialDirectory.CreatePrivate(directory);
                if (File.Exists(descriptor))
                    throw new InvalidOperationException("A connection descriptor already exists. Disable the other ElinGPT instance, or remove the stale descriptor after closing the game.");
                var connection = new JObject
                {
                    ["schema_version"] = Protocol.SchemaVersion,
                    ["host"] = "127.0.0.1",
                    ["port"] = server.Port,
                    ["token"] = server.Token,
                    ["pid"] = Process.GetCurrentProcess().Id,
                    ["session_id"] = pump.Bridge.SessionId
                };
                using (var output = new FileStream(descriptor, FileMode.CreateNew, FileAccess.Write, FileShare.Read))
                using (var writer = new StreamWriter(output)) writer.Write(Protocol.Serialize(connection));
                previousBackground = Application.runInBackground;
                Application.runInBackground = true;
                errorText = "";
                Logger.LogInfo("ElinGPT enabled. Pairing descriptor: " + descriptor);
            }
            catch (Exception error)
            {
                Report(error);
                errorText = error.Message;
                Disable();
                bridgeEnabled.Value = false;
            }
        }

        private void Disable()
        {
            string? token = server?.Token;
            server?.Dispose();
            server = null;
            try { pump?.Close(); }
            catch (Exception error)
            {
                Report(error);
                errorText = "Shutdown action cancellation failed: " + error.Message;
            }
            finally
            {
                pump = null;
                adapter = null;
                if (previousBackground.HasValue)
                {
                    Application.runInBackground = previousBackground.Value;
                    previousBackground = null;
                }
            }
            if (token != null && File.Exists(descriptor))
            {
                try
                {
                    var value = Protocol.Parse(File.ReadAllText(descriptor));
                    if (value["token"]?.Value<string>() == token) File.Delete(descriptor);
                }
                catch (Exception error) { Report(error); errorText = "Could not remove connection descriptor: " + error.Message; }
            }
        }

        private void Report(Exception error) => Logger.LogError(error);

        internal void HumanInput()
        {
            if (pump?.Bridge.HasController == true && (Input.anyKeyDown ||
                Input.GetMouseButtonDown(0) || Input.GetMouseButtonDown(1) || Input.mouseScrollDelta.y != 0))
                pump.Bridge.Release("Human input took over.");
        }

        private void Update()
        {
            if (quitPending && Time.realtimeSinceStartup >= quitAt)
            {
                allowQuit = true;
                quitPending = false;
                EClass.core.Quit();
                return;
            }
            if (Input.GetKeyDown(KeyCode.F8))
            {
                bridgeEnabled.Value = !bridgeEnabled.Value;
                if (bridgeEnabled.Value) Enable(); else Disable();
            }
            if (Input.GetKeyDown(KeyCode.F9)) pump?.Bridge.Release("Emergency human takeover.");
            HumanInput();
        }

        private void LateUpdate()
        {
            if (bridgeEnabled.Value && server == null) Enable();
            else if (!bridgeEnabled.Value && server != null) Disable();
            if (pump == null || adapter == null || Time.realtimeSinceStartup < nextTick) return;
            nextTick = Time.realtimeSinceStartup + 0.05f;
            pump.Tick();
        }

        private void OnGUI()
        {
            if (!bridgeEnabled.Value && errorText.Length == 0) return;
            var message = pump?.Bridge.HasController == true
                ? "ElinGPT | " + pump.Bridge.ControllerName + " | " + pump.Bridge.Summary
                : "ElinGPT | local bridge enabled | F8 off, F9 takeover";
            if (errorText.Length != 0) message = "ElinGPT ERROR | " + errorText;
            GUI.Box(new Rect(12, 12, Math.Min(Screen.width - 24, 760), 46), message);
        }

        private void OnDestroy()
        {
            Disable();
            harmony?.UnpatchSelf();
            Instance = null;
        }

        [HarmonyPatch(typeof(AM_Adv), "get_ShouldPauseGame")]
        private static class DecisionPausePatch
        {
            private static void Postfix(ref bool __result)
            {
                if (Instance?.adapter?.DecisionPause == true) __result = true;
            }
        }

        [HarmonyPatch(typeof(global::Core), nameof(global::Core.Update))]
        private static class HumanInputPatch
        {
            private static void Prefix() => Instance?.HumanInput();
        }

        [HarmonyPatch(typeof(Scene), nameof(Scene.Init))]
        private static class ScenePatch
        {
            private static void Prefix() => Instance?.adapter?.Invalidate();
        }

        [HarmonyPatch(typeof(Game), nameof(Game.OnLoad))]
        private static class LoadPatch
        {
            private static void Prefix() => Instance?.adapter?.Invalidate();
        }

        [HarmonyPatch(typeof(Input), nameof(Input.GetKeyDown), new[] { typeof(KeyCode) })]
        private static class UiKeyDownPatch
        {
            private static bool Prefix(KeyCode key, ref bool __result)
            {
                if (Instance?.adapter?.UiKeys.Down(key) != true) return true;
                __result = true;
                return false;
            }
        }

        [HarmonyPatch(typeof(Input), nameof(Input.GetKey), new[] { typeof(KeyCode) })]
        private static class UiKeyHeldPatch
        {
            private static bool Prefix(KeyCode key, ref bool __result)
            {
                if (Instance?.adapter?.UiKeys.Held(key) != true) return true;
                __result = true;
                return false;
            }
        }

        [HarmonyPatch(typeof(Input), nameof(Input.GetKeyUp), new[] { typeof(KeyCode) })]
        private static class UiKeyUpPatch
        {
            private static bool Prefix(KeyCode key, ref bool __result)
            {
                if (Instance?.adapter?.UiKeys.Up(key) != true) return true;
                __result = true;
                return false;
            }
        }

        [HarmonyPatch(typeof(EInput), nameof(EInput.Update))]
        private static class BackgroundUiInputPatch
        {
            private static int lastFrame = -1;
            private static readonly Func<EAction> NativeAction = AccessTools.MethodDelegate<Func<EAction>>(
                AccessTools.Method(typeof(EInput), "GetAction"));
            private static void Postfix()
            {
                if (Instance?.pump?.Bridge.HasController != true || lastFrame == Time.frameCount) return;
                lastFrame = Time.frameCount;
                if (!Application.isFocused)
                {
                    // The native updater returns before input housekeeping when unfocused.
                    if (EInput.skipFrame > 0) EInput.skipFrame--;
                    EInput.waitReleaseAnyKey = false;
                    EInput.requireConfirmReset = false;
                    var selected = EventSystem.current?.currentSelectedGameObject;
                    EInput.isInputFieldActive = selected != null && selected.activeInHierarchy &&
                        (selected.GetComponent<UnityEngine.UI.InputField>() != null ||
                         selected.GetComponent<TMPro.TMP_InputField>() != null);
                    EInput.action = Instance.adapter?.UiKeys.ActiveThisFrame == true && !EInput.isInputFieldActive
                        ? NativeAction() : EAction.None;
                    if (!EInput.isInputFieldActive) Instance.adapter?.UiKeys.UpdateMouseAliases();
                }
                try { Instance.adapter?.AdvanceUiPointerInput(); }
                catch (Exception error)
                {
                    Instance.Report(error);
                    Instance.pump.Bridge.Release("Queued UI input failed: " + error.Message);
                }
            }
        }

        [HarmonyPatch(typeof(InputModuleEX), nameof(InputModuleEX.UpdateEventData))]
        private static class UiPointerContextPatch
        {
            private static void Postfix()
            {
                if (Instance?.pump?.Bridge.HasController == true) Instance.adapter?.ApplyUiPointerContext();
            }
        }

        [HarmonyPatch(typeof(Input), "get_mousePosition")]
        private static class UiMousePositionPatch
        {
            private static bool Prefix(ref Vector3 __result)
            {
                if (Instance?.pump?.Bridge.HasController != true || Instance.adapter?.UiPointerPosition is not Vector3 position)
                    return true;
                __result = position;
                return false;
            }
        }

        [HarmonyPatch(typeof(global::Core), nameof(global::Core.Quit))]
        private static class QuitPatch
        {
            private static bool Prefix()
            {
                var plugin = Instance;
                plugin?.adapter?.MarkQuitting();
                if (plugin == null || plugin.allowQuit || plugin.pump?.Bridge.HasController != true) return true;
                // Permit one final status response before Unity tears down the transport.
                if (!plugin.quitPending)
                {
                    plugin.quitPending = true;
                    plugin.quitAt = Time.realtimeSinceStartup + 0.75f;
                }
                return false;
            }
        }
    }
}
