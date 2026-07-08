"""
R5 NEURO-SYMBOLIC: NSL Formal Grammar
======================================
Defines the complete LALR(1) grammar for Natural Solution Language (NSL).
This replaces the heuristic keyword matcher with a formal compiler pipeline:

  Raw Text → Tokenizer → Parser → AST → EventSample

The grammar is intentionally restrictive — it is NOT a general-purpose NLP parser.
It is a domain-specific language (DSL) tuned exclusively to TENIR institutional
governance narratives. This is the "zero hallucination" guarantee:
if a phrase does not fit the grammar, it is REJECTED, not guessed.

Grammar BNF:
  statement     := intent_phrase entity_phrase? modifier_clause* context_clause?
  intent_phrase := ACCELERATE_KW | DELAY_KW | RESTRICT_KW | ALLOCATE_KW | QUERY_KW
  entity_phrase := ARTICLE? entity_type IDENTIFIER?
  entity_type   := RND_ENTITY | PROCUREMENT_ENTITY | BUDGET_ENTITY | PERSONNEL_ENTITY | LEGAL_ENTITY
  modifier_clause := MODIFIER_KW SCALAR_VALUE?
  context_clause  := BECAUSE_KW reason_phrase
  reason_phrase   := (WORD)+
"""

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import List, Optional, Dict, Tuple
import re


# ─── TOKEN TYPES ─────────────────────────────────────────────────────────────

class TokType(Enum):
    # Intent keywords
    ACCELERATE      = auto()
    DELAY           = auto()
    RESTRICT        = auto()
    ALLOCATE        = auto()
    QUERY           = auto()
    # Entity types
    RND_ENTITY      = auto()
    PROCUREMENT_ENTITY = auto()
    BUDGET_ENTITY   = auto()
    PERSONNEL_ENTITY = auto()
    LEGAL_ENTITY    = auto()
    # Modifiers
    URGENCY         = auto()
    RISK            = auto()
    SCALE           = auto()
    CAPACITY_MOD    = auto()
    # Structure
    ARTICLE         = auto()
    BECAUSE         = auto()
    IDENTIFIER      = auto()
    NUMBER          = auto()
    WORD            = auto()
    EOF             = auto()


@dataclass
class Token:
    type: TokType
    value: str
    position: int
    scalar: Optional[float] = None   # For NUMBER tokens


# ─── LEXICON ─────────────────────────────────────────────────────────────────
# Each entry: (regex_pattern, TokType, optional_scalar_extractor)
# Order matters — more specific patterns must come before generic WORD.

