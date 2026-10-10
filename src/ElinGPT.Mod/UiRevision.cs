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
                foreach (var node in nodes.OfType<JObject>().ToArray())
                {
                    node.Remove("bounds");
                    // Hover hints track the spectator's mouse, not a gameplay decision.
                    if (node["pointer_hint"]?.Value<bool>() == true) node.Remove();
                }
            return stable;
        }
    }
}
