using System;
using System.IO;
using Newtonsoft.Json.Linq;

namespace ElinGPT.Core
{
    public static class GameplayGuide
    {
        private static readonly JObject Content = Load();

        private static JObject Load()
        {
            using (var stream = typeof(GameplayGuide).Assembly.GetManifestResourceStream("ElinGPT.GameplayGuide.json")
                ?? throw new InvalidOperationException("The embedded ElinGPT gameplay guide is missing."))
            using (var reader = new StreamReader(stream))
                return JObject.Parse(reader.ReadToEnd());
        }

        public static JObject Read() => (JObject)Content.DeepClone();
    }
}