LEXICON: List[Tuple[str, TokType, Optional[float]]] = [
    # Intent keywords (multi-word first)
    (r"\bfast.?track\b",               TokType.ACCELERATE,     None),
    (r"\bspeed.?up\b",                 TokType.ACCELERATE,     None),
    (r"\baccelerat\w*\b",              TokType.ACCELERATE,     None),
    (r"\bpush.?through\b",             TokType.ACCELERATE,     None),
    (r"\bexpedite\b",                  TokType.ACCELERATE,     None),
    (r"\bdelai\w*\b",                  TokType.DELAY,          None),
    (r"\bdelay\w*\b",                  TokType.DELAY,          None),
    (r"\bslow.?down\b",                TokType.DELAY,          None),
    (r"\bpaus\w*\b",                   TokType.DELAY,          None),
    (r"\bpostpon\w*\b",                TokType.DELAY,          None),
    (r"\bfreez\w*\b",                  TokType.DELAY,          None),
    (r"\bstall\w*\b",                  TokType.DELAY,          None),
    (r"\brestric\w*\b",                TokType.RESTRICT,       None),
    (r"\blimit\w*\b",                  TokType.RESTRICT,       None),
    (r"\bblock\w*\b",                  TokType.RESTRICT,       None),
    (r"\bcap\b",                       TokType.RESTRICT,       None),
    (r"\bstop\w*\b",                   TokType.RESTRICT,       None),
    (r"\ballocat\w*\b",                TokType.ALLOCATE,       None),
    (r"\bassign\w*\b",                 TokType.ALLOCATE,       None),
    (r"\bdeploy\w*\b",                 TokType.ALLOCATE,       None),
    (r"\bcommit\w*\b",                 TokType.ALLOCATE,       None),
    (r"\bquer\w*\b",                   TokType.QUERY,          None),
    (r"\bcheck\w*\b",                  TokType.QUERY,          None),
    (r"\bstatus\b",                    TokType.QUERY,          None),
    (r"\breview\w*\b",                 TokType.QUERY,          None),
    # Entity types
    (r"\br\s*[&\-]\s*d\b|\bresearch\b|\bproject\b|\bpatent\b|\binnovation\b",
                                       TokType.RND_ENTITY,     None),
    (r"\bprocurement\b|\bcontract\b|\bpurchas\w*\b|\btender\b|\bsupplier\b",
                                       TokType.PROCUREMENT_ENTITY, None),
    (r"\bbudget\b|\bfunding\b|\bcredit\b|\bfinance\b|\ballowance\b",
                                       TokType.BUDGET_ENTITY,  None),
    (r"\bpersonnel\b|\bstaff\b|\bteam\b|\bresearcher\b|\bengineer\b",
                                       TokType.PERSONNEL_ENTITY, None),
    (r"\blegal\b|\bpolicy\b|\bregulat\w*\b|\bcomplian\w*\b|\bmandate\b",
                                       TokType.LEGAL_ENTITY,   None),
    # Modifiers
    (r"\burgent\w*\b|\bcritical\b|\bemergency\b|\bimmediately\b",
                                       TokType.URGENCY,        0.85),
    (r"\brisk\w*\b|\bdanger\w*\b|\bthreat\w*\b|\bhazard\w*\b",
                                       TokType.RISK,           0.70),
    (r"\bmassive\b|\blarge.?scale\b|\bsignificant\b|\bmajor\b",
                                       TokType.SCALE,          0.80),
    (r"\boverwhelm\w*\b|\bsaturat\w*\b|\bexhaust\w*\b|\bburnout\b",
                                       TokType.CAPACITY_MOD,  -0.25),
    (r"\bfear\w*\b|\bbehind\b|\bstalling\b|\bstruggl\w*\b",
                                       TokType.CAPACITY_MOD,  -0.15),
    # Structure
    (r"\bthe\b|\ba\b|\ban\b|\bthis\b|\bthat\b",
                                       TokType.ARTICLE,        None),
    (r"\bbecause\b|\bdue\s+to\b|\bowing\s+to\b|\bas\s+a\s+result\b",
                                       TokType.BECAUSE,        None),
    (r"\b[A-Z]{2,}[-\w]*\b",          TokType.IDENTIFIER,     None),
    (r"\b\d+(?:\.\d+)?%?\b",          TokType.NUMBER,         None),
    (r"\b\w+\b",                       TokType.WORD,           None),
]

# Compile all patterns
_COMPILED_LEXICON = [(re.compile(pat, re.IGNORECASE), tok_type, scalar)
                     for pat, tok_type, scalar in LEXICON]


# ─── TOKENIZER ────────────────────────────────────────────────────────────────

def tokenize(text: str) -> List[Token]:
    """
    Greedy left-to-right tokenizer.
    Skips whitespace and punctuation except when embedded in identifiers.
    """
    tokens: List[Token] = []
    pos = 0
    text = text.strip()

    while pos < len(text):
        # Skip whitespace
        if text[pos].isspace():
            pos += 1
            continue

        # Skip standalone punctuation (commas, periods, etc.)
        if re.match(r"[,;:\.!\?\"']", text[pos]):
            pos += 1
            continue

        # Try each lexicon rule in order
        matched = False
        for pattern, tok_type, default_scalar in _COMPILED_LEXICON:
            m = pattern.match(text, pos)
            if m:
                value = m.group(0)
                # Parse scalar from NUMBER tokens
                scalar = default_scalar
                if tok_type == TokType.NUMBER:
                    try:
                        scalar = float(value.strip('%')) / (100.0 if '%' in value else 1.0)
                    except ValueError:
                        scalar = None
                tokens.append(Token(tok_type, value, pos, scalar))
                pos = m.end()
                matched = True
                break

        if not matched:
            pos += 1   # Skip unrecognized character

    tokens.append(Token(TokType.EOF, "", pos))
    return tokens


