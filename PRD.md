# PRD — PathSense
**Explainable, Adaptive Peer & Path Selection for Proximity File Transfer**

| | |
|---|---|
| Document | Product Requirements Document |
| Status | Draft v1.0 — MVP scope locked, Phase 2/Advanced scope proposed |
| Owner | Adithyan C S S |
| Companion docs | TRD.md (technical spec), BUILD_GUIDE.md (implementation steps), USER_FLOW.md (demo/run guide) |

---

## 1. Purpose

PathSense is a proximity file-sharing system for a college Computer Networks project. Unlike AirDrop, Google Nearby Connections/Quick Share, or Wi-Fi Direct, it does not just move a file from one nearby device to another — it continuously measures every nearby candidate device's network quality, picks the best one using a transparent scoring formula it can explain in plain language, and automatically re-routes a transfer if the chosen path degrades while the transfer is running. This document defines *what* the product must do and why; TRD.md defines *how*.

## 2. Problem statement

Consumer proximity-sharing tools solve discovery well (walk up, appear in a list, tap to send) but treat the transfer channel as a black box: they pick a path once, using undocumented internal logic, and never reconsider that choice even if the connection visibly degrades mid-transfer. The user gets no explanation for why a transfer is slow and no recovery when it could have used a better nearby device instead. This is a real, demonstrable gap — not a claim of "no prior art" — documented against the public behavior of AirDrop, Nearby Connections, and Wi-Fi Aware in the accompanying research (see `project-proposal.md` from the earlier research phase).

## 3. Goals

- G1 — Automatic discovery: nearby PathSense nodes find each other with zero manual configuration (no typed IPs).
- G2 — Transparent decision-making: when more than one peer is available, the system measures each one (latency, jitter, packet loss, throughput) and explains, in plain text, exactly why it picked the peer it picked.
- G3 — Adaptive transfer: an in-progress transfer detects sustained degradation on its current path and reroutes to a better available peer without restarting from zero when it's the *same* peer reconnecting, and without unnecessary delay when it's a *different* peer.
- G4 — Provable benefit: the system must produce before/after evidence (metrics, graphs) showing the adaptive behavior outperforms a static, single-path baseline under the same injected conditions.
- G5 — Demonstrability: everything above must be visibly observable on a live dashboard during a 3–5 minute demo, not hidden in logs.

## 4. Non-goals (explicitly out of scope for this project)

- NG1 — Internet-scale routing or NAT traversal beyond a shared LAN/hotspot (no TURN relay in MVP).
- NG2 — Mobile app store distribution (Android/iOS native apps) — the MVP targets laptops.
- NG3 — Reinforcement learning or LLM-based path selection in the live decision loop (see PRD §9 rationale).
- NG4 — Production-grade PKI/certificate authority for security (a lighter, documented model is used instead — see §8).
- NG5 — Guaranteed operation on networks that block UDP multicast (client-isolated public/enterprise Wi-Fi) — the demo network is expected to be a hotspot the team controls.

## 5. Target users / personas

- **Demo judges** — need to *see* the decision and the adaptation happen live, with plain-language explanations, not just a fast transfer.
- **The project team (builders)** — need a system they can extend (mobile, more peers) after the MVP without re-architecting.
- **A general end user (illustrative, not the primary audience for a college demo)** — someone sharing a file with a nearby device who currently gets no visibility or resilience from existing tools.

## 6. Use cases / user stories

1. As a user, when I open PathSense, I see a live list of nearby devices with their name and connection quality, without typing anything.
2. As a user, when I send a file and more than one peer is available, I see the system evaluate all candidates and I see *why* it picked the one it picked (the specific numbers, not just "best peer").
3. As a user, if the chosen peer's connection degrades badly while my transfer is running, I see the system notice, tell me it's searching for a better option, switch, and show me the resulting improvement — without me having to restart the transfer myself.
4. As a user, if my chosen peer briefly drops and reconnects, my transfer picks up where it left off rather than starting over.
5. As a judge, I can ask "why not peer C?" and the system already has a plain-language answer on screen.
6. As a builder, I can run the same experiment (static peer selection vs. adaptive) twice and get comparable, graphable results.

