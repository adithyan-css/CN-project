# TRD — PathSense
**Technical Requirements Document**

| | |
|---|---|
| Document | Technical Requirements Document (companion to PRD.md) |
| Reference implementation | Built and test-verified in this session — Python 3.11, standard library only. Every code block and every "verified" claim in this document corresponds to a test that was actually executed; none of it is speculative. |
| Scope | MVP (Levels 0–6 in BUILD_GUIDE.md). Phase 2/Advanced items are marked NOT IMPLEMENTED where relevant. |

---

## 1. System overview

```
                 ┌───────────────────────────────────────────┐
                 │                  Node process               │
                 │  (one runs on each of the 3 laptops)         │
                 │                                             │
 UDP multicast   │  Discovery ──┐                              │
 (peer HELLO) ◄──┤              ▼                              │
                 │        MetricsCollector ──► Decision Engine  │
 UDP probes +    │              ▲                    │          │
 TCP burst    ◄──┤  MeasurementServer                 ▼          │
 (latency/       │                              AdaptationLoop   │
 loss/throughput)│                                    │          │
                 │  TransferReceiver ◄── TCP ──  TransferSender   │
                 │        ▲                                    │
                 │        │                                    │
                 │  HTTP dashboard (stdlib http.server)          │
                 │  GET /api/state   POST /api/send             │
                 └───────────────────────────────────────────┘
```

Every laptop runs the identical `Node` process (`pathsense/node.py`). There is no distinguished "server" machine in the MVP — each node is simultaneously a possible sender and a possible receiver. This is a deliberate simplification: it avoids a whole class of distributed-systems complexity (leader election, a single point of failure) that a college project does not need to take on, and it matches how AirDrop/Quick Share actually behave (any device can initiate a share to any other).

## 2. Technology stack & rationale

| Choice | Rationale |
|---|---|
| Python 3 standard library only (`socket`, `threading`, `http.server`, `json`, `hashlib`, `struct`) | Zero install step on any demo laptop; nothing to go wrong from a missing/incompatible package at demo time; every network primitive is something you wrote and can explain line-by-line to a judge, per the PRD §9 explainability requirement. |
| UDP multicast for discovery (not broadcast) | Verified in a live sandbox test to loop back correctly on `127.0.0.1` for local development, and behaves identically to broadcast-based discovery on a real LAN subnet where multicast is allowed — the same general mechanism family as mDNS/Bonjour, just without the DNS-style naming layer. **Known limitation:** some networks (enterprise/guest Wi-Fi with client isolation) block multicast; see PRD §11 assumption A1. |
| Stop-and-wait chunked TCP transfer (not a sliding window) | Slower at saturating a link than a windowed/pipelined protocol, but it means "how many chunks has the current peer acknowledged" is always known exactly, with no in-flight ambiguity — this is what makes pause/resume/reroute simple and *correct* rather than merely fast. Documented, deliberate trade-off. |
| stdlib `http.server` for the dashboard (not Flask/FastAPI) | No extra dependency to install on 3 different laptops right before a demo. |
| No TLS on the transfer socket in the MVP | **Explicit limitation, not an oversight.** The MVP transfer channel is plaintext TCP on the local network. TLS-wrapping is scoped to Phase 2 (see PRD §10) — do not claim the MVP is encrypted in your report. |

## 3. Module map

| File | Responsibility | Depends on |
|---|---|---|
| `pathsense/protocol.py` | Shared constants (ports, timeouts, chunk size) and the length-prefixed JSON framing helpers used by every TCP-based module. | — |
| `pathsense/discovery.py` | `Discovery` — multicast HELLO announce/listen, live `PeerInfo` table with staleness expiry. | protocol |
| `pathsense/measurement.py` | `MeasurementServer` (answers probes/throughput tests), `probe_latency`, `measure_throughput`, `MetricsCollector` (continuous per-peer rolling metrics history). | protocol |
| `pathsense/decision.py` | `score_peers`, `explain`, `TransferProfile` — pure functions, no I/O. | measurement (for the `PeerMetricsSample` type only) |
| `pathsense/transfer.py` | `TransferReceiver`, `TransferSender`, `TransferState` — chunked, checksummed, resumable file transfer. | protocol |
| `pathsense/adaptation.py` | `HysteresisController` (pure state machine), `AdaptationLoop` (thread tying collector + decision + sender together). | decision, measurement, transfer |
| `pathsense/dashboard.py` | stdlib HTTP server: `GET /api/state`, `POST /api/send`, and the polling HTML/JS page. | — |
| `pathsense/node.py` | Wires every module above into one runnable peer process + CLI entry point. | all of the above |

