using System;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using System.Text.RegularExpressions;
using ElinGPT.Core;
using Newtonsoft.Json.Linq;
using TMPro;
using UnityEngine;
using UnityEngine.EventSystems;
using UnityEngine.Events;
using UnityEngine.UI;

namespace ElinGPT.Mod
{
    internal sealed class UiAdapter
    {
        private readonly Dictionary<string, GameObject> objects = new Dictionary<string, GameObject>();
        private static readonly Dictionary<Type, FieldInfo[]> EventFields = new Dictionary<Type, FieldInfo[]>();
        private static readonly Dictionary<Type, PropertyInfo[]> EventProperties = new Dictionary<Type, PropertyInfo[]>();
        private int epoch;
        private PointerEventData? virtualPointer;
        private string? queuedClick;
        private PointerEventData.InputButton clickButton;
        private int clickQueuedFrame, clickActiveFrame;
        private GameObject? pressedObject;
        private PointerEventData? pressedPointer;
        private ButtonState? pressedMouse;
        public UiKeyInput Keys { get; } = new UiKeyInput();
        public Vector3? PointerPosition => virtualPointer == null ? (Vector3?)null : virtualPointer.position;
        private static bool DecisionUiActive()
        {
            if (EClass.ui == null) return false;
            var modalRoot = EClass.ui.transform.Find("Layers");
            var contextRoot = EClass.ui.transform.Find("ContextMenu");
            return HasVisibleChild(modalRoot) || HasVisibleChild(contextRoot);
        }

        private static bool HasVisibleChild(Transform? root)
        {
            if (root == null) return false;
            for (int i = 0; i < root.childCount; i++)
            {
                var child = root.GetChild(i).gameObject;
                if (child.GetComponent<Layer>() != null && Visible(child) ||
                    child.GetComponent<UIContextMenu>() != null && Visible(child)) return true;
            }
            return false;
        }
        public bool HasQueuedInput => Keys.Pending || queuedClick != null || pressedPointer != null;
        public void CancelQueuedInput()
        {
            queuedClick = null;
            Keys.Clear();
            pressedMouse?.Clear();
            pressedMouse = null;
            var go = pressedObject;
            var pointer = pressedPointer;
            pressedObject = null;
            pressedPointer = null;
            virtualPointer = null;
            if (go != null && pointer != null) ExecuteEvents.Execute(go, pointer, ExecuteEvents.pointerUpHandler);
        }
        public void Invalidate() { epoch++; objects.Clear(); CancelQueuedInput(); }

        public void AdvancePointerInput()
        {
            if (pressedPointer != null)
            {
                pressedMouse!.Clear();
                if (Time.frameCount == clickActiveFrame + 1)
                {
                    pressedMouse.up = pressedMouse.clicked = true;
                    if (pressedObject != null)
                    {
                        SetPointerContext(pressedObject, pressedPointer);
                        ExecuteEvents.Execute(pressedObject, pressedPointer, ExecuteEvents.pointerUpHandler);
                        if (pressedObject != null)
                            ExecuteEvents.Execute(pressedObject, pressedPointer, ExecuteEvents.pointerClickHandler);
                    }
                }
                else if (Time.frameCount > clickActiveFrame + 1)
                {
                    pressedMouse = null;
                    pressedPointer = null;
                    pressedObject = null;
                }
                return;
            }
            if (queuedClick == null || Time.frameCount <= clickQueuedFrame || EInput.IsConsumed) return;
            string id = queuedClick;
            queuedClick = null;
            var go = Resolve(id);
            string operation = clickButton == PointerEventData.InputButton.Left ? "click" : "right_click";
            if (!Node(go, null, 0)["actions"]!.Values<string>().Contains(operation))
                throw new BridgeException("ui_action_unavailable", "Queued click target is no longer available.");
            pressedObject = go;
            pressedPointer = Pointer(go, clickButton);
            pressedPointer.pointerPress = pressedPointer.rawPointerPress = go;
            pressedPointer.pointerPressRaycast = pressedPointer.pointerCurrentRaycast;
            clickActiveFrame = Time.frameCount;
            pressedMouse = clickButton == PointerEventData.InputButton.Left ? EInput.leftMouse : EInput.rightMouse;
            pressedMouse.Clear();
            pressedMouse.consumed = pressedMouse.wasConsumed = false;
            pressedMouse.down = pressedMouse.pressing = pressedMouse.usedMouse = true;
            pressedMouse.timeSinceLastButtonDown = 0;
            SetPointerContext(go, pressedPointer);
            ExecuteEvents.Execute(go, pressedPointer, ExecuteEvents.pointerEnterHandler);
            if (go != null) ExecuteEvents.Execute(go, pressedPointer, ExecuteEvents.pointerDownHandler);
        }

