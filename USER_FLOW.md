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

## Step 2 — Start PathSense on each laptop

Open a terminal on each laptop, go into the `pathsense` project folder, and run:

```
# On Laptop A:
python3 -m pathsense.node --name "Laptop A" --dashboard-port 8801

# On Laptop B:
python3 -m pathsense.node --name "Laptop B" --dashboard-port 8801

# On Laptop C:
python3 -m pathsense.node --name "Laptop C" --dashboard-port 8801
```

(Use whichever names you want judges to see — they show up in the dashboard exactly as typed.) Each laptop prints something like:

```
PathSense node 'Laptop A' running. Dashboard: http://localhost:8801
```

Leave that terminal window open and running on all 3 laptops for the whole demo.

## Step 3 — Open the dashboard

On any laptop (or all of them, if you want each person to see their own view), open a web browser and go to:

```
http://localhost:8801
```

Within a few seconds, the other two laptops will appear automatically in the "Nearby Devices" table — no IP address typing, no pairing code. If a laptop doesn't show up within ~10 seconds, double check it's on the same Wi-Fi network from Step 1.

You'll see each nearby device's live latency, packet loss, throughput, and a computed "score" updating continuously — this is the system actively measuring the network in real time, not a static list.

## Step 4 — Send a file

On the dashboard:
1. Pick a peer from the dropdown ("Peer:").
2. Type the **full file path** of a file that exists on that specific laptop (e.g., `/home/you/demo_video.mp4` on Mac/Linux, or `C:\Users\you\demo_video.mp4` on Windows).
3. Click **Send**.

The "Current Transfer" panel shows live progress, and the "Adaptation Events" log shows the system's own running commentary — including the peer-selection explanation the moment the transfer starts.

## Step 5 — The live "wow" moment (do this on purpose, don't wait for it to happen naturally)

While a transfer is running, deliberately make the chosen laptop's connection worse — either:
- physically move that laptop farther from the hotspot laptop (weakens real Wi-Fi signal), or
- (if you built the optional throttle script from BUILD_GUIDE.md's "what's left" section) trigger it on that laptop.

Watch the dashboard: the degrading laptop's latency/loss numbers will visibly worsen, and — after a few seconds of sustained bad readings (this delay is intentional, so one blip doesn't cause a switch) — the Adaptation Events log will show something like:

```
14:32:07 — Route degraded (current score 0.31). Searching for better transfer option...
14:32:08 — Switched to Laptop C — 2.1x better than Laptop B
```

The transfer keeps going, now via Laptop C, and completes successfully.

## What this demonstrates, in one sentence for judges

"Every other nearby file-sharing app would just get slower right now and tell you nothing — ours notices, explains why, and fixes itself automatically while the transfer is still running."

## Troubleshooting (things that can genuinely go wrong, and what they mean)

| What you see | What it means | What to do |
|---|---|---|
| A laptop never appears in "Nearby Devices" | It's not on the same network, or that network blocks multicast discovery | Re-check Step 1; switch everyone to the same laptop's hotspot |
| A peer appears but its latency/loss/throughput stay blank ("-") | Discovery worked but active measurement hasn't completed a round yet | Wait a few more seconds — measurement runs on its own interval |
| "Send failed: peer ... not currently discovered" | The peer's entry expired (it went offline, or the network dropped) right before you clicked Send | Make sure the target laptop's `pathsense.node` process is still running, then try again |
| The reroute never triggers even though you degraded the connection | The degradation wasn't sustained long enough, or wasn't severe enough to cross the switch threshold | Make the change more drastic (move farther, or degrade for longer) — the system requires several consecutive bad readings on purpose, so one brief dip won't trigger it |
| You want to rehearse without 3 physical laptops | You can run all 3 `pathsense.node` processes on one machine using different `--measurement-port`/`--transfer-port`/`--dashboard-port` values for each (see BUILD_GUIDE.md's test files for exactly this pattern) | Use different port numbers per instance, e.g. `--dashboard-port 8801`, `8802`, `8803` |

## What NOT to promise judges (matches TRD.md §12/§14 — stay honest here)

- The transfer channel in this MVP is **not encrypted** — say so if asked; it's a documented, deliberate scope decision (Phase 2 item), not a hidden gap.
- Rerouting to a **different** peer restarts that peer's copy of the file from the beginning (it can't "resume" data another device already received) — the win is switching away from a bad path quickly, not magic teleportation of already-sent bytes. Resuming from a checkpoint only applies when reconnecting to the **same** peer after a drop.
- This runs over your own local Wi-Fi/hotspot — it does not require the internet, but it also isn't built to work across two networks that aren't sharing one hotspot/router (that's a named Phase 2 item, not a bug).
