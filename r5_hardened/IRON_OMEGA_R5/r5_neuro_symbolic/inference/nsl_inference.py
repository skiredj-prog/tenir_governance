"""
R5 NEURO-SYMBOLIC: NSL Inference Engine
=========================================
Dual-backend inference: fine-tuned LLM (Ollama) + LALR(1) grammar fallback.

Decision tree:
  1. If grammar_only_mode=True → always use grammar parser (deterministic, no GPU)
  2. Else: call Ollama LLM → validate JSON schema → if confidence >= 0.82 → accept
  3. If LLM fails JSON validation OR confidence < 0.82 → fall back to grammar parser

The grammar is the zero-hallucination guarantee.
The LLM (when fine-tuned) handles novel phrasings the grammar doesn't cover.
"""

from __future__ import annotations
import json
import logging
import time
import urllib.request
import urllib.error
from dataclasses import dataclass
from typing import Optional, Dict, Any, Tuple
from uuid import uuid4

from ..grammar.nsl_grammar import compile_nsl_safe, StatementAST, NSLParser

logger = logging.getLogger("r5.nsl.inference")

# ─── CONFIG ───────────────────────────────────────────────────────────────────

@dataclass
class NSLInferenceConfig:
    model_name: str = "tenir-nsl:latest"
    grammar_only_mode: bool = False
    ollama_base_url: str = "http://localhost:11434"
    llm_timeout_sec: float = 8.0
    confidence_threshold: float = 0.82
    max_tokens: int = 256


@dataclass
class NSLInferenceResult:
    inference_backend: str
    intent: Optional[str]
    entity_type: Optional[str]
    entity_identifier: Optional[str]
    compiled_params: Dict[str, float]
    confidence: float
    latency_ms: int
    error: Optional[str]
    final_ast: Optional[Dict[str, Any]]

    @property
    def validation_passed(self) -> bool:
        return not self.error and bool(self.compiled_params)

# ─── SYSTEM PROMPT (same as dataset_generator) ────────────────────────────────

_SYSTEM_PROMPT = (
    "You are a deterministic NSL compiler for the TENIR institutional governance system. "
    "Your ONLY function is to parse governance narrative text and output a valid JSON AST. "
    "Output ONLY valid JSON. No explanation, no preamble, no markdown fences.\n"
    "Schema: {\"intent\": \"ACCELERATE|DELAY|RESTRICT|ALLOCATE|QUERY\", "
    "\"entity_type\": \"RND_PROJECT|PROCUREMENT_CONTRACT|BUDGET|PERSONNEL|LEGAL_POLICY\", "
    "\"entity_identifier\": \"<string or null>\", "
    "\"modifiers\": {\"urgency\": <float 0-1 or null>, \"risk\": <float 0-1 or null>, "
    "\"scale\": <float 0-1 or null>, \"capacity_delta\": <float -1 to 1 or null>}, "
    "\"context\": \"<reason phrase or null>\", \"confidence\": <float 0-1>}"
)

# ─── LLM OUTPUT SCHEMA (expected from fine-tuned model) ───────────────────────

_REQUIRED_KEYS = {"intent", "entity_type", "modifiers", "confidence"}
_VALID_INTENTS  = {"ACCELERATE", "DELAY", "RESTRICT", "ALLOCATE", "QUERY"}
_VALID_ENTITIES = {"RND_PROJECT", "PROCUREMENT_CONTRACT", "BUDGET", "PERSONNEL", "LEGAL_POLICY"}

def _validate_llm_json(raw: str) -> Tuple[bool, Optional[Dict], str]:
    """
    Validates raw LLM output as a legal NSL AST JSON.
    Returns (is_valid, parsed_dict, error_message).
    """
    # Strip markdown fences if present (even well-tuned models sometimes emit them)
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(l for l in lines if not l.startswith("```")).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        return False, None, f"JSON parse error: {e}"

    missing = _REQUIRED_KEYS - data.keys()
    if missing:
        return False, None, f"Missing required keys: {missing}"

    if data["intent"] not in _VALID_INTENTS:
        return False, None, f"Invalid intent: {data['intent']!r}"

    if data.get("entity_type") and data["entity_type"] not in _VALID_ENTITIES:
        return False, None, f"Invalid entity_type: {data['entity_type']!r}"

    confidence = data.get("confidence", 0.0)
    if not isinstance(confidence, (int, float)) or not (0.0 <= confidence <= 1.0):
        return False, None, f"Invalid confidence: {confidence!r}"

    return True, data, ""


def _ast_from_llm_json(data: Dict) -> Tuple[Optional[StatementAST], Dict[str, float]]:
    """
    Converts validated LLM JSON into a StatementAST-equivalent and event params.
    We build a minimal StatementAST compatible with the grammar's output.
    """
    # Map LLM JSON → grammar AST params using same semantics as nsl_grammar
    P = 0.50; V = 0.50; K = 0.85; O = 0.75

    intent = data["intent"]
    mods = data.get("modifiers") or {}

    if intent == "ACCELERATE":
        V += 0.28; P += 0.18; O -= 0.15
    elif intent == "DELAY":
        V -= 0.22; P -= 0.12; O += 0.12
    elif intent == "RESTRICT":
        K -= 0.25; O -= 0.22; P += 0.10
    elif intent == "ALLOCATE":
        K += 0.15; O -= 0.10
    elif intent == "QUERY":
        P += 0.05

    if mods.get("urgency"):
        u = float(mods["urgency"])
        V += u * 0.25; P += u * 0.15; O -= u * 0.10
    if mods.get("risk"):
        r = float(mods["risk"])
        P += r * 0.20; K -= r * 0.10
    if mods.get("scale"):
        s = float(mods["scale"])
        P += s * 0.15; V += s * 0.10; O -= s * 0.12
    if mods.get("capacity_delta"):
        cd = float(mods["capacity_delta"])
        K += cd; O += cd * 0.5

    params = {
        "pressure":     max(0.05, min(2.5, round(P, 4))),
        "velocity":     max(0.05, min(2.5, round(V, 4))),
        "capacity":     max(0.05, min(2.0, round(K, 4))),
        "option_space": max(0.00, min(1.00, round(O, 4))),
    }
    return None, params  # AST is None — only params needed downstream