        public void ApplyPointerContext()
        {
            if (virtualPointer == null) return;
            virtualPointer.hovered.RemoveAll(go => go == null);
            InputModuleEX.eventData = virtualPointer;
            InputModuleEX.list = virtualPointer.hovered;
            InputModuleEX.topGameObject = virtualPointer.pointerEnter;
            EInput.uiMousePosition = virtualPointer.position;
            if (EClass.ui != null && EClass.ui.canvasScaler != null)
                EInput.uiMousePosition /= EClass.ui.canvasScaler.scaleFactor;
        }

        public static string Clean(string? text, int max = 400)
        {
            string value = Regex.Replace(text ?? "", "<[^>]*>", "").Trim();
            return value.Length > max ? value.Substring(0, max) : value;
        }

        private string Id(GameObject go) => "ui:" + epoch + ":" + go.GetInstanceID();

        private static bool Enabled(Component component) =>
            !(component is Behaviour behaviour) || behaviour.isActiveAndEnabled;

        private static bool Visible(GameObject go)
        {
            if (!go.activeInHierarchy) return false;
            var canvas = go.GetComponentInParent<Canvas>();
            if (canvas == null || !canvas.isActiveAndEnabled) return false;
            foreach (var group in go.GetComponentsInParent<CanvasGroup>())
            {
                if (group.alpha <= 0.01f) return false;
                if (group.ignoreParentGroups) break;
            }
            return true;
        }

        private static bool Sensitive(GameObject go) =>
            go.GetComponentsInParent<InputField>().Any(f => f.contentType == InputField.ContentType.Password ||
                f.contentType == InputField.ContentType.Pin) ||
            go.GetComponentsInParent<TMP_InputField>().Any(f => f.contentType == TMP_InputField.ContentType.Password ||
                f.contentType == TMP_InputField.ContentType.Pin);

        private static List<GameObject> Roots()
        {
            var candidates = UnityEngine.Object.FindObjectsOfType<Canvas>(true)
                .Where(canvas => canvas.gameObject.scene.IsValid())
                .Select(canvas => canvas.rootCanvas.gameObject).Distinct()
                .OrderBy(go => go.GetInstanceID()).ToList();
            var transforms = new HashSet<Transform>(candidates.Select(go => go.transform));
            return candidates.Where(go =>
            {
                for (var parent = go.transform.parent; parent != null; parent = parent.parent)
                    if (transforms.Contains(parent)) return false;
                return true;
            }).ToList();
        }

        private static Rect Bounds(GameObject go)
        {
            var transform = go.transform as RectTransform;
            if (transform == null) return new Rect();
            var corners = new Vector3[4];
            transform.GetWorldCorners(corners);
            var canvas = go.GetComponentInParent<Canvas>();
            var camera = canvas != null && canvas.renderMode != RenderMode.ScreenSpaceOverlay ? canvas.worldCamera : null;
            var points = corners.Select(point => RectTransformUtility.WorldToScreenPoint(camera, point)).ToArray();
            return Rect.MinMaxRect(points.Min(p => p.x), points.Min(p => p.y), points.Max(p => p.x), points.Max(p => p.y));
        }

