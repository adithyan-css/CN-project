"""
Level 6b — Explainability assistant (Review 3).

A question box on the dashboard: "Why did my transfer slow down?", "Why did you
turn compression on?", "Is this encrypted?". The assistant answers from the
agent's own decision log and the current measurements.

Two backends:

  * LocalAssistant (default) — no network, no API key, no cost. It matches the
    question to an intent and writes the answer from the recorded decisions and
    numbers. Deterministic, so the same state always gives the same answer,
    which is what you want when an examiner asks the same question twice.
  * LlmAssistant (optional) — if NETSHARE_LLM_KEY is set, the same facts are
    sent as a compact JSON brief to any OpenAI-compatible endpoint (a hosted
    provider or a model running locally) and it writes the reply. Nothing but those facts is sent, and the assistant is never in the
    control path: it explains decisions, it does not make them. If the call
    fails for any reason, the local answer is used instead.

Keeping the LLM out of the control loop is deliberate (it is also what the PRD
said in Review 1): a transfer decision has to happen in milliseconds, has to be
reproducible, and has to work with no internet.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Optional

INTENTS = {
    "slow": ("slow", "slower", "speed", "throughput", "fast", "mbps", "rate"),
    "pause": ("pause", "paused", "stop", "stopped", "stall", "stalled", "freeze"),
    "resume": ("resume", "resumed", "continue", "restart", "again"),
    "tune": ("window", "compression", "compress", "tuning", "tune", "setting", "settings"),
    "predict": ("predict", "prediction", "forecast", "model", "ai", "risk", "learn", "learning"),
    "security": ("secure", "security", "encrypt", "encrypted", "encryption", "safe", "pair",
                 "pairing", "code", "private"),
    "transport": ("bluetooth", "wifi", "wi-fi", "transport", "fallback", "radio"),
    "status": ("status", "progress", "how far", "done", "left", "remaining", "eta"),
    "receiver": ("receiver", "device", "who", "peer", "switch", "another"),
}


def _classify(question: str) -> str:
    q = question.lower()
    best, score = "status", 0
    for intent, words in INTENTS.items():
        hits = sum(1 for w in words if w in q)
        if hits > score:
            best, score = intent, hits
    return best


class LocalAssistant:
    name = "local"

    def answer(self, question: str, brief: dict) -> str:
        intent = _classify(question)
        t = brief.get("transfer") or {}
        health = brief.get("health") or {}
        forecast = brief.get("forecast") or {}
        decisions = brief.get("decisions") or []
        peer = t.get("peer_name", "the other device")

        def recent(kind: str) -> Optional[dict]:
            for d in reversed(decisions):
                if d.get("kind") == kind:
                    return d
            return None

        if not t:
            return ("No transfer has run yet in this session. Start one and I can explain every "
                    "decision the agent makes.")

        if intent == "slow":
            parts = [f"Current goodput is {t.get('goodput_mbps', 0)} Mbps with window "
                     f"{t.get('window')} and compression {'on' if t.get('compression') else 'off'}."]
            if health:
                parts.append(f"The link to {peer} is {'healthy' if health.get('healthy') else 'degraded'}: "
                             f"{health.get('reason')}.")
            tune = recent("tune")
            if tune:
                parts.append(f"Most recent tuning decision: {tune['sentence']}")
            if t.get("transport", "wifi") != "wifi":
                parts.append("The file is going over the Bluetooth fallback, which is much slower than "
                             "Wi-Fi by nature.")
            return " ".join(parts)

        if intent in ("pause", "resume"):
            d = recent("pause") or recent("predict") if intent == "pause" else recent("resume")
            if d:
                return (f"{d['sentence']} At that moment the transfer was at "
                        f"{d['progress_pct']:.0f}%. No data was lost — chunks already acknowledged are "
                        f"kept and only the missing ones are sent when it resumes.")
            return (f"The transfer to {peer} has not paused in this session. It pauses only after three "
                    f"bad readings in a row, or when the model predicts the link is about to fail.")

        if intent == "tune":
            d = recent("tune")
            tuner = brief.get("tuner") or {}
            head = d["sentence"] if d else (f"Settings are window {t.get('window')} and compression "
                                            f"{'on' if t.get('compression') else 'off'}.")
            if tuner.get("arms"):
                best = tuner["arms"][0]
                head += (f" Across {tuner.get('updates', 0)} learning updates, the best-performing setting "
                         f"so far is {best['arm']} at {best['avg_mbps']} Mbps average.")
            return head

        if intent == "predict":
            if forecast and forecast.get("source") == "model":
                return (f"The model forecasts {forecast['throughput_mbps']} Mbps in "
                        f"{forecast['horizon_sec']:.0f} seconds, with a {forecast['risk']:.0%} chance the "
                        f"link becomes unusable. The agent pauses early when that risk stays above 65% for "
                        f"three ticks, so it acts before the transfer stalls rather than after.")
            return ("No trained model is loaded, so the agent is using the rule-based checks: pause after "
                    "three consecutive bad readings, resume after two good ones.")

        if intent == "security":
            enc = t.get("encrypted")
            code = t.get("pair_code")
            return (f"The transfer to {peer} is {'encrypted with AES-256-GCM' if enc else 'NOT encrypted'}. "
                    f"{'The devices paired with code ' + code + ', which both users confirmed, and the key comes from an X25519 exchange, so nobody on the network can read or alter the file.' if enc and code else 'Install the cryptography package and pair the devices to enable encryption.'}")

        if intent == "transport":
            d = recent("transport")
            if d:
                return d["sentence"]
            return (f"The file is going over {t.get('transport', 'wifi')}. If Wi-Fi fails twice, the agent "
                    f"moves the rest of the file to the Bluetooth fallback automatically.")

        if intent == "receiver":
            return (f"The file is being sent to {peer} and will only ever go to {peer}. The agent pauses "
                    f"and resumes on that device; it never redirects a file to a different one, because "
                    f"the file has to reach the person you chose.")

        return (f"Transfer of {t.get('file_id', 'the file')} to {peer}: {t.get('status')} at "
                f"{t.get('progress_pct')}%, {t.get('acked_chunks')}/{t.get('total_chunks')} chunks, "
                f"{t.get('goodput_mbps', 0)} Mbps, {t.get('pauses', 0)} pause(s) and "
                f"{t.get('resumes', 0)} resume(s).")


class LlmAssistant:
    """Optional: let a hosted language model write the answer from the same facts.

    Provider-agnostic on purpose — it speaks the OpenAI-compatible chat
    completions format that nearly every provider (and local servers such as
    llama.cpp or Ollama) exposes, so the project is not tied to one vendor.
    Configure with environment variables:

        NETSHARE_LLM_KEY     API key (without it, the local assistant is used)
        NETSHARE_LLM_URL     endpoint (default: OpenAI-compatible /chat/completions)
        NETSHARE_LLM_MODEL   model name
    """

    name = "llm"
    SYSTEM = ("You explain the decisions of a file-transfer agent to a user. Answer in at most four "
              "sentences, in plain language, using only the JSON facts given. Never invent numbers. "
              "If the facts do not contain the answer, say so.")
    DEFAULT_URL = "https://api.openai.com/v1/chat/completions"
    DEFAULT_MODEL = "gpt-4o-mini"

    def __init__(self, api_key: str, model: Optional[str] = None, url: Optional[str] = None):
        self.api_key = api_key
        self.model = model or os.environ.get("NETSHARE_LLM_MODEL", self.DEFAULT_MODEL)
        self.url = url or os.environ.get("NETSHARE_LLM_URL", self.DEFAULT_URL)

    def answer(self, question: str, brief: dict) -> str:
        payload = {"model": self.model, "max_tokens": 300,
                   "messages": [{"role": "system", "content": self.SYSTEM},
                                {"role": "user",
                                 "content": f"Facts:\n{json.dumps(brief)[:6000]}\n\nQuestion: {question}"}]}
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode(),
                                     headers={"content-type": "application/json",
                                              "authorization": f"Bearer {self.api_key}"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read())
        return (body.get("choices", [{}])[0].get("message", {}).get("content", "") or "").strip()


class Assistant:
    """Uses the LLM when one is configured, and always has the local fallback."""

    def __init__(self, api_key: Optional[str] = None):
        self.local = LocalAssistant()
        key = api_key or os.environ.get("NETSHARE_LLM_KEY")
        self.remote = LlmAssistant(key) if key else None
        self.backend = "llm" if self.remote else "local"

    def answer(self, question: str, brief: dict) -> dict:
        question = (question or "").strip()[:400]
        if not question:
            return {"answer": "Ask me anything about this transfer.", "backend": "local"}
        if self.remote:
            try:
                text = self.remote.answer(question, brief)
                if text:
                    return {"answer": text, "backend": "llm"}
            except (urllib.error.URLError, OSError, ValueError, KeyError):
                pass
        return {"answer": self.local.answer(question, brief), "backend": "local"}