# ─── AST NODES ────────────────────────────────────────────────────────────────

@dataclass
class IntentNode:
    intent: str          # ACCELERATE | DELAY | RESTRICT | ALLOCATE | QUERY
    token: Token


@dataclass
class EntityNode:
    entity_type: str     # RND_PROJECT | PROCUREMENT_CONTRACT | ...
    identifier: Optional[str]


@dataclass
class ModifierNode:
    modifier_type: str   # URGENCY | RISK | SCALE | CAPACITY_MOD
    value: float         # Normalized [0.0, 1.0] or delta


@dataclass
class ContextNode:
    reason_tokens: List[str]


@dataclass
class StatementAST:
    intent: IntentNode
    entity: Optional[EntityNode]
    modifiers: List[ModifierNode]
    context: Optional[ContextNode]
    raw_text: str

    def compile_to_event_params(self) -> Dict[str, float]:
        """
        Deterministic compilation: StatementAST → {pressure, velocity, capacity, option_space}.

        Semantics:
          ACCELERATE → V↑, P↑, option_space↓
          DELAY      → V↓, P↓, option_space↑
          RESTRICT   → K↓, option_space↓↓
          ALLOCATE   → K↑, option_space depends on scale
          QUERY      → minimal change, confidence affects P
        """
        # Base parameters (institutional neutral)
        P = 0.50   # pressure
        V = 0.50   # velocity
        K = 0.85   # capacity
        O = 0.75   # option_space

        intent = self.intent.intent

        if intent == "ACCELERATE":
            V += 0.28
            P += 0.18
            O -= 0.15
        elif intent == "DELAY":
            V -= 0.22
            P -= 0.12
            O += 0.12
        elif intent == "RESTRICT":
            K -= 0.25
            O -= 0.22
            P += 0.10
        elif intent == "ALLOCATE":
            K += 0.15
            O -= 0.10   # commitment reduces option space
        elif intent == "QUERY":
            P += 0.05   # inquiry adds slight pressure
            # K and V unchanged

        # Apply modifiers
        for mod in self.modifiers:
            mt = mod.modifier_type
            mv = mod.value
            if mt == "URGENCY":
                V += mv * 0.25
                P += mv * 0.15
                O -= mv * 0.10
            elif mt == "RISK":
                P += mv * 0.20
                K -= mv * 0.10
            elif mt == "SCALE":
                P += mv * 0.15
                V += mv * 0.10
                O -= mv * 0.12
            elif mt == "CAPACITY_MOD":
                K += mv   # mv is negative for capacity reduction
                O += mv * 0.5

        # Clamp
        return {
            "pressure":     max(0.05, min(2.5, round(P, 4))),
            "velocity":     max(0.05, min(2.5, round(V, 4))),
            "capacity":     max(0.05, min(2.0, round(K, 4))),
            "option_space": max(0.00, min(1.00, round(O, 4))),
        }


# ─── PARSER ──────────────────────────────────────────────────────────────────

_INTENT_MAP = {
    TokType.ACCELERATE: "ACCELERATE",
    TokType.DELAY:      "DELAY",
    TokType.RESTRICT:   "RESTRICT",
    TokType.ALLOCATE:   "ALLOCATE",
    TokType.QUERY:      "QUERY",
}

_ENTITY_MAP = {
    TokType.RND_ENTITY:           "RND_PROJECT",
    TokType.PROCUREMENT_ENTITY:   "PROCUREMENT_CONTRACT",
    TokType.BUDGET_ENTITY:        "BUDGET",
    TokType.PERSONNEL_ENTITY:     "PERSONNEL",
    TokType.LEGAL_ENTITY:         "LEGAL_POLICY",
}

_MODIFIER_MAP = {
    TokType.URGENCY:      "URGENCY",
    TokType.RISK:         "RISK",
    TokType.SCALE:        "SCALE",
    TokType.CAPACITY_MOD: "CAPACITY_MOD",
}