## 7. Functional requirements

### 7.1 Discovery
- FR-1: A node MUST announce itself on the local network at a regular interval without user action.
- FR-2: A node MUST maintain a live table of currently-visible peers and remove a peer that stops announcing within a bounded timeout.
- FR-3: Discovery MUST require no manually entered IP address, port, or pairing code for two nodes on the same LAN/hotspot to see each other.

### 7.2 Measurement
- FR-4: The system MUST actively measure, per discovered peer: round-trip latency, jitter, packet loss percentage, and achieved throughput (a real measured value, not an estimate from a single packet).
- FR-5: Measurement MUST repeat on a rolling interval for every currently discovered peer, not just once at startup.
- FR-6: Measurement MUST continue during an active transfer, at a tighter interval, so degradation can be detected while it's happening.

### 7.3 Decision engine
- FR-7: Given ≥2 measured peers, the system MUST NOT simply select the peer with the highest raw bandwidth — selection MUST weigh latency, loss, throughput, and stability together (see TRD §7 for the exact formula).
- FR-8: The system MUST exclude a peer from consideration if its measured packet loss or throughput crosses a defined unusable threshold, rather than merely down-weighting it.
- FR-9: The system MUST expose, for the selected peer and every excluded/non-selected candidate, a plain-language explanation referencing the actual measured numbers that drove the decision.
- FR-10: The weighting MUST be adjustable by a declared transfer profile (e.g., large-file vs. low-latency) — the profile changes the weighting, not the underlying formula.

### 7.4 Transfer
- FR-11: File transfer MUST be split into checksummed chunks, and the receiver MUST verify each chunk's checksum before accepting it.
- FR-12: If the connection to the current peer drops and reconnects, the transfer MUST resume from the last acknowledged chunk rather than restarting the whole file.
- FR-13: If the system reroutes to a different peer, the new peer MUST end up with a byte-identical copy of the file (no silent partial/corrupted transfer).

### 7.5 Adaptation
- FR-14: While a transfer is active, the system MUST continuously re-evaluate whether a different currently-known peer has become significantly better than the one in use.
- FR-15: The system MUST NOT reroute on a single noisy/borderline measurement — it MUST require a sustained (multi-sample) degradation before switching, to avoid flapping.
- FR-16: On triggering a reroute, the system MUST log/display a human-readable event describing what degraded and what it switched to.

### 7.6 Dashboard
- FR-17: The system MUST display, live: discovered peers and their current metrics, the current peer's score and explanation, current transfer progress, and a running log of adaptation events.
- FR-18: The system MUST let the user initiate a transfer to a chosen peer from the dashboard itself (no separate CLI step required for the demo).

### 7.7 Security (MVP-appropriate, see TRD §12 for exact scope)
- FR-19: A receiving node MUST NOT silently accept a file from an unrecognized sender without at least a configurable accept/auto-accept policy that the user controls.
- FR-20: Every chunk and the whole file MUST be checksum-verified; a corrupted chunk MUST be detectable.

## 8. Non-functional requirements

- NFR-1 (Reliability): The MVP demo path (discovery → measurement → decision → transfer → one controlled degradation → reroute → completion) must be reproducible on demand, not dependent on ambient network conditions the team doesn't control.
- NFR-2 (Explainability): Every automated decision the system makes must have a corresponding human-readable justification retrievable from the dashboard at the moment it's made — not reconstructed after the fact from logs.
- NFR-3 (Portability): The MVP must run with nothing beyond a standard Python 3 installation — no OS-specific SDK, no app store build, no admin/root privileges — on Windows, macOS, and Linux laptops alike.
- NFR-4 (Performance): A multi-second file transfer must be achievable over a real Wi-Fi hotspot within the demo window (a few MB in well under a minute).
- NFR-5 (Observability): State needed for the six experiments in the research phase (latency vs. time, loss vs. throughput, selection accuracy, static vs. adaptive, before/after, single- vs. adaptive-path) must be capturable from the running system, not invented after the fact.

## 9. Explicit product decision: no ML/LLM in the live decision loop

