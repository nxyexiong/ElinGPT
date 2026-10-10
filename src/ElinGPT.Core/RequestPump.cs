using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Threading;
using System.Threading.Tasks;
using Newtonsoft.Json.Linq;

namespace ElinGPT.Core
{
    public sealed class PendingRequest
    {
        private int state;
        public string Client { get; }
        public JObject Request { get; }
        public TaskCompletionSource<JObject> Completion { get; } =
            new TaskCompletionSource<JObject>(TaskCreationOptions.RunContinuationsAsynchronously);
        public PendingRequest(string client, JObject request) { Client = client; Request = request; }
        public bool TryStart() => Interlocked.CompareExchange(ref state, 1, 0) == 0;
        public bool CancelBeforeStart() => Interlocked.CompareExchange(ref state, 2, 0) == 0;
    }

    public sealed class RequestPump
    {
        private readonly ConcurrentQueue<PendingRequest> requests = new ConcurrentQueue<PendingRequest>();
        private readonly ConcurrentQueue<string> disconnects = new ConcurrentQueue<string>();
        private readonly HashSet<string> deadClients = new HashSet<string>();
        private readonly object gate = new object();
        private int count;
        private int closed;
        public GameBridge Bridge { get; }
        public RequestPump(GameBridge bridge) { Bridge = bridge; }

        public PendingRequest Submit(string client, JObject request)
        {
            var pending = new PendingRequest(client, request);
            lock (gate)
            {
                if (closed != 0) throw new BridgeException("disabled", "Bridge is shutting down.");
                if (Interlocked.Increment(ref count) > 64)
                {
                    Interlocked.Decrement(ref count);
                    throw new BridgeException("queue_full", "Main-thread queue is full.");
                }
                requests.Enqueue(pending);
            }
            return pending;
        }

        public void Disconnected(string client) => disconnects.Enqueue(client);

        public void Tick()
        {
            while (disconnects.TryDequeue(out var client))
            {
                deadClients.Add(client);
                Bridge.Disconnect(client);
            }
            Bridge.Tick();
            for (int i = 0; i < 8 && requests.TryDequeue(out var pending); i++)
            {
                Interlocked.Decrement(ref count);
                if (pending.TryStart())
                    pending.Completion.TrySetResult(deadClients.Contains(pending.Client)
                        ? Protocol.Error(pending.Request["id"]?.Value<string>(), "disconnected", "Client disconnected before execution.")
                        : Bridge.Handle(pending.Client, pending.Request));
            }
            if (requests.IsEmpty) deadClients.Clear();
        }

        public void Close()
        {
            lock (gate) closed = 1;
            while (requests.TryDequeue(out var pending))
            {
                Interlocked.Decrement(ref count);
                pending.CancelBeforeStart();
                pending.Completion.TrySetResult(Protocol.Error(pending.Request["id"]?.Value<string>(),
                    "disabled", "Bridge shut down before request execution."));
            }
            Bridge.Release("Bridge disabled.");
        }
    }
}