        private static string PathOf(Transform transform)
        {
            var parts = new List<string>();
            for (var current = transform; current != null; current = current.parent)
                parts.Add(current.name);
            parts.Reverse();
            return string.Join("\\", parts);
        }

        private static string TextOf(GameObject go)
        {
            var text = go.GetComponent<Text>();
            if (text != null) return text.text;
            var tmp = go.GetComponent<TMP_Text>();
            return tmp != null ? tmp.text : "";
        }

        private static string LabelOf(GameObject go)
        {
            string own = Clean(TextOf(go));
            if (own.Length != 0) return own;
            var labels = go.GetComponentsInChildren<Text>(false).Where(t => Visible(t.gameObject) && !Sensitive(t.gameObject))
                .Select(t => Clean(t.text))
                .Concat(go.GetComponentsInChildren<TMP_Text>(false).Where(t => Visible(t.gameObject) && !Sensitive(t.gameObject))
                    .Select(t => Clean(t.text))).Where(t => t.Length != 0).Distinct().Take(3);
            string label = string.Join(" | ", labels);
            return label.Length == 0 ? go.name : Clean(label);
        }

        private static bool Usable(GameObject go)
        {
            var selectable = go.GetComponent<Selectable>();
            return Visible(go) && (selectable == null || selectable.IsInteractable()) &&
                go.GetComponents<Behaviour>().Any(behaviour => behaviour.isActiveAndEnabled);
        }

        private static bool Handles<T>(GameObject go) where T : IEventSystemHandler =>
            go.GetComponents<Component>().Any(component => component is T && Enabled(component));

        private static PointerEventData Pointer(GameObject go, PointerEventData.InputButton button = PointerEventData.InputButton.Left)
        {
            var system = EventSystem.current ??
                throw new BridgeException("ui_not_ready", "No active Unity EventSystem.");
            var data = new PointerEventData(system)
            {
                position = Bounds(go).center,
                button = button,
                pointerId = -1,
                clickCount = 1,
                eligibleForClick = true,
                pressPosition = Bounds(go).center
            };
            var results = new List<RaycastResult>();
            system.RaycastAll(data, results);
            if (results.Count != 0) data.pointerCurrentRaycast = results[0];
            return data;
        }

        private static bool Occluded(GameObject go)
        {
            var rect = Bounds(go);
            if (rect.width <= 0 || rect.height <= 0 || EventSystem.current == null) return false;
            var pointer = Pointer(go);
            var hit = pointer.pointerCurrentRaycast.gameObject;
            return hit != null && hit != go && !hit.transform.IsChildOf(go.transform) && !go.transform.IsChildOf(hit.transform);
        }

        private static IEnumerable<KeyValuePair<string, UnityEventBase>> Events(Component component)
        {
            var type = component.GetType();
            if (!EventFields.TryGetValue(type, out var fields))
            {
                fields = type.GetFields(BindingFlags.Public | BindingFlags.Instance)
                    .Where(field => typeof(UnityEventBase).IsAssignableFrom(field.FieldType)).ToArray();
                EventFields[type] = fields;
                EventProperties[type] = new[] { "onClick", "onValueChanged", "onEndEdit", "onSubmit", "onSelect", "onDeselect" }
                    .Select(name => type.GetProperty(name, BindingFlags.Public | BindingFlags.Instance))
                    .Where(property => property != null && property.GetIndexParameters().Length == 0 &&
                        typeof(UnityEventBase).IsAssignableFrom(property.PropertyType)).Cast<PropertyInfo>().ToArray();
            }
            foreach (var field in fields)
                if (field.GetValue(component) is UnityEventBase value)
                    yield return new KeyValuePair<string, UnityEventBase>(field.Name, value);
            // These public properties are the standard uGUI/TMP event contracts.
            foreach (var property in EventProperties[type])
            {
                if (property.GetValue(component, null) is UnityEventBase value)
                    yield return new KeyValuePair<string, UnityEventBase>(property.Name, value);
            }
        }