## 4. Data models

```python
# discovery.py
PeerInfo(device_id: str, name: str, ip: str, measurement_port: int,
         transfer_port: int, last_seen: float)

# measurement.py
PeerMetricsSample(peer_id: str, latency_ms: float|None, jitter_ms: float|None,
                   loss_pct: float, throughput_mbps: float|None, measured_at: float)

# decision.py
ScoredPeer(peer_id: str, score: float, excluded: bool, exclude_reason: str|None,
           terms: dict[str, float], raw_metrics: PeerMetricsSample)

# transfer.py
TransferState(file_id: str, file_path: str, file_size: int, chunk_size: int,
              total_chunks: int, acked_chunks: set[int], peer_id: str,
              status: str,  # IDLE|TRANSFERRING|PAUSED|DEGRADED|COMPLETE|FAILED
              started_at: float)
```

## 5. Network ports & protocols

| Purpose | Port (default base) | Transport | Notes |
|---|---|---|---|
| Discovery | `50900` | UDP multicast, group `239.255.10.10` | Every node sends + listens on the same group/port |
| Measurement (per node) | `50910 + offset` | UDP (probes) **and** TCP (throughput burst) on the *same* port number | UDP and TCP namespaces don't collide, so one port number serves both |
| Transfer (per node) | `50920 + offset` | TCP | One `TransferReceiver` listener per node, accepts concurrent sessions |
| Dashboard (per node) | `8800 + offset` | HTTP | `GET /`, `GET /api/state`, `POST /api/send` |

Each of the 3 demo laptops runs with a *different* `offset` (or you simply use the same base ports since each laptop is a separate machine with its own localhost — offsets matter only if you run multiple nodes on one machine, e.g. for local testing/rehearsal, exactly as the automated tests in BUILD_GUIDE.md do).

## 6. Wire protocol specification

### 6.1 Discovery (UDP multicast datagram, one-shot, no reply)
```json
{"type": "HELLO", "device_id": "a1b2c3d4e5f6", "name": "Laptop A",
 "measurement_port": 50911, "transfer_port": 50921}
```

### 6.2 Measurement — latency/jitter/loss (UDP, request/reply)
```json
// request:  {"type": "PROBE", "seq": 3, "ts": 1737000000123}
// reply:    {"type": "PROBE_ACK", "seq": 3, "orig_ts": 1737000000123, "echo_ts": 1737000000141}
```
Latency = `now() - orig_ts` at the moment the reply is received by the sender. Loss = fraction of `count` probes that never got an ACK within `PROBE_TIMEOUT_SEC` (default 0.5s). Jitter = population standard deviation of the RTT samples collected in one probing round (default 8 probes/round).

### 6.3 Measurement — throughput (TCP)
```json
// client -> server header: {"type": "THROUGHPUT_TEST", "bytes": 2000000}
// client then sends exactly `bytes` raw bytes
// server -> client reply:  {"type": "THROUGHPUT_RESULT", "duration_sec": 0.183}
```
Throughput (Mbps) = `bytes * 8 / duration_sec / 1_000_000`, using the **receiver's** measured duration (excludes the sender's own socket-buffering time, which would otherwise make the number optimistic).

### 6.4 Transfer (TCP, length-prefixed JSON headers; a `CHUNK` header is immediately followed by its raw binary payload, exactly `length` bytes)
```json
OFFER      {"type":"OFFER","file_id":"...","name":"demo.bin","size":1500000,
            "chunk_size":65536,"total_chunks":23,"checksum_algo":"sha256"}
ACCEPT     {"type":"ACCEPT","file_id":"..."}
REJECT     {"type":"REJECT","file_id":"...","reason":"declined"}
CHUNK      {"type":"CHUNK","chunk_index":4,"length":65536,"checksum":"<sha256 hex>"}  + raw bytes
CHUNK_ACK  {"type":"CHUNK_ACK","chunk_index":4,"ok":true}
COMPLETE   {"type":"COMPLETE"}
```
Transfer is **stop-and-wait**: the sender does not send chunk *N+1* until it has received the `CHUNK_ACK` for chunk *N*. This is what makes `TransferState.acked_chunks` an exact, race-free record of progress at every instant — the property both RESUME and REROUTE depend on.