# ─── INFERENCE ENGINE ─────────────────────────────────────────────────────────

class NSLInferenceEngine:
    """
    Dual-backend NSL inference engine.

    Usage:
        engine = NSLInferenceEngine(config)
        result = engine.infer("Accelerate the partner_b-H2 R&D project urgently")
        # result = {
        #     "backend": "llm" | "grammar",
        #     "intent": "ACCELERATE",
        #     "entity_type": "RND_PROJECT",
        #     "entity_identifier": "partner_b-H2",
        #     "params": {"pressure": 0.78, "velocity": 0.89, "capacity": 0.85, "option_space": 0.52},
        #     "confidence": 0.94,
        #     "latency_ms": 312,
        #     "error": null
        # }
    """

    def __init__(self, config: NSLInferenceConfig):
        self.cfg = config
        logger.info(
            f"[NSL] Engine ready. mode={'grammar-only' if config.grammar_only_mode else 'llm+grammar'} "
            f"model={config.model_name}"
        )

    def infer(self, text: str) -> NSLInferenceResult:
        """Single-call entry point. Always returns a structured result — never raises."""
        t0 = time.monotonic()

        if self.cfg.grammar_only_mode:
            return self._grammar_path(text, t0, forced=True)

        # Try LLM first
        llm_result = self._llm_path(text, t0)
        if llm_result is not None:
            return llm_result

        # Fall back to grammar
        logger.debug("[NSL] LLM path failed — falling back to grammar")
        return self._grammar_path(text, t0, forced=False)

    # ── LLM path ──────────────────────────────────────────────────────────────

    def _llm_path(self, text: str, t0: float) -> Optional[NSLInferenceResult]:
        """
        Calls Ollama /api/generate, validates output, returns result or None.
        """
        url = f"{self.cfg.ollama_base_url}/api/generate"
        body = json.dumps({
            "model": self.cfg.model_name,
            "prompt": text,
            "system": _SYSTEM_PROMPT,
            "stream": False,
            "options": {
                "num_predict": self.cfg.max_tokens,
                "temperature": 0.0,   # Deterministic — no creativity
                "top_p": 1.0,
            }
        }).encode()

        try:
            req = urllib.request.Request(
                url, data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.cfg.llm_timeout_sec) as resp:
                raw_resp = json.loads(resp.read())
            llm_output = raw_resp.get("response", "")
        except urllib.error.URLError as e:
            logger.debug(f"[NSL] Ollama unreachable: {e}")
            return None
        except Exception as e:
            logger.warning(f"[NSL] LLM call error: {e}")
            return None

        is_valid, data, err = _validate_llm_json(llm_output)
        if not is_valid:
            logger.debug(f"[NSL] LLM JSON invalid: {err}")
            return None

        confidence = float(data.get("confidence", 0.0))
        if confidence < self.cfg.confidence_threshold:
            logger.debug(f"[NSL] LLM confidence {confidence:.2f} < threshold {self.cfg.confidence_threshold}")
            return None

        _, params = _ast_from_llm_json(data)
        return NSLInferenceResult(
            inference_backend="llm",
            intent=data["intent"],
            entity_type=data.get("entity_type"),
            entity_identifier=data.get("entity_identifier"),
            compiled_params=params,
            confidence=confidence,
            latency_ms=int((time.monotonic() - t0) * 1000),
            error=None,
            final_ast=data,
        )

    # ── Grammar path ──────────────────────────────────────────────────────────

    def _grammar_path(self, text: str, t0: float, forced: bool) -> NSLInferenceResult:
        """
        Runs the LALR(1) grammar parser. Returns result dict — never raises.
        """
        ast, params, err = compile_nsl_safe(text)

        if ast is None:
            return NSLInferenceResult(
                inference_backend="grammar",
                intent=None,
                entity_type=None,
                entity_identifier=None,
                compiled_params={},
                confidence=0.0,
                latency_ms=int((time.monotonic() - t0) * 1000),
                error=err or "Grammar parse failed",
                final_ast=None,
            )

        confidence = 0.95 if forced else 0.80
        return NSLInferenceResult(
            inference_backend="grammar",
            intent=ast.intent.intent,
            entity_type=ast.entity.entity_type if ast.entity else None,
            entity_identifier=ast.entity.identifier if ast.entity else None,
            compiled_params=params,
            confidence=confidence,
            latency_ms=int((time.monotonic() - t0) * 1000),
            error=None,
            final_ast={
                "intent": ast.intent.intent,
                "entity_type": ast.entity.entity_type if ast.entity else None,
                "entity_identifier": ast.entity.identifier if ast.entity else None,
                "modifiers": {m.modifier_type: m.value for m in ast.modifiers},
                "context": " ".join(ast.context.reason_tokens) if ast.context else None,
                "confidence": confidence,
            },
        )


# ─── SINGLETON ────────────────────────────────────────────────────────────────

_engine: Optional[NSLInferenceEngine] = None


def get_inference_engine(config: Optional[NSLInferenceConfig] = None) -> NSLInferenceEngine:
    global _engine
    if _engine is None:
        _engine = NSLInferenceEngine(config or NSLInferenceConfig(grammar_only_mode=True))
    return _engine