        private static bool CanInvoke(UnityEventBase value) => value is UnityEvent || value is UnityEvent<string> ||
            value is UnityEvent<bool> || value is UnityEvent<int> || value is UnityEvent<float>;

        private static JArray EventDescriptions(Component[] components)
        {
            var result = new JArray();
            for (int i = 0; i < components.Length; i++)
            {
                var component = components[i];
                if (component == null) continue;
                foreach (var entry in Events(component).GroupBy(e => e.Key).Select(group => group.First()))
                {
                    var callbacks = Enumerable.Range(0, entry.Value.GetPersistentEventCount())
                        .Select(entry.Value.GetPersistentMethodName).Where(name => name.Length != 0).Take(8);
                    result.Add(new JObject
                    {
                        ["id"] = i + ":" + entry.Key,
                        ["component"] = component.GetType().FullName,
                        ["name"] = entry.Key,
                        ["invokable"] = CanInvoke(entry.Value) && Enabled(component),
                        ["callbacks"] = new JArray(callbacks)
                    });
                }
            }
            return result;
        }

        private JObject Node(GameObject go, string? parent, int depth)
        {
            string id = Id(go);
            if (!objects.ContainsKey(id) && objects.Count >= 8192)
            {
                foreach (var stale in objects.Where(pair => pair.Value == null).Select(pair => pair.Key).ToArray()) objects.Remove(stale);
                if (objects.Count >= 8192) throw new BridgeException("ui_registry_full", "UI handle limit reached; restart the bridge.");
            }
            objects[id] = go;
            var components = go.GetComponents<Component>();
            var selectable = go.GetComponent<Selectable>();
            var input = go.GetComponent<InputField>();
            var tmp = go.GetComponent<TMP_InputField>();
            var dropdown = go.GetComponent<Dropdown>();
            var tmpDropdown = go.GetComponent<TMP_Dropdown>();
            var slider = go.GetComponent<Slider>();
            var toggle = go.GetComponent<Toggle>();
            var scroll = go.GetComponent<ScrollRect>();
            var rect = Bounds(go);
            bool visible = Visible(go);
            bool sensitive = Sensitive(go);
            bool enabled = selectable != null ? selectable.isActiveAndEnabled : components.Any(c => c is Behaviour && Enabled(c));
            bool interactable = selectable == null || selectable.IsInteractable();
            bool occluded = visible && selectable != null && Occluded(go);
            var actions = new JArray();
            if (visible && enabled && interactable && !occluded && !sensitive)
            {
                if (Handles<IPointerClickHandler>(go) || Handles<IPointerDownHandler>(go)) { actions.Add("click"); actions.Add("right_click"); }
                if (Handles<IPointerEnterHandler>(go)) actions.Add("hover");
                if (selectable != null || Handles<ISelectHandler>(go)) actions.Add("focus");
                if (Handles<ISubmitHandler>(go)) actions.Add("submit");
                if (Handles<ICancelHandler>(go)) actions.Add("cancel");
                if (Handles<IScrollHandler>(go)) actions.Add("scroll");
                if (Handles<IDragHandler>(go)) actions.Add("drag");
                if (input != null || tmp != null)
                {
                    if (input != null && !input.readOnly || tmp != null && !tmp.readOnly) actions.Add("set_text");
                    actions.Add("end_edit");
                }
                if (dropdown != null || tmpDropdown != null || slider != null || toggle != null) actions.Add("set_value");
                if (components.Any(c => c != null && Events(c).Any(e => CanInvoke(e.Value)))) actions.Add("invoke_event");
            }
            var role = input != null || tmp != null ? "text_input" : dropdown != null || tmpDropdown != null ? "dropdown" :
                slider != null ? "slider" : toggle != null ? "toggle" : go.GetComponent<Button>() != null ? "button" :
                scroll != null ? "scroll_view" : go.GetComponent<Text>() != null || go.GetComponent<TMP_Text>() != null ? "text" :
                go.GetComponent<Canvas>() != null ? "canvas" : "container";
            var node = new JObject
            {
                ["id"] = id,
                ["parent_id"] = parent,
                ["name"] = go.name,
                ["path"] = PathOf(go.transform),
                ["sibling_index"] = go.transform.GetSiblingIndex(),
                ["depth"] = depth,
                ["child_count"] = go.transform.childCount,
                ["components"] = new JArray(components.Where(c => c != null).Select(c => c.GetType().FullName)),
                ["role"] = role,
                ["active_self"] = go.activeSelf,
                ["active_in_hierarchy"] = go.activeInHierarchy,
                ["enabled"] = enabled,
                ["visible"] = visible,
                ["interactable"] = interactable,
                ["occluded"] = occluded,
                ["focused"] = EventSystem.current?.currentSelectedGameObject == go,
                ["sensitive"] = sensitive,
                ["pointer_hint"] = go.GetComponentInParent<WidgetMouseover>() != null,
                ["actions"] = actions,
                ["bounds"] = new JObject
                {
                    ["x"] = Math.Round(rect.x, 2),
                    ["y"] = Math.Round(rect.y, 2),
                    ["width"] = Math.Round(rect.width, 2),
                    ["height"] = Math.Round(rect.height, 2)
                },
                ["label"] = visible && !sensitive && (selectable != null || role == "text") ? LabelOf(go) : go.name,
                ["text"] = visible && !sensitive ? Clean(TextOf(go), 4096) : null,
                ["events"] = !sensitive ? EventDescriptions(components) : new JArray()
            };
            if (visible && !sensitive)
            {
                if (input != null) { node["value"] = input.text; node["read_only"] = input.readOnly; node["character_limit"] = input.characterLimit; }
                if (tmp != null) { node["value"] = tmp.text; node["read_only"] = tmp.readOnly; node["character_limit"] = tmp.characterLimit; }
                if (toggle != null) node["value"] = toggle.isOn;
                if (slider != null) { node["value"] = slider.value; node["min"] = slider.minValue; node["max"] = slider.maxValue; node["whole_numbers"] = slider.wholeNumbers; }
                if (dropdown != null) { node["value"] = dropdown.value; node["options"] = new JArray(dropdown.options.Select(option => option.text)); }
                if (tmpDropdown != null) { node["value"] = tmpDropdown.value; node["options"] = new JArray(tmpDropdown.options.Select(option => option.text)); }
                if (scroll != null) node["scroll_position"] = new JObject { ["x"] = scroll.normalizedPosition.x, ["y"] = scroll.normalizedPosition.y };
            }
            return node;
        }

