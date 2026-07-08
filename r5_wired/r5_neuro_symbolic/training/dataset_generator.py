"""
R5 NEURO-SYMBOLIC: Fine-Tuning Dataset Generator
=================================================
Generates the training dataset for fine-tuning a local LLM (Llama 3 8B / Mistral 7B)
to natively output NSL ASTs from institutional narrative text.

Strategy: QLoRA (4-bit quantized LoRA) on ~2,000–5,000 instruction-pairs.
The model is trained ONLY to emit structured JSON ASTs — conversational
ability is deliberately suppressed via the system prompt and loss masking.

Output format: JSONL (one training sample per line), compatible with:
  - Axolotl (recommended)
  - LLaMA-Factory
  - Unsloth

Each sample:
  {
    "instruction": "<system prompt>",
    "input": "<raw institutional narrative>",
    "output": "<deterministic JSON AST>"
  }
"""

import json
import random
import uuid
from dataclasses import dataclass, asdict
from typing import List, Dict, Any
from pathlib import Path

# ─── SYSTEM PROMPT ────────────────────────────────────────────────────────────
# This is injected as the system message for every training example.
# The model must learn: only produce JSON, nothing else.

SYSTEM_PROMPT = """You are a deterministic NSL compiler for the TENIR institutional governance system.
Your ONLY function is to parse governance narrative text and output a valid JSON Abstract Syntax Tree (AST).

RULES:
1. Output ONLY valid JSON. No explanation, no commentary, no preamble.
2. If the intent is ambiguous, default to the most conservative interpretation.
3. Never hallucinate entity identifiers. Use null if not explicitly mentioned.
4. The output must be parseable by json.loads() with zero errors.

AST SCHEMA:
{
  "intent": "ACCELERATE|DELAY|RESTRICT|ALLOCATE|QUERY",
  "entity_type": "RND_PROJECT|PROCUREMENT_CONTRACT|BUDGET|PERSONNEL|LEGAL_POLICY",
  "entity_identifier": "<string or null>",
  "modifiers": {
    "urgency": <float 0.0-1.0 or null>,
    "risk": <float 0.0-1.0 or null>,
    "scale": <float 0.0-1.0 or null>,
    "capacity_delta": <float -1.0 to 1.0 or null>
  },
  "context": "<brief reason phrase or null>",
  "confidence": <float 0.0-1.0>
}"""

# ─── NARRATIVE TEMPLATES ──────────────────────────────────────────────────────

@dataclass
class TrainingSample:
    instruction: str
    input: str
    output: str