### 6.5 Dashboard control API (HTTP, JSON)
```
GET  /api/state
  -> {"device_id","name","peers":[PeerInfo...], "metrics":{peer_id:PeerMetricsSample},
      "scores":{peer_id:ScoredPeer}, "transfer": TransferState|null, "events":[str...]}

POST /api/send   body: {"peer_id": "...", "file_path": "/abs/path/to/file"}
  -> {"status":"started","file_id":"..."} | {"error":"..."}
```

## 7. Decision engine algorithm (exact, as implemented)

Given the latest metrics of every currently-measured peer:

1. **Gate.** Drop any peer whose `loss_pct > 15.0` or `throughput_mbps < 0.5` entirely (`excluded=True`, with a stated reason) — these are correctness cutoffs, not soft penalties.
2. **Normalize** each remaining candidate's throughput, latency, and loss to `[0,1]` using **min-max scaling against the currently observed peer set** (not fixed global constants) — this keeps the score meaningful whether the ambient network is a fast office LAN or a crowded hackathon hotspot.
3. **Stability** = `1 - min(coefficient_of_variation(last 5 throughput samples), 1.0)`; a peer with fewer than 2 samples defaults to `1.0` (a brand-new peer isn't penalized for lack of history).
4. **Reliability** = historical successful-transfer rate for that peer, default `1.0` for a peer with no prior transfers recorded.
5. **Weighted sum**, using one of three profile presets:

```
Score(p) = w_throughput · norm_throughput(p)
         + w_latency    · norm_latency(p)        # inverted: lower latency = higher score
         + w_loss       · norm_loss(p)            # inverted: lower loss = higher score
         + w_stability  · stability(p)
         + w_reliability· reliability(p)

BALANCED:     w = (0.30, 0.25, 0.25, 0.10, 0.10)
LARGE_FILE:   w = (0.40, 0.10, 0.20, 0.20, 0.10)
LOW_LATENCY:  w = (0.10, 0.45, 0.25, 0.10, 0.10)
```

6. **Rank** remaining (non-excluded) candidates by `Score` descending.
7. **Explain**: for the winner, list each weighted term's contribution, largest first; for every excluded peer, state the specific threshold it breached.

**Verified property (test `test_does_not_blindly_pick_highest_bandwidth`):** given the exact A/B/C example from the project brief (A: 20ms/2%/80Mbps, B: 60ms/0%/40Mbps, C: 35ms/5%/100Mbps), the engine selects **A**, not the highest-bandwidth peer C — confirming FR-7.

## 8. Adaptation algorithm (hysteresis controller)

```
state: consecutive_breaches = 0

on every tick (default every 0.5s, only while a transfer is TRANSFERRING):
    current  = score of the peer currently in use
    best_alt = highest score among all OTHER non-excluded known peers
    breached = (current <= 0) OR (best_alt >= current * switch_margin)   # default switch_margin = 1.3
    if breached: consecutive_breaches += 1
    else:        consecutive_breaches = 0
    if consecutive_breaches >= required_consecutive_breaches:            # default 3
        trigger reroute; reset consecutive_breaches to 0
```

Requiring **consecutive** breaches (not a single sample) is the anti-flapping mechanism — a lone noisy measurement heals the streak back to zero instead of causing a switch. **Verified** (`test_hysteresis_requires_consecutive_breaches_not_one_spike`): a single bad sample followed by a good one does not trigger a switch; three consecutive bad samples does.

### 8.1 What "reroute" actually does — and its documented limitation

- **Resume (same peer_id reconnecting):** `TransferSender.acked_chunks` is preserved; only unacknowledged chunks are (re-)sent. **Verified** (`test_resume_same_peer_after_interruption`).
- **Reroute (different peer_id):** the new peer has zero bytes of the file, so `acked_chunks` is reset and the *entire* file is (re-)sent to the new peer — there is no such thing as a byte-offset resume onto a machine that never received anything. This was caught as a real bug during implementation (an earlier version tried to send only the "remaining" chunks to the new peer, producing a truncated/zero-padded file) and fixed; the corrected behavior is what's verified by `test_reroute_to_different_peer_sends_full_file_not_partial`. **State this limitation explicitly in your report and demo narration** — "we switch fast because we detect degradation early, not because we magically resume on a machine that never had the data" is the honest and still-impressive framing.
- A second, narrower limitation: the receiver decides whether an incoming transfer is a *resume* of an existing partial file (skip truncation) versus a *fresh* transfer (truncate/preallocate) using a **name + exact size match** heuristic, not a persistent `file_id` session table. This is adequate for a demo (each transfer round uses a fresh source file) but is explicitly a simplification — a production version would track resumability by `file_id`, not filename+size.

## 9. Concurrency model

Every long-running piece of work is its own daemon thread; there is no async/await event loop, which keeps the code approachable for a viva/code walkthrough. Per node:

- 1 thread: discovery announce loop
- 1 thread: discovery listen loop
- 1 thread: discovery stale-peer reaper
- 1 thread: measurement UDP echo responder
- 1 thread: measurement TCP throughput responder (accept loop; spawns 1 short-lived thread per incoming test)
- 1 thread: `MetricsCollector` probing loop
- 1 thread: `TransferReceiver` accept loop (spawns 1 thread per incoming transfer session)
- 1 thread: `AdaptationLoop` tick loop (spawns 1 thread per active `send_to()` call)
- 1 thread (via `ThreadingHTTPServer`): dashboard HTTP server (spawns 1 thread per request)

All shared mutable state (`Discovery._peers`, `MetricsCollector._history`) is protected by a `threading.Lock` and accessed only through accessor methods — never touched directly from outside the module that owns it.

## 10. Configuration parameters (all in `protocol.py`, override via CLI flags where exposed)

| Parameter | Default | Meaning |
|---|---|---|
| `DISCOVERY_INTERVAL_SEC` | 1.0 | How often a node announces itself |
| `DISCOVERY_STALE_AFTER_SEC` | 5.0 | A peer not heard from in this long is dropped |
| `PROBE_TIMEOUT_SEC` | 0.5 | Per-probe UDP timeout before counting it as lost |
| `CHUNK_SIZE_DEFAULT` | 65536 (64 KiB) | Transfer chunk size |
| `MetricsCollector.interval_sec` | 2.0 | Idle measurement cadence (tighten during an active transfer — Phase 2 enhancement; MVP uses one fixed interval) |
| `HysteresisController.switch_margin` | 1.3 | Alternative must score ≥1.3× current to count as a breach |
| `HysteresisController.required_consecutive_breaches` | 3 | Anti-flapping threshold |
| `MAX_ACCEPTABLE_LOSS_PCT` (decision.py) | 15.0 | Hard exclusion gate |
| `MIN_ACCEPTABLE_THROUGHPUT_MBPS` (decision.py) | 0.5 | Hard exclusion gate |

## 11. Error handling & failure modes

| Failure | Behavior |
|---|---|
| Peer goes silent (no HELLO within `DISCOVERY_STALE_AFTER_SEC`) | Removed from peer table; disappears from dashboard automatically |
| Probe times out | Counted as one lost probe in that round's loss_pct; does not crash the collector loop |
| Throughput test can't connect | `measure_throughput` returns `None`; that peer is excluded from scoring until a successful measurement lands (peers lacking `latency_ms`/`throughput_mbps` are filtered out of `score_peers`'s candidate set) |
| Transfer socket drops mid-chunk | `TransferSender.send_to` catches the `OSError`/`ConnectionError`, sets status `DEGRADED`, and returns `False` — the caller (AdaptationLoop or a manual retry) decides whether to reconnect to the same peer (resume) or a different one (reroute) |
| Chunk fails checksum on receipt | Receiver replies `CHUNK_ACK` with `"ok": false`; **MVP limitation:** the current sender implementation does not yet automatically retry a failed chunk within the same `send_to()` call — this is a known Phase-2 hardening item, flagged here rather than silently assumed to work |
| Two nodes generate colliding `device_id`s | Practically prevented by using a 12-hex-character UUID4 slice (~2.8×10^14 possible values) — not formally guaranteed, acceptable for a college demo scale (3–10 devices) |