        public JObject Read(JObject? args = null)
        {
            int limit = args?["limit"] != null ? (int)Protocol.Integer(args, "limit", 1, 4096) : 1024;
            int maxDepth = args?["depth"] != null ? (int)Protocol.Integer(args, "depth", 0, 32) : 16;
            if (args?["include_inactive"] != null && args["include_inactive"]!.Type != JTokenType.Boolean)
                throw new BridgeException("invalid_argument", "include_inactive must be boolean.");
            bool includeInactive = args?["include_inactive"]?.Value<bool>() == true;
            var roots = args?["root_id"] != null ? new List<GameObject> { Resolve(Protocol.Text(args, "root_id", 128)) } : Roots();
            var queue = new Queue<Tuple<GameObject, string?, int>>();
            foreach (var root in roots) queue.Enqueue(Tuple.Create(root, (string?)null, 0));
            var nodes = new JArray();
            var visited = new HashSet<int>();
            bool depthClipped = false;
            while (queue.Count != 0 && nodes.Count < limit)
            {
                var entry = queue.Dequeue();
                var go = entry.Item1;
                if (go == null || !visited.Add(go.GetInstanceID())) continue;
                nodes.Add(Node(go, entry.Item2, entry.Item3));
                if (!includeInactive && !go.activeInHierarchy) continue;
                if (entry.Item3 >= maxDepth)
                {
                    depthClipped |= go.transform.childCount != 0;
                    continue;
                }
                for (int i = 0; i < go.transform.childCount; i++)
                    queue.Enqueue(Tuple.Create(go.transform.GetChild(i).gameObject, (string?)Id(go), entry.Item3 + 1));
            }
            if (objects.Count > 8192)
                foreach (var key in objects.Where(pair => pair.Value == null).Select(pair => pair.Key).ToArray()) objects.Remove(key);
            var system = EventSystem.current;
            bool active = DecisionUiActive();
            return new JObject
            {
                ["format"] = "gameobject_tree",
                ["active"] = active,
                ["roots"] = new JArray(roots.Select(Id)),
                ["nodes"] = nodes,
                ["truncated"] = queue.Count != 0 || depthClipped,
                ["limits"] = new JObject { ["nodes"] = limit, ["depth"] = maxDepth },
                ["focus_id"] = system?.currentSelectedGameObject != null ? Id(system.currentSelectedGameObject) : null,
                ["input_busy"] = EInput.IsConsumed || HasQueuedInput,
                ["drag_active"] = EClass.ui != null && EClass.ui.IsDragging,
                ["global_actions"] = new JArray("key"),
                ["key_bindings"] = EInput.keys == null ? new JArray() : new JArray(EInput.keys.List()
                    .Select(binding => new JObject
                    {
                        ["key"] = binding.key.ToString().ToLowerInvariant(),
                        ["action"] = binding.action.ToString()
                    })),
                ["keys"] = new JArray(UiKeyInput.Names)
            };
        }