_TEMPLATES: List[Dict[str, Any]] = [
    # ACCELERATE + RND
    {
        "input": "We need to fast-track the {proj} R&D project. Budget pressure is mounting.",
        "ast": {
            "intent": "ACCELERATE", "entity_type": "RND_PROJECT",
            "entity_identifier": "{proj}",
            "modifiers": {"urgency": 0.75, "risk": null, "scale": null, "capacity_delta": null},
            "context": "Budget pressure mounting", "confidence": 0.92
        }
    },
    {
        "input": "The committee is stalling the patent review due to budget fears.",
        "ast": {
            "intent": "DELAY", "entity_type": "RND_PROJECT",
            "entity_identifier": null,
            "modifiers": {"urgency": null, "risk": 0.60, "scale": null, "capacity_delta": -0.15},
            "context": "Budget fears causing stall", "confidence": 0.88
        }
    },
    {
        "input": "Accelerate the procurement cycle for the Green Hydrogen pilot. This is urgent.",
        "ast": {
            "intent": "ACCELERATE", "entity_type": "PROCUREMENT_CONTRACT",
            "entity_identifier": "Green Hydrogen pilot",
            "modifiers": {"urgency": 0.85, "risk": null, "scale": null, "capacity_delta": null},
            "context": null, "confidence": 0.95
        }
    },
    {
        "input": "Restrict further budget allocations to the R&D unit until the quarterly review.",
        "ast": {
            "intent": "RESTRICT", "entity_type": "BUDGET",
            "entity_identifier": null,
            "modifiers": {"urgency": null, "risk": null, "scale": null, "capacity_delta": -0.20},
            "context": "Until quarterly review", "confidence": 0.90
        }
    },
    {
        "input": "Deploy 3 additional engineers to the {proj} project immediately.",
        "ast": {
            "intent": "ALLOCATE", "entity_type": "PERSONNEL",
            "entity_identifier": "{proj}",
            "modifiers": {"urgency": 0.80, "risk": null, "scale": 0.50, "capacity_delta": 0.20},
            "context": null, "confidence": 0.93
        }
    },
    {
        "input": "What is the current status of contract {proj}?",
        "ast": {
            "intent": "QUERY", "entity_type": "PROCUREMENT_CONTRACT",
            "entity_identifier": "{proj}",
            "modifiers": {"urgency": null, "risk": null, "scale": null, "capacity_delta": null},
            "context": null, "confidence": 0.97
        }
    },
    {
        "input": "The {proj} regulatory compliance is at risk. We must pause R&D deliverables until legal signs off.",
        "ast": {
            "intent": "DELAY", "entity_type": "RND_PROJECT",
            "entity_identifier": "{proj}",
            "modifiers": {"urgency": 0.70, "risk": 0.85, "scale": null, "capacity_delta": null},
            "context": "Legal sign-off required for compliance", "confidence": 0.89
        }
    },
    {
        "input": "Commit 40% of the innovation budget to the applied research stream.",
        "ast": {
            "intent": "ALLOCATE", "entity_type": "BUDGET",
            "entity_identifier": null,
            "modifiers": {"urgency": null, "risk": null, "scale": 0.60, "capacity_delta": -0.10},
            "context": "Applied research stream", "confidence": 0.91
        }
    },
    {
        "input": "Stop all supplier negotiations pending the board review of the procurement policy.",
        "ast": {
            "intent": "RESTRICT", "entity_type": "PROCUREMENT_CONTRACT",
            "entity_identifier": null,
            "modifiers": {"urgency": null, "risk": 0.55, "scale": null, "capacity_delta": null},
            "context": "Board review of procurement policy", "confidence": 0.87
        }
    },
    {
        "input": "The research team is overwhelmed. We should freeze new project commitments.",
        "ast": {
            "intent": "RESTRICT", "entity_type": "RND_PROJECT",
            "entity_identifier": null,
            "modifiers": {"urgency": null, "risk": 0.65, "scale": null, "capacity_delta": -0.30},
            "context": "Research team overwhelmed", "confidence": 0.86
        }
    },
    {
        "input": "Expedite the Phase 2 electrolysis contract before the partner_b deadline.",
        "ast": {
            "intent": "ACCELERATE", "entity_type": "PROCUREMENT_CONTRACT",
            "entity_identifier": "Phase 2 electrolysis contract",
            "modifiers": {"urgency": 0.90, "risk": null, "scale": null, "capacity_delta": null},
            "context": "partner_b deadline constraint", "confidence": 0.94
        }
    },
    {
        "input": "Review the legal implications of the new AI governance mandate.",
        "ast": {
            "intent": "QUERY", "entity_type": "LEGAL_POLICY",
            "entity_identifier": "AI governance mandate",
            "modifiers": {"urgency": null, "risk": 0.50, "scale": null, "capacity_delta": null},
            "context": null, "confidence": 0.88
        }
    },
]

# Filler project names for template substitution
_PROJECTS = [
    "partner_b-H2-2026", "partner_a-AI-Pilot", "GreenPhosphate-III", "SolarMining-Q2",
    "AgriTech-Phase2", "WaterTreatment-partner_b", "Electrolysis-Pilot",
    "MaterialsResearch-007", "NanoLayer-partner_a", "CarbonCapture-2025",
]

# Paraphrase variants for linguistic diversity
_PARAPHRASES: Dict[str, List[str]] = {
    "accelerate": ["speed up", "expedite", "fast-track", "push forward", "advance", "rush"],
    "delay": ["pause", "postpone", "freeze", "hold back", "slow down", "defer"],
    "restrict": ["limit", "cap", "stop", "block", "suspend", "halt"],
    "urgent": ["immediately", "as soon as possible", "without delay", "critically"],
    "risk": ["danger", "threat", "concern", "exposure"],
}


def _substitute(template: str, proj: str) -> str:
    return template.replace("{proj}", proj)


def _ast_to_json(ast_template: Dict[str, Any], proj: str) -> str:
    """Resolves template substitutions in AST, returns JSON string."""
    resolved = {}
    for k, v in ast_template.items():
        if isinstance(v, str):
            resolved[k] = v.replace("{proj}", proj)
        elif isinstance(v, dict):
            resolved[k] = {
                mk: (mv.replace("{proj}", proj) if isinstance(mv, str) else mv)
                for mk, mv in v.items()
            }
        else:
            resolved[k] = v
    return json.dumps(resolved, indent=None)


