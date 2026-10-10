using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace ElinGPT.Core
{
    public sealed class TcpBridgeServer : IDisposable
    {
        private readonly TcpListener listener;
        private readonly RequestPump pump;
        private readonly Action<Exception> report;
        private readonly HashSet<TcpClient> clients = new HashSet<TcpClient>();
        private readonly object sync = new object();
        private readonly byte[] secret;
        private volatile bool stopped;
        public int Port => ((IPEndPoint)listener.LocalEndpoint).Port;
        public string Token { get; }

        public TcpBridgeServer(RequestPump pump, int port, Action<Exception> report)
        {
            this.pump = pump;
            this.report = report;
            secret = new byte[32];
            using (var rng = RandomNumberGenerator.Create()) rng.GetBytes(secret);
            Token = Convert.ToBase64String(secret);
            listener = new TcpListener(IPAddress.Loopback, port);
            listener.Start(8);
            new Thread(Accept) { IsBackground = true, Name = "ElinGPT accept" }.Start();
        }

        private void Accept()
        {
            try
            {
                while (!stopped)
                {
                    var client = listener.AcceptTcpClient();
                    lock (sync)
                    {
                        if (stopped || clients.Count >= 8) { client.Close(); continue; }
                        clients.Add(client);
                    }
                    new Thread(() => Serve(client)) { IsBackground = true, Name = "ElinGPT client" }.Start();
                }
            }
            catch (SocketException error) { if (!stopped) report(error); }
            catch (ObjectDisposedException error) { if (!stopped) report(error); }
        }

        private bool Authenticate(JToken? token)
        {
            if (token?.Type != JTokenType.String) return false;
            byte[] supplied;
            try { supplied = Convert.FromBase64String(token.Value<string>()!); }
            catch (FormatException) { return false; }
            if (supplied.Length != secret.Length) return false;
            int difference = 0;
            for (int i = 0; i < secret.Length; i++) difference |= supplied[i] ^ secret[i];
            return difference == 0;
        }

        private static string? ReadLine(Stream stream)
        {
            using (var data = new MemoryStream())
            {
                int value;
                while ((value = stream.ReadByte()) != -1)
                {
                    if (value == '\n') return new UTF8Encoding(false, true).GetString(data.ToArray());
                    if (data.Length >= Protocol.MaxRequestBytes)
                        throw new BridgeException("request_too_large", "Request exceeds 64 KiB.");
                    data.WriteByte((byte)value);
                }
                if (data.Length != 0) throw new BridgeException("invalid_request", "Connection ended in a partial JSON frame.");
                return null;
            }
        }

        private static void Write(Stream stream, JObject response)
        {
            byte[] bytes = Encoding.UTF8.GetBytes(Protocol.Serialize(response) + "\n");
            if (bytes.Length > Protocol.MaxResponseBytes)
                bytes = Encoding.UTF8.GetBytes(Protocol.Serialize(Protocol.Error(response["id"]?.Value<string>(),
                    "response_too_large", "Response exceeds the 8 MiB limit.")) + "\n");
            stream.Write(bytes, 0, bytes.Length);
        }

        private void Serve(TcpClient client)
        {
            string clientId = Guid.NewGuid().ToString("N");
            PendingRequest? pending = null;
            bool usedPump = false;
            try
            {
                client.NoDelay = true;
                client.ReceiveTimeout = 45000;
                client.SendTimeout = 10000;
                using (client)
                using (var stream = client.GetStream())
                {
                    string? line;
                    while (!stopped && (line = ReadLine(stream)) != null)
                    {
                        JObject request;
                        try { request = Protocol.Parse(line); }
                        catch (JsonException) { Write(stream, Protocol.Error(null, "invalid_json", "Invalid JSON object.")); continue; }
                        if (!Authenticate(request["token"]))
                        {
                            Write(stream, Protocol.Error(null, "unauthorized", "Invalid local pairing token."));
                            break;
                        }
                        request.Remove("token");
                        try
                        {
                            pending = pump.Submit(clientId, request);
                            usedPump = true;
                            if (pending.Completion.Task.Wait(10000))
                                Write(stream, pending.Completion.Task.Result);
                            else
                            {
                                bool cancelled = pending.CancelBeforeStart();
                                Write(stream, Protocol.Error(request["id"]?.Value<string>(),
                                    cancelled ? "main_thread_timeout" : "outcome_unknown",
                                    cancelled ? "Game did not process the request; it was cancelled before execution."
                                        : "Request execution started; do not retry blindly. Observe and query action_status."));
                            }
                            pending = null;
                        }
                        catch (BridgeException error) { Write(stream, Protocol.Error(request["id"]?.Value<string>(), error.Code, error.Message)); }
                    }
                }
            }
            catch (IOException error) { if (!stopped) report(error); }
            catch (SocketException error) { if (!stopped) report(error); }
            catch (ObjectDisposedException error) { if (!stopped) report(error); }
            catch (BridgeException error) { if (!stopped) report(error); }
            catch (DecoderFallbackException error) { if (!stopped) report(error); }
            finally
            {
                pending?.CancelBeforeStart();
                if (usedPump) pump.Disconnected(clientId);
                lock (sync) clients.Remove(client);
                client.Close();
            }
        }

        public void Dispose()
        {
            stopped = true;
            listener.Stop();
            lock (sync)
                foreach (var client in clients) client.Close();
        }
    }
}