        private GameObject Resolve(string id)
        {
            if (!objects.TryGetValue(id, out var go) || go == null || Id(go) != id || go.GetComponentInParent<Canvas>() == null)
                throw new BridgeException("stale_ui", "UI node no longer belongs to the advertised tree. Observe again.");
            return go;
        }

        private static float Number(JObject args, string key, float min, float max)
        {
            if (args[key]?.Type != JTokenType.Integer && args[key]?.Type != JTokenType.Float)
                throw new BridgeException("invalid_argument", key + " must be numeric.");
            float value = args[key]!.Value<float>();
            if (float.IsNaN(value) || float.IsInfinity(value) || value < min || value > max)
                throw new BridgeException("invalid_argument", key + " is out of range.");
            return value;
        }

        private static void PrepareInput() => InputModuleEX.GetPointerEventData()?.hovered?.RemoveAll(go => go == null);

        public void Act(JObject args)
        {
            if (HasQueuedInput) throw new BridgeException("input_busy", "A UI input operation is still pending.");
            string operation = args["operation"] == null ? "click" : Protocol.Text(args, "operation", 32);
            if (operation == "key")
            {
                string name = Protocol.Text(args, "key", 32);
                var binding = EInput.keys?.List().FirstOrDefault(item =>
                    string.Equals(item.key.ToString(), name, StringComparison.OrdinalIgnoreCase));
                if (binding != null && binding.action == EAction.MouseMiddle && virtualPointer != null)
                {
                    var button = virtualPointer.pointerEnter?.GetComponent<UIButton>();
                    if (button == null || !Usable(button.gameObject) || Occluded(button.gameObject) || !button.CanMiddleClick())
                        throw new BridgeException("ui_action_unavailable", "The hovered control does not support the native middle-click shortcut.");
                    PrepareInput();
                    ApplyPointerContext();
                    button.OnMiddleClick(true);
                    return;
                }
                Keys.Queue(name);
                return;
            }
            string id = Protocol.Text(args, args["node_id"] != null ? "node_id" : "choice_id", 128);
            var go = Resolve(id);
            var node = Node(go, null, 0);
            if (!node["actions"]!.Values<string>().Contains(operation))
                throw new BridgeException("ui_action_unavailable", "This node does not currently advertise " + operation + ".");
            PrepareInput();
            var system = EventSystem.current ?? throw new BridgeException("ui_not_ready", "No active EventSystem.");
            switch (operation)
            {
                case "hover":
                    var hover = Pointer(go);
                    SetPointerContext(go, hover);
                    ExecuteEvents.Execute(go, hover, ExecuteEvents.pointerEnterHandler);
                    break;
                case "click":
                case "right_click":
                    if (EInput.IsConsumed) throw new BridgeException("input_busy", "Native UI input is consumed for this frame.");
                    queuedClick = id;
                    clickButton = operation == "click" ? PointerEventData.InputButton.Left : PointerEventData.InputButton.Right;
                    clickQueuedFrame = Time.frameCount;
                    break;
                case "focus":
                    system.SetSelectedGameObject(go);
                    go.GetComponent<InputField>()?.ActivateInputField();
                    go.GetComponent<TMP_InputField>()?.ActivateInputField();
                    break;
                case "submit": ExecuteEvents.Execute(go, new BaseEventData(system), ExecuteEvents.submitHandler); break;
                case "cancel": ExecuteEvents.Execute(go, new BaseEventData(system), ExecuteEvents.cancelHandler); break;
                case "set_text": SetTextValue(go, args); break;
                case "end_edit":
                    if (go.GetComponent<InputField>() is InputField input) input.DeactivateInputField();
                    else if (go.GetComponent<TMP_InputField>() is TMP_InputField tmp) tmp.DeactivateInputField();
                    break;
                case "set_value": SetValue(go, args); break;
                case "scroll":
                    var wheel = Pointer(go);
                    SetPointerContext(go, wheel);
                    wheel.scrollDelta = new Vector2(Number(args, "dx", -10, 10), Number(args, "dy", -10, 10));
                    ExecuteEvents.Execute(go, wheel, ExecuteEvents.scrollHandler);
                    break;
                case "drag":
                    if (EInput.IsConsumed) throw new BridgeException("input_busy", "Native UI input is consumed for this frame.");
                    var target = Resolve(Protocol.Text(args, "target_id", 128));
                    if (!Visible(target) || Occluded(target)) throw new BridgeException("ui_action_unavailable", "Drop target is not visible/unoccluded.");
                    Drag(go, target);
                    break;
                case "invoke_event": InvokeEvent(go, args); break;
                default: throw new BridgeException("unsupported_ui_operation", "Unknown UI operation.");
            }
        }

