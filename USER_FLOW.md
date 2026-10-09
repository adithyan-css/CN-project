# USER_FLOW — Connecting 3 Computers & Running the Demo

Plain-language guide for the day of the demo. No networking jargon needed
to follow this — the technical "why" for each step lives in TRD.md if
you want it.

## What you need

- 3 laptops (Windows, Mac, or Linux — any mix is fine).
- Python already installed on all 3 (check by opening a terminal/command
  prompt and typing `python3 --version` — if that fails, try `python --version`).
- The `pathsense` project folder copied onto all 3 laptops (USB drive, or
  `git clone`/zip-and-copy — any way of getting the same folder onto each
  machine works; no installation step is needed since it uses only Python's
  built-in libraries).

## Step 1 — Get all 3 laptops on the SAME Wi-Fi network

This is the single most important step. Discovery only works between
devices on the same network.

**Easiest, most reliable option: make one laptop a Wi-Fi hotspot and
connect the other two to it.**
- **Windows:** Settings → Network & Internet → Mobile hotspot → turn it on. Note the network name and password shown there.
- **Mac:** System Settings → General → Sharing → Internet Sharing (share your Wi-Fi or Ethernet connection over Wi-Fi) — turn it on, note the network name/password. (Exact wording varies slightly by macOS version — look for "Internet Sharing" or "Personal Hotspot" under Sharing/General settings.)
- Then on the other two laptops, just connect to that Wi-Fi network like any other, using the password shown.

**Why a hotspot you control, instead of the venue's Wi-Fi:** some public/college/event Wi-Fi networks block device-to-device discovery on purpose (a security feature called "client isolation"). A hotspot from one of your own laptops avoids that risk entirely — don't discover this problem live during judging.

## Step 2 — Start NetShare on each laptop

Open a terminal on each laptop, go into the project folder, and run:

```
# On Laptop A:
python -m pathsense.node --name "Laptop A" --dashboard-port 8801

# On Laptop B:
python -m pathsense.node --name "Laptop B" --dashboard-port 8801
```

The first time, Windows asks whether to allow Python through the firewall —
tick **both** Private and Public, or the laptops will not see each other.

The startup lines tell you exactly what this machine can do: which discovery it
is using (Bluetooth or UDP multicast), whether encryption is available, whether
a trained prediction model was found, and whether the learning tuner is on. Read
them out during the demo; they are the honest capability list for that laptop.

Optional switches worth knowing for the review:

- `--discovery ble` — force Bluetooth discovery (falls back with a message if
  the radio or its libraries are missing).
- `--discovery ble-sim` — run the real BLE beacon format over the network
  instead of the radio, clearly labelled, for machines with no usable Bluetooth
  stack.
- `--no-encryption` — run one laptop in plaintext to show the difference.
- `--log-metrics data/links.csv` — record measurements to train the model on.

## Step 3 — Open the dashboard

On any laptop (or all of them, if you want each person to see their own view), open a web browser and go to:

```
http://localhost:8801
```

Within a few seconds, the other two laptops will appear automatically in the "Nearby Devices" table — no IP address typing, no pairing code. If a laptop doesn't show up within ~10 seconds, double check it's on the same Wi-Fi network from Step 1.

You'll see each nearby device's live latency, packet loss, throughput, and a computed "score" updating continuously — this is the system actively measuring the network in real time, not a static list.

## Step 4 — Send a file and confirm the pairing code

On Laptop A's dashboard:

1. Pick Laptop B from the dropdown.
2. Click **Browse…** and choose a file (1 GB or more so the transfer lasts long
   enough to interrupt), or type its full path.
3. Click **Send**.

Both laptops now show the same **6-digit code**. On Laptop B, press **"Codes
match — pair"**. Nothing is sent until that happens, and after this first time
the two devices remember each other and pair silently.

Say this while it happens: the code comes from an X25519 key exchange, and it is
derived from *both* public keys, so if anyone had swapped a key in the middle the
two screens would show different numbers. Once confirmed, every byte is
AES-256-GCM encrypted, which the dashboard shows as an "encrypted" badge on both
sides.

The **Agent & AI** panel then shows the model's forecast (throughput 5 s ahead
and the risk the link fails), the window size and compression the learning tuner
has chosen, and what it has learned so far.

## Step 5 — The live moment: pause and resume on the same receiver

While the transfer runs, make the receiver's link bad:

- **Real (best):** turn off Wi-Fi on Laptop B for about 10 seconds, then back on.
- **Simulated (backup):** click **Simulate link degradation (8 s)**. It injects
  clearly labelled fake measurements; the pause and resume that follow are real.