The scoring and rerouting decisions are deterministic (a documented weighted formula plus a hysteresis threshold), not a trained model or an LLM call. This is a requirement, not an oversight: a live path-selection decision needs to be explainable on demand and fast (sub-second), and a judge's hardest question ("is your AI actually deciding, or is this just a formula?") must have an honest, defensible answer. An optional, clearly-labeled offline weight-tuning step is allowed in Phase 2/Advanced scope but must never be the thing making the live decision.

## 10. Scope

**MVP (must ship, demo-ready):** FR-1 through FR-20 above, running on 3 laptops over a shared Wi-Fi hotspot, using LAN/UDP-multicast discovery (no BLE/Wi-Fi Direct/Wi-Fi Aware dependency).

**Phase 2:** cross-subnet rendezvous relay for non-shared networks; multi-peer parallel/split transfer; a mobile companion shell using BLE/Wi-Fi Direct for real proximity discovery on phones; TLS-wrapped transfer channel and stronger pairing confirmation (MVP transfer is plaintext TCP — see TRD §12 limitations).

**Advanced / stretch:** offline weight auto-tuning from historical transfer logs (the one legitimate ML claim, applied only between sessions, never live); Wi-Fi Aware (NAN) as an additional discovery/measurement medium; fuzzy-logic or constrained-optimization chunk placement across simultaneously-active multi-peer transfers.

## 11. Assumptions & constraints

- A1: All demo devices share one Wi-Fi network the team controls (e.g., a laptop's own mobile hotspot), specifically to avoid client-isolation/multicast-blocking issues on networks the team doesn't control.
- A2: Judges will ask "why is this not just AirDrop/Quick Share" — the product must have a ready, honest answer (see the research proposal's Judge Q&A).
- A3: Python 3.9+ is available on all demo machines; no internet access is required at demo time.
- A4: Controlled network degradation (throttling/moving a device) is performed intentionally during the demo, not left to chance.

## 12. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Multicast blocked on the demo network | Discovery fails live | Use a team-controlled hotspot (A1); document a manual-IP fallback |
| No real degradation occurs naturally during the demo | The adaptation story falls flat | Self-triggered, controlled degradation (throttle script or physical distance) — never rely on ambient conditions |
| Judge conflates "intelligent" with "AI/ML" and expects a model | Perceived as less impressive | Lead with the explicit, honest framing in PRD §9 — this reads as engineering maturity, not a weakness |
| Stop-and-wait transfer protocol caps throughput below what a sliding-window protocol could achieve | Numbers look modest | Document this as a deliberate trade for correctness/explainability (TRD §6); still enough to show clear before/after deltas |

## 13. Success metrics / acceptance criteria

- AC-1: Two+ nodes discover each other within 5 seconds with zero manual configuration (verifies FR-1–FR-3).
- AC-2: Given 3 candidate peers shaped like the brief's worked example (A: low latency/some loss/high bandwidth, B: higher latency/zero loss/mid bandwidth, C: mid latency/highest loss/highest bandwidth), the system does not select C (verifies FR-7).
- AC-3: A transfer interrupted and reconnected to the *same* peer completes without re-sending already-acknowledged chunks (verifies FR-12).
- AC-4: A transfer rerouted to a *different* peer produces a checksum-identical file on that peer (verifies FR-13).
- AC-5: A single noisy sample does not trigger a reroute; sustained degradation across multiple consecutive samples does (verifies FR-15).
- AC-6: The dashboard's `/api/state` reflects live peers, metrics, scores, transfer progress, and an event log without a page reload (verifies FR-17).

All six acceptance criteria above are implemented as automated tests in the reference implementation — see BUILD_GUIDE.md for the exact test code and verified pass output.

## 14. Glossary

- **Peer** — another PathSense node currently visible via discovery.
- **Score** — the weighted 0–1 value the Decision Engine assigns a peer.
- **Hysteresis** — requiring several consecutive bad measurements (not one) before acting, to avoid switching back and forth on noise.
- **Reroute** — redirecting an in-progress transfer to a different peer.
- **Resume** — continuing a transfer to the *same* peer from the last acknowledged chunk after an interruption.
- **Transfer profile** — a named weighting preset (e.g., LARGE_FILE, LOW_LATENCY, BALANCED) that changes how the scoring formula weighs its inputs.