        private void Drag(GameObject source, GameObject target)
        {
            var pointer = Pointer(source);
            SetPointerContext(source, pointer);
            pointer.pointerPress = pointer.rawPointerPress = pointer.pointerDrag = source;
            pointer.pointerPressRaycast = pointer.pointerCurrentRaycast;
            bool pressed = false, released = false, began = false;
            try
            {
                ExecuteEvents.Execute(source, pointer, ExecuteEvents.pointerEnterHandler);
                pressed = true;
                ExecuteEvents.Execute(source, pointer, ExecuteEvents.pointerDownHandler);
                if (source == null) throw new BridgeException("stale_ui", "Drag source closed on pointer down.");
                ExecuteEvents.Execute(source, pointer, ExecuteEvents.initializePotentialDrag);
                began = true;
                ExecuteEvents.Execute(source, pointer, ExecuteEvents.beginDragHandler);
                pointer.dragging = true;
                pointer.eligibleForClick = false;
                if (target == null) throw new BridgeException("stale_ui", "Drop target closed during drag.");
                pointer.delta = Bounds(target).center - pointer.position;
                pointer.position = Bounds(target).center;
                pointer.pointerCurrentRaycast = Pointer(target).pointerCurrentRaycast;
                SetPointerContext(target, pointer);
                if (source != null) ExecuteEvents.Execute(source, pointer, ExecuteEvents.dragHandler);
                released = true;
                if (source != null) ExecuteEvents.Execute(source, pointer, ExecuteEvents.pointerUpHandler);
                if (target != null) ExecuteEvents.ExecuteHierarchy(target, pointer, ExecuteEvents.dropHandler);
            }
            finally
            {
                try
                {
                    if (pressed && !released && source != null) ExecuteEvents.Execute(source, pointer, ExecuteEvents.pointerUpHandler);
                    if (began && source != null) ExecuteEvents.Execute(source, pointer, ExecuteEvents.endDragHandler);
                }
                finally
                {
                    pointer.dragging = false;
                    pointer.pointerPress = pointer.rawPointerPress = pointer.pointerDrag = null;
                }
            }
        }

