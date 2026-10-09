# NetShare

**AI-agent based auto-pairing file sharing for nearby devices.**
Computer Networks project — code for Review 2 and Review 3.

Two laptops find each other with no configuration, confirm a 6-digit pairing
code, and move a file over an encrypted channel. An agent on each device keeps
measuring the link, predicts trouble before it happens, tunes the transfer while
it runs, pauses and resumes without losing progress, and explains every decision
in plain language.

Everything runs on **Python's standard library**. The optional extras
(`cryptography` for AES-GCM, `bleak`/`winsdk` for the Bluetooth radio,
`scikit-learn` + `matplotlib` for training and charts) are only needed for the
features that require them, and the program says clearly when one is missing
instead of pretending.

---

## Quick start

```bash
# on each laptop, in the project folder
python -m pathsense.node --name "Laptop A" --dashboard-port 8801
```

Open <http://localhost:8801>. Nearby devices appear on their own. Pick one,
choose a file, press Send, and confirm the 6-digit code on the receiving laptop.

Useful switches:

| Switch | What it does |
|---|---|
| `--discovery auto\|ble\|ble-sim\|multicast` | how devices find each other (default `auto`: Bluetooth if the radio works, otherwise UDP multicast) |
| `--no-encryption` | plaintext transfers, for a side-by-side comparison |
| `--auto-pair` | confirm pairing codes automatically (unattended runs only) |
| `--log-metrics data/links.csv` | record every measurement for training |
| `--model path.json` | use a different trained model |
| `--no-learning` / `--reset-learning` | freeze or clear the tuner |

---

## How it works

```
        Bluetooth LE beacon                 encrypted Wi-Fi (or Bluetooth fallback)
Laptop A ─────────────────────► Laptop B    Laptop A ══════════════════════════► Laptop B
   discovery + contact details                 chunks, SHA-256 each, pipelined
                 │                                          ▲
                 ▼                                          │
       X25519 key exchange ─► same 6-digit code ─► AES-256-GCM session key
```

Inside each node:

| Layer | File | Job |
|---|---|---|
| Discovery | `discovery.py`, `ble.py` | UDP multicast beacons, or BLE advertisements carrying device id, IPv4, ports and key fingerprint in 20 bytes |
| Pairing | `pairing.py` | X25519 + HKDF; the 6-digit code is derived from both public keys, so a swapped key shows different codes |
| Encryption | `secure_channel.py` | AES-256-GCM frames, separate key per direction, nonce never reused |
| Measurement | `measurement.py` | UDP echo probes (latency, jitter, loss) and a TCP burst (throughput), every 2 s |
| Scoring | `decision.py` | weighted score per device, plus the absolute health check for the receiver in use |
| Transfer | `transfer.py` | chunked, checksummed, pipelined, resumable; one sender is bound to one receiver |
| Transports | `transports.py` | Wi-Fi TCP, Bluetooth RFCOMM, and a paced stand-in where Python has no RFCOMM |
| Prediction | `predictor.py` | boosted-tree forecast of throughput and failure risk 5 s ahead, pure-Python inference |
| Learning | `tuner.py` | LinUCB contextual bandit choosing window size and compression from measured reward |
| Adaptation | `adaptation.py` | the sense → predict → act → explain loop, with hysteresis both ways |
| Explanation | `assistant.py` | answers questions from the agent's own decision log; optional LLM backend |
| Dashboard | `dashboard.py` | live metrics, pairing prompt, AI panel, assistant, decision log |

### Why it pauses instead of switching devices

An earlier design re-routed a struggling transfer to a *different* device. That
is wrong: the file has to reach the person you chose. The agent therefore keeps
the receiver fixed, pauses when the link is unusable, and resumes from the first
missing chunk when it recovers.

### What was deliberately dropped

Forward error correction was in the Review 3 plan as a tuning knob. It was cut
after analysis: the data path is TCP, which already retransmits lost segments,
so parity chunks would add overhead and no reliability. FEC belongs with a UDP
data path (future work, TRD).

---

## The AI parts, honestly

**Prediction.** `tools/train_model.py` trains two gradient-boosted tree models
(throughput in 5 s; probability the link is unusable in 5 s) and exports them as
JSON. On the shipped simulated dataset (≈47k rows):

| | model | baseline | baseline meaning |
|---|---|---|---|
| Throughput MAE | **9.7 Mbps** | 10.5 Mbps | "it will be exactly what it is now" |
| Degradation AUC | **0.72** | 0.51 | "it is bad right now" |

That is a real but modest gain, which is what honest 5-second-ahead prediction
on a noisy radio link looks like. It is enough to be useful and not enough to
justify acting on a weak hunch, so the agent only pauses early when the risk
stays above 80% for three ticks; between 55% and 80% it just shrinks the window,
which is cheap and reversible. An earlier threshold of 65% made the AI agent
*slower* than the plain rules in evaluation — that result is why the threshold
is where it is.

**Learning.** The bandit learns which window size and compression setting works
in which conditions, and keeps that knowledge between runs. In the test
environment it reaches about **1.15x** the throughput of the fixed rule.

**The assistant never decides anything.** It explains decisions after the fact,
so control stays fast, reproducible and offline.

---

## Results

```bash
python tools/train_model.py                      # train (needs scikit-learn)
python tools/evaluate.py --trials 6 --mb 6       # compare the three policies
```

Same file, same seeded link conditions (bandwidth cap, delay and real
connection cuts through a TCP proxy), three policies:

| Policy | Finished | Mean time | Mean throughput |
|---|---|---|---|
| Fixed settings, no agent | 5/6 | 11.2 s | 5.0 Mbps |
| Rule-based agent (Review 2) | 6/6 | 4.3 s | 15.3 Mbps |
| **AI agent (Review 3)** | **6/6** | **2.7 s** | **18.7 Mbps** |

Charts and raw rows are in `results/`. Two caveats stated plainly: these numbers
come from an application-level link simulator, not `tc netem`/Clumsy on real
radios, and the AI column is a *warmed-up* agent — on its very first runs the
bandit explores and is roughly level with the rule-based agent. Both effects are
reproducible with `--fresh-learning`.

---

## Tests

```bash
for f in tests/test_*.py; do python "$f"; done
```

34 tests across 7 files, using real sockets, real files and real threads:
discovery, measurement, scoring, transfer, pause/resume, pairing and crypto
(including a man-in-the-middle check), BLE payloads, model quality against a
baseline, bandit learning, and the assistant.

## Platform notes

- **Bluetooth**: scanning needs `bleak`; advertising needs `winsdk` on Windows or
  BlueZ on Linux. Without them the node says so and uses UDP multicast.
- **Bluetooth fallback transport**: Python exposes RFCOMM sockets on Linux and
  macOS but not Windows, where a paced TCP link stands in and is labelled
  "emulated" everywhere it appears.
- **Encryption** needs `pip install cryptography`; without it the dashboard says
  the transfer is not encrypted rather than hiding it.