The log shows something like:

```
12:49:52 — Link to Laptop B degraded: packet loss 40% exceeds 15%. Pausing at 41% (chunk 7809/19200); progress kept.
12:49:59 — Link to Laptop B recovered (latency 0.9 ms, loss 0%, throughput 48.2 Mbps). Resuming from 41% (chunk 7810/19200); only missing chunks are sent.
12:50:03 — Transfer complete: all 19200 chunks verified after 1 pause(s) and 1 resume(s).
```

If the trained model is loaded you may instead see the agent act *early*:

```
12:49:50 — Predicted degradation on Laptop B's link: 86% risk within 5s (forecast 1.8 Mbps). Pausing early at 38% instead of waiting for it to stall.
```

Laptop B shows the mirror image in **Incoming Transfer**: RECEIVING →
INTERRUPTED ("chunks already received are kept") → COMPLETE with the saved path.

## Step 6 — Ask the assistant

In **Ask the assistant**, type "Why did my transfer slow down?" or press one of
the suggested questions. It answers from the agent's own decision log and says
whether the answer came from the local explainer or a language model. Point out
that it only explains decisions and never makes them — control has to be fast,
repeatable and work with no internet.

## Step 7 — Show the evidence

```
python tools/evaluate.py --trials 6 --mb 6
```

Three policies (no agent, Review 2 rules, Review 3 AI) over identical seeded
link conditions, with the charts in `results/`. Quote the numbers from
README.md and be straight about the two caveats: the link is simulated at the
application level, and the AI figure is a warmed-up agent.

## What this demonstrates, in one sentence for judges

"Other nearby file-sharing apps just stall or fail and tell you nothing. Ours notices the link degrading, says why, pauses without losing progress, and resumes from where it stopped once the link recovers."

## Troubleshooting (things that can genuinely go wrong, and what they mean)

| What you see | What it means | What to do |
|---|---|---|
| A laptop never appears in "Nearby Devices" | It's not on the same network, or that network blocks multicast discovery | Re-check Step 1; switch everyone to the same laptop's hotspot |
| A peer appears but its latency/loss/throughput stay blank ("-") | Discovery worked but active measurement hasn't completed a round yet | Wait a few more seconds — measurement runs on its own interval |
| "Send failed: peer ... not currently discovered" | The peer's entry expired (it went offline, or the network dropped) right before you clicked Send | Make sure the target laptop's `pathsense.node` process is still running, then try again |
| The transfer never pauses even though you degraded the connection | The degradation wasn't sustained or severe enough (the thresholds are over 15% loss, over 300 ms latency, under 0.5 Mbps, or no fresh measurement for 8 s) | Turn Wi-Fi off for longer, or use **Simulate link degradation**. Several consecutive bad readings are required on purpose |
| The transfer finishes before you can degrade it | The file is too small for your network's speed | Use a bigger file (1–2 GB) |
| You want to rehearse without 3 physical laptops | You can run all 3 `pathsense.node` processes on one machine using different `--measurement-port`/`--transfer-port`/`--dashboard-port` values for each (see BUILD_GUIDE.md's test files for exactly this pattern) | Use different port numbers per instance, e.g. `--dashboard-port 8801`, `8802`, `8803` |

## What NOT to promise judges (matches TRD.md §12/§14 — stay honest here)

- Transfers **are** encrypted now (AES-256-GCM after pairing), but only if the `cryptography` package is installed — the dashboard says plainly when it is not.
- Bluetooth **discovery** is real where the radio and its libraries are available; `--discovery ble-sim` carries the same beacon format over the network and is labelled as simulated. The Bluetooth **fallback transport** uses real RFCOMM on Linux/macOS and a speed-limited stand-in on Windows, labelled "emulated".
- The prediction model is trained on **simulated** link traces unless you have collected real ones with `--log-metrics`. Its gain over the naive baseline is real but modest (AUC 0.72 vs 0.51) — quote it as that, not as magic.
- The agent does **not** switch the file to another device. It pauses and resumes on the receiver you chose (Review 2 design change). Picking a better path to the same receiver (Wi-Fi Direct or Bluetooth fallback) and the AI prediction and tuning are Review 3 work.
- **Simulate link degradation** fakes the *measurements* only, and the dashboard labels it as simulated. Say so if you use it.
- This runs over your own local Wi-Fi/hotspot — it does not require the internet, but it also isn't built to work across two networks that aren't sharing one hotspot/router (that's a named Phase 2 item, not a bug).
