using System;
using System.Collections.Generic;
using ElinGPT.Core;
using UnityEngine;

namespace ElinGPT.Mod
{
    internal sealed class UiKeyInput
    {
        private static readonly Dictionary<string, KeyCode> Codes = new Dictionary<string, KeyCode>(StringComparer.Ordinal)
        {
            ["enter"] = KeyCode.Return,
            ["escape"] = KeyCode.Escape,
            ["tab"] = KeyCode.Tab,
            ["space"] = KeyCode.Space,
            ["up"] = KeyCode.UpArrow,
            ["down"] = KeyCode.DownArrow,
            ["left"] = KeyCode.LeftArrow,
            ["right"] = KeyCode.RightArrow,
            ["page_up"] = KeyCode.PageUp,
            ["page_down"] = KeyCode.PageDown,
            ["home"] = KeyCode.Home,
            ["end"] = KeyCode.End
        };
        private KeyCode? pending;
        private KeyCode? active;
        private readonly HashSet<ButtonState> mouseAliases = new HashSet<ButtonState>();
        private int queuedFrame, activeFrame = -10;
        static UiKeyInput()
        {
            for (char letter = 'a'; letter <= 'z'; letter++) Codes[letter.ToString()] = (KeyCode)Enum.Parse(typeof(KeyCode), letter.ToString().ToUpperInvariant());
            for (int digit = 0; digit <= 9; digit++) Codes[digit.ToString()] = (KeyCode)((int)KeyCode.Alpha0 + digit);
        }
        public static IEnumerable<string> Names => Codes.Keys;
        public bool Pending => pending.HasValue || (active.HasValue && Time.frameCount <= activeFrame + 2);

        public void Queue(string name)
        {
            if (!Codes.TryGetValue(name, out var key)) throw new BridgeException("unsupported_ui_key", "Key is not in the advertised UI key set.");
            if (Pending) throw new BridgeException("input_busy", "A UI key is already queued.");
            pending = key;
            queuedFrame = Time.frameCount;
        }

        private void Advance()
        {
            if (pending.HasValue && Time.frameCount > queuedFrame && !EInput.IsConsumed)
            {
                active = pending;
                pending = null;
                activeFrame = Time.frameCount;
            }
        }

        public bool Down(KeyCode key) { Advance(); return active == key && Time.frameCount == activeFrame; }
        public bool Held(KeyCode key) => Down(key);
        public bool Up(KeyCode key) { Advance(); return active == key && Time.frameCount == activeFrame + 1; }
        public bool ActiveThisFrame { get { Advance(); return active.HasValue && Time.frameCount == activeFrame; } }
        public void UpdateMouseAliases()
        {
            foreach (var button in new[] { EInput.leftMouse, EInput.rightMouse, EInput.middleMouse })
            {
                if (button?.keymap != null && (Down(button.keymap.key) || Up(button.keymap.key)))
                {
                    button.Update();
                    mouseAliases.Add(button);
                }
                else if (button != null && mouseAliases.Remove(button)) button.Clear();
            }
        }
        public void Clear()
        {
            pending = null;
            active = null;
            activeFrame = -10;
            foreach (var button in mouseAliases) button.Clear();
            mouseAliases.Clear();
        }
    }
}