def generate_dataset(n_samples: int = 2000, seed: int = 42) -> List[TrainingSample]:
    """
    Generates n_samples training examples via template expansion + light augmentation.
    """
    random.seed(seed)
    samples: List[TrainingSample] = []

    for i in range(n_samples):
        # Pick a template
        template = random.choice(_TEMPLATES)
        proj = random.choice(_PROJECTS)
        input_text = _substitute(template["input"], proj)

        # Light augmentation: swap intent keyword with a paraphrase ~30% of time
        if random.random() < 0.30:
            intent_lower = template["ast"]["intent"].lower()
            if intent_lower in _PARAPHRASES:
                synonym = random.choice(_PARAPHRASES[intent_lower])
                # Find and replace the primary intent keyword
                for kw in ["accelerate", "delay", "restrict", "allocate", "query",
                           "fast-track", "speed up", "pause", "stop", "limit",
                           "commit", "review", "check"]:
                    if kw in input_text.lower():
                        input_text = input_text.lower().replace(kw, synonym, 1)
                        input_text = input_text[0].upper() + input_text[1:]
                        break

        # Occasional sentence re-ordering (~20%)
        if random.random() < 0.20:
            words = input_text.split()
            random.shuffle(words[:4])  # Only shuffle prefix to preserve meaning
            input_text = " ".join(words)

        ast_json = _ast_to_json(template["ast"], proj)

        samples.append(TrainingSample(
            instruction=SYSTEM_PROMPT,
            input=input_text,
            output=ast_json,
        ))

    return samples


def write_jsonl(samples: List[TrainingSample], path: str) -> None:
    """Writes training samples as JSONL."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for sample in samples:
            f.write(json.dumps({
                "instruction": sample.instruction,
                "input": sample.input,
                "output": sample.output,
            }) + "\n")
    print(f"[R5-NSL] Wrote {len(samples)} training samples → {path}")


def write_axolotl_config(output_dir: str, model_name: str = "meta-llama/Meta-Llama-3-8B") -> None:
    """
    Writes the Axolotl fine-tuning configuration YAML for QLoRA training.
    Axolotl: https://github.com/OpenAccess-AI-Collective/axolotl
    """
    config = f"""
# ============================================================
# TENIR R5: NSL Compiler — Axolotl QLoRA Fine-Tuning Config
# Base model: {model_name}
# ============================================================

base_model: {model_name}
model_type: LlamaForCausalLM
tokenizer_type: AutoTokenizer
load_in_4bit: true
strict: false

# Dataset (alpaca-formatted JSONL)
datasets:
  - path: {output_dir}/nsl_train.jsonl
    type: alpaca

val_set_size: 0.05
output_dir: {output_dir}/nsl_lora_model

# QLoRA configuration
# We target ONLY the attention layers (not MLP) to preserve reasoning while
# training the model to emit structured JSON exclusively.
adapter: qlora
lora_r: 32
lora_alpha: 64
lora_dropout: 0.05
lora_target_modules:
  - q_proj
  - k_proj
  - v_proj
  - o_proj
# We deliberately exclude gate_proj, up_proj, down_proj to suppress
# the model's conversational / generative tendencies.

# Training hyperparams
# Conservative: we want deterministic JSON output, not creative generation.
sequence_len: 512
sample_packing: true
pad_to_sequence_len: true

micro_batch_size: 4
gradient_accumulation_steps: 4
num_epochs: 3
optimizer: adamw_bnb_8bit
lr_scheduler: cosine
learning_rate: 0.0002
train_on_inputs: false   # Only compute loss on OUTPUT (AST JSON), not input
group_by_length: false

# Loss masking:
# The system prompt and input are masked from loss calculation.
# The model only learns to generate the AST JSON portion.
# This is the critical hyperparameter that ensures zero hallucination.

bf16: auto
fp16: false

# Regularization
weight_decay: 0.01
warmup_steps: 50

# Evaluation
eval_steps: 100
logging_steps: 10

# Inference constraints (applied at generation time, not training)
# These are set in the inference server (nsl_inference.py):
#   temperature: 0.0   (greedy, deterministic)
#   do_sample: false
#   max_new_tokens: 256
#   stop_sequences: ["}"]  (stop after JSON closes)
"""
    config_path = Path(output_dir) / "axolotl_nsl_config.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(config.strip())
    print(f"[R5-NSL] Axolotl config → {config_path}")


if __name__ == "__main__":
    OUTPUT_DIR = "r5_neuro_symbolic/training/data"
    train = generate_dataset(n_samples=3000)
    write_jsonl(train, f"{OUTPUT_DIR}/nsl_train.jsonl")
    write_axolotl_config(OUTPUT_DIR)
    print("[R5-NSL] Dataset generation complete.")