        private void SetPointerContext(GameObject go, PointerEventData pointer)
        {
            pointer.pointerEnter = go;
            pointer.hovered.Clear();
            for (var current = go.transform; current != null; current = current.parent)
                pointer.hovered.Add(current.gameObject);
            virtualPointer = pointer;
            ApplyPointerContext();
            if (EClass.ui != null) EClass.ui.RefreshActiveState();
        }

        private static void SetTextValue(GameObject go, JObject args)
        {
            if (args["value"]?.Type != JTokenType.String) throw new BridgeException("invalid_argument", "value must be text.");
            string text = args["value"]!.Value<string>()!;
            if (text.Length > 4096 || text.IndexOf('\0') >= 0) throw new BridgeException("invalid_argument", "Text exceeds 4096 characters or contains NUL.");
            if (go.GetComponent<InputField>() is InputField field)
            {
                if (field.readOnly) throw new BridgeException("ui_action_unavailable", "Text field is read-only.");
                field.text = text;
            }
            else if (go.GetComponent<TMP_InputField>() is TMP_InputField tmp)
            {
                if (tmp.readOnly) throw new BridgeException("ui_action_unavailable", "Text field is read-only.");
                tmp.text = text;
            }
        }

        private static void SetValue(GameObject go, JObject args)
        {
            if (go.GetComponent<Toggle>() is Toggle toggle)
            {
                if (args["value"]?.Type != JTokenType.Boolean) throw new BridgeException("invalid_argument", "Toggle value must be boolean.");
                toggle.isOn = args["value"]!.Value<bool>();
            }
            else if (go.GetComponent<Slider>() is Slider slider) slider.value = Number(args, "value", slider.minValue, slider.maxValue);
            else if (go.GetComponent<Dropdown>() is Dropdown dropdown)
                dropdown.value = (int)Protocol.Integer(args, "value", 0, dropdown.options.Count - 1);
            else if (go.GetComponent<TMP_Dropdown>() is TMP_Dropdown tmp)
                tmp.value = (int)Protocol.Integer(args, "value", 0, tmp.options.Count - 1);
        }

        private static void InvokeEvent(GameObject go, JObject args)
        {
            string eventId = Protocol.Text(args, "event_id", 128);
            var components = go.GetComponents<Component>();
            for (int i = 0; i < components.Length; i++)
            {
                var component = components[i];
                if (component == null || !Enabled(component)) continue;
                foreach (var entry in Events(component))
                {
                    if (i + ":" + entry.Key != eventId) continue;
                    var value = entry.Value;
                    if (value is UnityEvent plain) plain.Invoke();
                    else if (value is UnityEvent<string> text)
                    {
                        if (args["value"]?.Type != JTokenType.String)
                            throw new BridgeException("invalid_argument", "Event value must be a string.");
                        string argument = args["value"]!.Value<string>()!;
                        if (argument.Length > 4096 || argument.IndexOf('\0') >= 0)
                            throw new BridgeException("invalid_argument", "Event text exceeds the limit or contains NUL.");
                        text.Invoke(argument);
                    }
                    else if (value is UnityEvent<bool> boolean && args["value"]?.Type == JTokenType.Boolean) boolean.Invoke(args["value"]!.Value<bool>());
                    else if (value is UnityEvent<int> integer) integer.Invoke((int)Protocol.Integer(args, "value", int.MinValue, int.MaxValue));
                    else if (value is UnityEvent<float> number) number.Invoke(Number(args, "value", -1000000, 1000000));
                    else throw new BridgeException("unsupported_ui_event", "Event requires an unsupported argument type.");
                    return;
                }
            }
            throw new BridgeException("stale_ui", "UnityEvent no longer exists on this node.");
        }
    }
}
