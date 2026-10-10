using System;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace ElinGPT.Core
{
    public sealed class BridgeException : Exception
    {
        public string Code { get; }
        public BridgeException(string code, string message) : base(message) { Code = code; }
    }

    public static class Protocol
    {
        public const string SchemaVersion = "1";
        public const int MaxRequestBytes = 65536;
        public const int MaxResponseBytes = 8 * 1024 * 1024;

        public static JObject Parse(string text)
        {
            using (var input = new StringReader(text))
            using (var reader = new JsonTextReader(input) { MaxDepth = 32, DateParseHandling = DateParseHandling.None })
            {
                var value = JObject.Load(reader, new JsonLoadSettings { DuplicatePropertyNameHandling = DuplicatePropertyNameHandling.Error });
                if (reader.Read()) throw new BridgeException("invalid_request", "Only one JSON object is allowed.");
                return value;
            }
        }

        public static string Text(JObject obj, string key, int max = 256)
        {
            var value = obj[key];
            if (value?.Type != JTokenType.String)
                throw new BridgeException("invalid_argument", key + " must be a string.");
            string result = value.Value<string>()!;
            if (result.Length == 0 || result.Length > max || result.IndexOf('\0') >= 0)
                throw new BridgeException("invalid_argument", key + " has an invalid length or contains NUL.");
            return result;
        }

        public static long Integer(JObject obj, string key, long min, long max)
        {
            var value = obj[key];
            if (value?.Type != JTokenType.Integer)
                throw new BridgeException("invalid_argument", key + " must be an integer.");
            long number;
            try { number = value.Value<long>(); }
            catch (OverflowException) { throw new BridgeException("invalid_argument", key + " is out of range."); }
            if (number < min || number > max)
                throw new BridgeException("invalid_argument", key + " is out of range.");
            return number;
        }

        public static JObject Object(JObject obj, string key)
        {
            if (obj[key] is JObject value) return value;
            throw new BridgeException("invalid_argument", key + " must be an object.");
        }

        public static JObject Success(string id, JToken value) =>
            new JObject { ["id"] = id, ["result"] = value };

        public static JObject Error(string? id, string code, string message) =>
            new JObject { ["id"] = id, ["error"] = new JObject { ["code"] = code, ["message"] = message } };

        public static string Serialize(JToken value) => value.ToString(Formatting.None);
        public static string Number(long number) => number.ToString(CultureInfo.InvariantCulture);

        public static string Fingerprint(string method, JObject args)
        {
            using (var hash = SHA256.Create())
                return Convert.ToBase64String(hash.ComputeHash(Encoding.UTF8.GetBytes(method + ":" + Serialize(Canonical(args)))));
        }

        private static JToken Canonical(JToken value)
        {
            if (value is JObject obj)
                return new JObject(obj.Properties().OrderBy(p => p.Name, StringComparer.Ordinal)
                    .Select(p => new JProperty(p.Name, Canonical(p.Value))));
            if (value is JArray array) return new JArray(array.Select(Canonical));
            return value.DeepClone();
        }
    }

    public sealed class WorldFrame
    {
        public string Generation { get; set; } = "";
        public string Status { get; set; } = "unavailable";
        public bool Ready { get; set; }
        public bool HasUI { get; set; }
        public int Turn { get; set; }
        public int Hp { get; set; }
        public JObject Data { get; set; } = new JObject();
        public JObject? RevisionData { get; set; }
    }

    public sealed class ActionOutcome
    {
        public string Status { get; }
        public string Message { get; }
        public ActionOutcome(string status, string message) { Status = status; Message = message; }
    }

    public interface IGameAdapter
    {
        bool QuitRequested { get; }
        WorldFrame Read();
        JObject ReadUi(JObject args);
        JObject Inspect(JObject args);
        JArray AvailableActions(JObject args, long revision);
        void Execute(JObject args, long revision);
        void ChooseUI(JObject args);
        JObject SetUiText(JObject args);
        JObject SessionCommand(JObject args);
        ActionOutcome? PollAction(WorldFrame frame);
        void CancelOwnedAction();
        void SetDecisionPause(bool paused);
    }
}