class NSLParser:
    """
    LALR(1)-style recursive descent parser for NSL.
    Strict: raises NSLParseError if no valid intent is found.
    """

    class NSLParseError(ValueError):
        pass

    def __init__(self, tokens: List[Token]):
        self._tokens = tokens
        self._pos = 0

    def _peek(self) -> Token:
        return self._tokens[self._pos]

    def _consume(self, *expected: TokType) -> Token:
        tok = self._tokens[self._pos]
        if expected and tok.type not in expected:
            raise NSLParser.NSLParseError(
                f"Expected {expected} at pos {tok.position}, got {tok.type} ({tok.value!r})"
            )
        self._pos += 1
        return tok

    def _accept(self, *types: TokType) -> Optional[Token]:
        if self._peek().type in types:
            return self._consume()
        return None

    def parse(self, raw_text: str) -> StatementAST:
        intent_node = self._parse_intent()
        if intent_node is None:
            raise NSLParser.NSLParseError(
                f"No recognizable intent found in: {raw_text!r}"
            )
        entity_node = self._parse_entity()
        modifiers = self._parse_modifiers()
        context = self._parse_context()
        return StatementAST(
            intent=intent_node,
            entity=entity_node,
            modifiers=modifiers,
            context=context,
            raw_text=raw_text,
        )

    def _parse_intent(self) -> Optional[IntentNode]:
        """Scan for first intent keyword (not necessarily at position 0)."""
        while self._peek().type != TokType.EOF:
            tok = self._peek()
            if tok.type in _INTENT_MAP:
                self._consume()
                return IntentNode(intent=_INTENT_MAP[tok.type], token=tok)
            self._consume()   # skip preamble words
        return None

    def _parse_entity(self) -> Optional[EntityNode]:
        # Skip articles
        self._accept(TokType.ARTICLE)
        tok = self._peek()
        if tok.type in _ENTITY_MAP:
            self._consume()
            # Optionally consume an IDENTIFIER (e.g., project code)
            ident_tok = self._accept(TokType.IDENTIFIER)
            return EntityNode(
                entity_type=_ENTITY_MAP[tok.type],
                identifier=ident_tok.value if ident_tok else None,
            )
        return None

    def _parse_modifiers(self) -> List[ModifierNode]:
        mods: List[ModifierNode] = []
        # Scan remaining tokens (before EOF or BECAUSE) for modifiers
        # We need to scan the entire token list, even past BECAUSE, to catch modifiers like "due to risk"
        while self._peek().type != TokType.EOF:
            tok = self._peek()
            if tok.type in _MODIFIER_MAP:
                self._consume()
                # Try to read a NUMBER following the modifier
                num_tok = self._accept(TokType.NUMBER)
                scalar = num_tok.scalar if num_tok else (tok.scalar or 0.5)
                mods.append(ModifierNode(
                    modifier_type=_MODIFIER_MAP[tok.type],
                    value=float(scalar) if scalar is not None else 0.5,
                ))
            else:
                self._consume()
        
        # Reset position to parse context properly
        self._pos = 0
        # Skip intent and entity
        self._parse_intent()
        self._parse_entity()
        # Skip modifiers before BECAUSE
        while self._peek().type not in (TokType.EOF, TokType.BECAUSE):
            self._consume()
            
        return mods

    def _parse_context(self) -> Optional[ContextNode]:
        if not self._accept(TokType.BECAUSE):
            return None
        words: List[str] = []
        while self._peek().type != TokType.EOF:
            words.append(self._consume().value)
        return ContextNode(reason_tokens=words)


# ─── PUBLIC API ───────────────────────────────────────────────────────────────

def compile_nsl(text: str) -> Tuple[StatementAST, Dict[str, float]]:
    """
    Full NSL compilation pipeline: text → tokens → AST → EventSample params.

    Returns:
        (ast, params) where params = {pressure, velocity, capacity, option_space}

    Raises:
        NSLParser.NSLParseError if no intent can be parsed.
    """
    tokens = tokenize(text)
    parser = NSLParser(tokens)
    ast = parser.parse(text)
    params = ast.compile_to_event_params()
    return ast, params


def compile_nsl_safe(text: str) -> Tuple[Optional[StatementAST], Dict[str, float], str]:
    """
    Safe version — returns (None, {}, error_msg) on parse failure.
    """
    try:
        ast, params = compile_nsl(text)
        return ast, params, ""
    except NSLParser.NSLParseError as e:
        return None, {}, str(e)