## 12. Security model — MVP scope

**Implemented:** per-chunk SHA-256 checksum verification (FR-20); receiver-side auto-accept policy is a single configurable flag (`TransferReceiver.auto_accept`), so a "require confirmation" mode is a one-line change away.

**NOT implemented in the MVP (be honest about this to judges — see PRD §9's framing philosophy):**
- No TLS/encryption on the transfer or measurement sockets — traffic is plaintext on the local network.
- No pairing confirmation UI (auto-accept is the MVP default).
- No replay/anti-spoofing protection on discovery HELLO messages (any device on the multicast group can announce itself).
- No persistent device trust/allowlist.

These are all correctly scoped to Phase 2 in PRD §10 and should be presented as deliberately deferred, not undiscovered.

## 13. Testing strategy & verified coverage

Every module has a corresponding test file exercised against **real sockets on localhost** (never mocked network calls) — this was a deliberate choice so that "the tests pass" actually means "the network code works," not "the mocks agree with the code." All 19 tests below were executed in this session and passed.

| Test file | Proves | Requirement(s) verified |
|---|---|---|
| `tests/test_discovery.py` (2 tests) | Two independent nodes find each other via multicast within seconds with zero manual config; a silent peer is eventually forgotten | FR-1, FR-2, FR-3, AC-1 |
| `tests/test_measurement.py` (4 tests) | Latency/jitter/loss probing produces real numbers against a live server and correctly reports 100% loss against a dead one; throughput measurement produces a real, positive Mbps figure and correctly returns `None` against a dead peer | FR-4 |
| `tests/test_decision.py` (5 tests) | The scoring engine does not blindly pick the highest-bandwidth peer on the brief's own worked example; high-loss peers are gated out with a stated reason; a LOW_LATENCY profile shifts ranking toward lower latency; empty input is handled; explanation text names excluded peers and their reason | FR-7, FR-8, FR-9, FR-10, AC-2 |
| `tests/test_transfer.py` (3 tests) | A full file arrives checksum-identical; an interrupted transfer to the *same* peer resumes from the checkpoint instead of restarting; a transfer rerouted to a *different* peer produces a full, correct file (not a partial/corrupted one) | FR-11, FR-12, FR-13, AC-3, AC-4 |
| `tests/test_adaptation.py` (3 tests) | The hysteresis controller requires consecutive breaches, not a single spike, before recommending a switch; a peer scoring 0 still requires the configured number of ticks unless `required_consecutive_breaches=1`; a full end-to-end synthetic-degradation scenario actually reroutes a live in-progress transfer to a healthier peer and the file arrives correct | FR-14, FR-15, FR-16, AC-5 |
| `tests/test_node_integration.py` (1 test) | The fully wired `Node` process — discovery, measurement, decision, transfer, adaptation, and the HTTP dashboard API together — supports two nodes discovering each other and completing a real file transfer purely through the same `/api/state` and `/api/send` calls the dashboard UI uses | FR-17, FR-18, AC-6 |

Run all of them with: `for f in tests/test_*.py; do python3 "$f"; done` (see BUILD_GUIDE.md for the exact, already-executed output).

## 14. What is NOT yet built (be precise about this boundary)

Everything in §1–13 above was implemented and test-verified in this session. The following are **designed in the PRD/earlier research proposal but not implemented or tested here** — treat them as your own build work, not as already-done:
- Mobile/BLE/Wi-Fi Direct/Wi-Fi Aware discovery and transfer path.
- TLS-wrapped transfer channel and pairing-confirmation UI.
- Automatic chunk-retry-on-checksum-failure within a single `send_to()` call.
- Cross-subnet rendezvous relay server.
- Offline weight auto-tuning from historical transfer logs.
- The `tc netem`-based controlled-degradation script for the live demo (BUILD_GUIDE.md gives you the approach; the exact script is yours to write and test on your own demo network, since its correctness depends on your specific OS/network setup).
