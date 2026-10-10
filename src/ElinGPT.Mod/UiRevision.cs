using System;
using System.Linq;
using Newtonsoft.Json.Linq;

namespace ElinGPT.Mod
{
    internal static class UiRevision
    {
        public static JObject Create(JObject state)
        {
            var stable = (JObject)state.DeepClone();
            if (stable["ui"]?["nodes"] is JArray nodes)
            {
                foreach (var node in nodes.OfType<JObject>().ToArray())
                {
                    node.Remove("bounds");
                    node.Remove("child_count");
                    node.Remove("sibling_index");
                    // Fading log widgets duplicate state.messages; their lifecycle is not a new decision.
                    if (node["pointer_hint"]?.Value<bool>() == true ||
                        node["message_display"]?.Value<bool>() == true ||
                        node["visible"]?.Value<bool>() == false ||
                        node["role"]?.Value<string>() == "container" &&
                        !(node["actions"] is JArray actions && actions.Any()) &&
                        string.IsNullOrEmpty(node["text"]?.Value<string>()))
                        node.Remove();
                }
                stable["ui"]!["nodes"] = new JArray(nodes.OfType<JObject>().OrderBy(n => n["id"]?.Value<string>()));
            }
            return stable;
        }
    }
}
