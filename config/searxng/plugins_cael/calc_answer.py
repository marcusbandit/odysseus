# SPDX-License-Identifier: AGPL-3.0-or-later
"""Google-style server-side calculator instant answers.

Replaces the stock client-JS-only calculator so results also appear in
``format=json``. Covers everyday math ("2+2*3", "sqrt(2)*100", "2^10"),
percent forms ("15% of 340", "340 + 15%") and base conversions
("0xff in decimal", "255 in hex").

Everything is evaluated server-side through ``ast.parse`` plus an explicit
node whitelist (no ``eval``, no names beyond a fixed function/constant table),
with guards against huge powers and factorials. Any failure means "no
answer", never a broken search.

Bind mounted into the image next to the stock plugins (see docker-compose.yml)
and registered in settings.yml as ``searx.plugins.cael.calc_answer.SXNGPlugin``.
Rendered by the custom template ``answer/cael/calc.html``.
"""

from __future__ import annotations

import ast
import math
import re
import typing

from decimal import Decimal
from urllib.parse import quote_plus

from flask_babel import format_decimal

from searx.plugins import Plugin, PluginInfo
from searx.result_types import EngineResults
from searx.result_types.answer import BaseAnswer

if typing.TYPE_CHECKING:
    from searx.search import SearchWithPlugins
    from searx.extended_types import SXNG_Request
    from searx.plugins import PluginCfg

MAX_QUERY_LEN = 120
MAX_POW_EXP = 4096  # abs(exponent) above this is refused outright
MAX_POW_LOG10 = 308  # power results beyond ~1e308 are refused
MAX_FACTORIAL = 500
MAX_BASECONV_BITS = 128  # base conversions capped to sane chip lengths
SIG_DIGITS = 10
SCI_UPPER = 10**15  # scientific notation at/above this magnitude
SCI_LOWER = 1e-6  # and below this (nonzero) magnitude

ANSWER_URL = "https://www.wolframalpha.com/input?i={query}"
TEMPLATE = "answer/cael/calc.html"

# never treat "2024-2025" style year ranges as subtraction
YEAR_RANGE_RE = re.compile(r"^\s*\d{4}\s*-\s*\d{4}\s*$")
# a (signed) bare number is not a calculation
BARE_NUMBER_RE = re.compile(r"[+\-]?\s*[\d.,\s]+")
# charset gate for math expressions (after unicode normalization)
CHARSET_RE = re.compile(r"[0-9a-z+\-*/%^().,\s]+")
LETTER_RUN_RE = re.compile(r"[a-z]+")
OPERATOR_RE = re.compile(r"[+\-*/%^]")
FUNC_CALL_RE = re.compile(r"[a-z]+\s*\(")
# digit-group commas ("1,000,000") and Danish decimal comma ("2,5")
GROUP_COMMA_RE = re.compile(r"(?<=\d),(?=\d{3}\b)")
DECIMAL_COMMA_RE = re.compile(r"\d,(?!\d{3}\b)\d")

# "15% of 340" and "340 + 15%" / "340 - 15%"
PERCENT_OF_RE = re.compile(r"^\s*([\d.,]+)\s*%\s+of\s+([\d.,]+)\s*$")
PERCENT_ADD_RE = re.compile(r"^\s*([\d.,]+)\s*([+\-])\s*([\d.,]+)\s*%\s*$")

# "0xff in decimal", "255 in hex", "0b1011 in octal", "0o777 in decimal"
BASECONV_RE = re.compile(
    r"^\s*(0x[0-9a-f]+|0b[01]+|0o[0-7]+|\d+)\s+in\s+"
    r"(decimal|dec|hexadecimal|hex|binary|bin|octal|oct)\s*$"
)
BASE_WORDS = {
    "decimal": "decimal",
    "dec": "decimal",
    "hexadecimal": "hex",
    "hex": "hex",
    "binary": "binary",
    "bin": "binary",
    "octal": "octal",
    "oct": "octal",
}


def _safe_pow(base: float, exponent: float):
    if isinstance(base, bool) or isinstance(exponent, bool):
        raise ValueError("boolean operand")
    if not isinstance(base, (int, float)) or not isinstance(exponent, (int, float)):
        raise ValueError("non-numeric operand")
    if abs(exponent) > MAX_POW_EXP:
        raise ValueError("exponent too large")
    if base != 0:
        # math.log10 accepts arbitrarily large ints, no float conversion
        if exponent * math.log10(abs(base)) > MAX_POW_LOG10:
            raise ValueError("result too large")
    result = base**exponent
    if isinstance(result, complex):
        raise ValueError("complex result")
    return result


def _safe_factorial(value):
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError("factorial of non-integer")
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("factorial of non-integer")
    if value < 0 or value > MAX_FACTORIAL:
        raise ValueError("factorial argument out of range")
    return math.factorial(value)


def _cbrt(value: float) -> float:
    # sign-aware cube root; works on every python version and negative input
    return math.copysign(abs(value) ** (1.0 / 3.0), value)


FUNCS: dict[str, typing.Callable] = {
    "sqrt": math.sqrt,
    "cbrt": _cbrt,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "asin": math.asin,
    "acos": math.acos,
    "atan": math.atan,
    "log": math.log10,  # calculator convention: log is base 10, ln is natural
    "log2": math.log2,
    "log10": math.log10,
    "ln": math.log,
    "exp": math.exp,
    "abs": abs,
    "round": round,
    "floor": math.floor,
    "ceil": math.ceil,
    "pow": _safe_pow,
    "factorial": _safe_factorial,
}
CONSTS: dict[str, float] = {"pi": math.pi, "e": math.e, "tau": math.tau}
ALLOWED_NAMES = frozenset(FUNCS) | frozenset(CONSTS)

BINOPS: dict[type, typing.Callable] = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.FloorDiv: lambda a, b: a // b,
    ast.Mod: lambda a, b: a % b,
    ast.Pow: _safe_pow,
}


def _eval_node(node: ast.AST):
    """Whitelist walker: evaluates the AST, raises ValueError on any node,
    operator, name or constant type outside the whitelist."""
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ValueError("non-numeric constant")
        return node.value
    if isinstance(node, ast.Name):
        if node.id in CONSTS:
            return CONSTS[node.id]
        raise ValueError("unknown name")
    if isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.UAdd):
            return +_eval_node(node.operand)
        if isinstance(node.op, ast.USub):
            return -_eval_node(node.operand)
        raise ValueError("unsupported unary operator")
    if isinstance(node, ast.BinOp):
        handler = BINOPS.get(type(node.op))
        if handler is None:
            raise ValueError("unsupported binary operator")
        return handler(_eval_node(node.left), _eval_node(node.right))
    if isinstance(node, ast.Call):
        if node.keywords or not isinstance(node.func, ast.Name):
            raise ValueError("unsupported call")
        func = FUNCS.get(node.func.id)
        if func is None:
            raise ValueError("unknown function")
        args = []
        for arg in node.args:
            if isinstance(arg, ast.Starred):
                raise ValueError("starred argument")
            args.append(_eval_node(arg))
        return func(*args)
    raise ValueError("unsupported syntax")


# ---- number formatting ----


def _fmt_int(value: int) -> str:
    try:
        return format_decimal(value, format="#,##0")
    except Exception:  # pylint: disable=broad-except
        return f"{value:,}"


def _fmt_frac(value: float, decimals: int) -> str:
    try:
        # "#" pattern digits round and trim trailing zeros
        return format_decimal(value, format="#,##0." + "#" * decimals)
    except Exception:  # pylint: disable=broad-except
        text = f"{value:,.{decimals}f}".rstrip("0").rstrip(".")
        return text or "0"


def _fmt_sci(value) -> str:
    if isinstance(value, int):
        raw = format(Decimal(value), f".{SIG_DIGITS - 1}e")
    else:
        raw = f"{value:.{SIG_DIGITS - 1}e}"
    mantissa, _, exponent = raw.lower().partition("e")
    mantissa = mantissa.rstrip("0").rstrip(".")
    return f"{mantissa}e{int(exponent):+d}"


def _fmt_number(value) -> str:
    """Thousands separators, up to SIG_DIGITS significant digits, trailing
    zeros trimmed, scientific notation beyond 1e15 or below 1e-6."""
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite result")
        if value.is_integer() and abs(value) < SCI_UPPER:
            value = int(value)
    if isinstance(value, int):
        if abs(value) >= SCI_UPPER:
            return _fmt_sci(value)
        return _fmt_int(value)
    magnitude = abs(value)
    if magnitude == 0.0:
        return "0"
    if magnitude >= SCI_UPPER or magnitude < SCI_LOWER:
        return _fmt_sci(value)
    decimals = SIG_DIGITS - 1 - math.floor(math.log10(magnitude))
    decimals = max(0, min(int(decimals), 25))
    rounded = round(value, decimals)
    if rounded.is_integer():
        if abs(rounded) >= SCI_UPPER:
            return _fmt_sci(rounded)
        return _fmt_int(int(rounded))
    return _fmt_frac(rounded, decimals)


# ---- pretty-printing the parsed expression ----

BIN_SYMBOL = {
    ast.Add: " + ",
    ast.Sub: " - ",
    ast.Mult: " × ",
    ast.Div: " / ",
    ast.FloorDiv: " // ",
    ast.Mod: " % ",
    ast.Pow: "^",
}
BIN_PREC = {
    ast.Add: 10,
    ast.Sub: 10,
    ast.Mult: 20,
    ast.Div: 20,
    ast.FloorDiv: 20,
    ast.Mod: 20,
    ast.Pow: 40,
}
UNARY_PREC = 30
ATOM_PREC = 100
NAME_PRETTY = {"pi": "π", "tau": "τ"}


def _node_prec(node: ast.AST) -> int:
    if isinstance(node, ast.BinOp):
        return BIN_PREC[type(node.op)]
    if isinstance(node, ast.UnaryOp):
        return UNARY_PREC
    return ATOM_PREC


def _pretty(node: ast.AST) -> str:
    if isinstance(node, ast.Expression):
        return _pretty(node.body)
    if isinstance(node, ast.Constant):
        return _fmt_number(node.value)
    if isinstance(node, ast.Name):
        return NAME_PRETTY.get(node.id, node.id)
    if isinstance(node, ast.UnaryOp):
        symbol = "-" if isinstance(node.op, ast.USub) else "+"
        inner = _pretty(node.operand)
        if _node_prec(node.operand) < UNARY_PREC:
            inner = f"({inner})"
        return symbol + inner
    if isinstance(node, ast.BinOp):
        op = type(node.op)
        prec = BIN_PREC[op]
        left = _pretty(node.left)
        right = _pretty(node.right)
        if op is ast.Pow:  # right-associative
            if _node_prec(node.left) <= prec:
                left = f"({left})"
            if _node_prec(node.right) < prec:
                right = f"({right})"
        else:  # left-associative
            if _node_prec(node.left) < prec:
                left = f"({left})"
            if _node_prec(node.right) <= prec:
                right = f"({right})"
        return left + BIN_SYMBOL[op] + right
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        args = ", ".join(_pretty(arg) for arg in node.args)
        return f"{node.func.id}({args})"
    raise ValueError("unsupported syntax")


# ---- solvers; each returns (expression, result) plus extra chips or None ----


def _parse_plain_number(text: str) -> float | None:
    text = GROUP_COMMA_RE.sub("", text)
    if text.count(",") == 1 and re.search(r"\d,\d", text):
        text = text.replace(",", ".")
    elif "," in text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _try_baseconv(query: str) -> tuple[str, str, list[str]] | None:
    match = BASECONV_RE.match(query)
    if not match:
        return None
    literal, base_word = match.groups()
    try:
        if literal.startswith("0x"):
            value = int(literal, 16)
        elif literal.startswith("0b"):
            value = int(literal, 2)
        elif literal.startswith("0o"):
            value = int(literal, 8)
        else:
            value = int(literal, 10)
    except ValueError:
        return None
    if value.bit_length() > MAX_BASECONV_BITS:
        return None
    target = BASE_WORDS[base_word]
    reprs = {
        "decimal": _fmt_int(value),
        "hex": hex(value),
        "binary": bin(value),
        "octal": oct(value),
    }
    expression = f"{literal} in {target}"
    extra = [
        f"DEC {reprs['decimal']}",
        f"HEX {reprs['hex']}",
        f"BIN {reprs['binary']}",
        f"OCT {reprs['octal']}",
    ]
    return expression, reprs[target], extra


def _try_percent(query: str) -> tuple[str, str, list[str]] | None:
    match = PERCENT_OF_RE.match(query)
    if match:
        pct = _parse_plain_number(match.group(1))
        base = _parse_plain_number(match.group(2))
        if pct is None or base is None:
            return None
        try:
            value = base * pct / 100.0
            expression = f"{_fmt_number(pct)}% of {_fmt_number(base)}"
            return expression, _fmt_number(value), []
        except (OverflowError, ValueError):
            return None
    match = PERCENT_ADD_RE.match(query)
    if match:
        base = _parse_plain_number(match.group(1))
        sign = match.group(2)
        pct = _parse_plain_number(match.group(3))
        if pct is None or base is None:
            return None
        try:
            part = base * pct / 100.0
            value = base + part if sign == "+" else base - part
            expression = f"{_fmt_number(base)} {sign} {_fmt_number(pct)}%"
            extra = [f"{_fmt_number(pct)}% of {_fmt_number(base)} = {_fmt_number(part)}"]
            return expression, _fmt_number(value), extra
        except (OverflowError, ValueError):
            return None
    return None


def _try_expression(query: str) -> tuple[str, str, list[str]] | None:
    if not CHARSET_RE.fullmatch(query):
        return None
    # every run of letters must be a whitelisted function or constant;
    # this keeps "windows 11", "400 usd", "top 10 movies" out
    for token in LETTER_RUN_RE.findall(query):
        if token not in ALLOWED_NAMES:
            return None
    if not any(ch.isdigit() for ch in query):
        return None
    if BARE_NUMBER_RE.fullmatch(query):
        return None
    has_call = FUNC_CALL_RE.search(query) is not None
    if not has_call and not OPERATOR_RE.search(query):
        return None
    if "," in query and not has_call:
        # single Danish decimal comma ("2,5") vs digit-group commas ("1,000")
        if query.count(",") == 1 and DECIMAL_COMMA_RE.search(query):
            query = re.sub(r"(?<=\d),(?=\d)", ".", query)
        else:
            query = GROUP_COMMA_RE.sub("", query)
    try:
        tree = ast.parse(query.replace("^", "**"), mode="eval")
        value = _eval_node(tree)
        if isinstance(value, complex) or isinstance(value, bool):
            return None
        result = _fmt_number(value)
        expression = _pretty(tree)
    except Exception:  # pylint: disable=broad-except
        # OverflowError, ZeroDivisionError, ValueError, SyntaxError, ...
        # any failure means "no answer", never a broken search
        return None
    return expression, result, []


def _solve(raw_query: str) -> tuple[str, str, list[str]] | None:
    query = raw_query.strip().lower()
    if not query or len(query) > MAX_QUERY_LEN:
        return None
    if YEAR_RANGE_RE.match(query):
        return None
    query = query.replace("π", "pi").replace("×", "*").replace("÷", "/")
    for solver in (_try_baseconv, _try_percent, _try_expression):
        solved = solver(query)
        if solved is not None:
            return solved
    return None


class CalcAnswer(BaseAnswer, kw_only=True):
    """Calculator result: summary in ``answer``, precomputed display parts in
    ``expression`` / ``result`` / ``extra`` (templates must not do math)."""

    template: str = TEMPLATE
    answer: str
    expression: str
    result: str
    extra: list[str] = []

    def __hash__(self) -> int:
        # content hash so AnswerSet dedups identical calculator answers
        return hash(self.answer)


@typing.final
class SXNGPlugin(Plugin):
    """Server-side calculator for math, percent and base-conversion queries."""

    id = "calc_answer"
    keywords = []  # not first-word triggered; gate on the whole query instead

    def __init__(self, plg_cfg: "PluginCfg") -> None:
        super().__init__(plg_cfg)
        self.info = PluginInfo(
            id=self.id,
            name="Calculator",
            description=(
                "Evaluates math expressions, percentages and number-base "
                "conversions server-side (also in JSON output)."
            ),
            examples=["2+2*3", "15% of 340", "0xff in decimal"],
            preference_section="query",
        )

    def post_search(self, request: "SXNG_Request", search: "SearchWithPlugins") -> EngineResults:
        results = EngineResults()
        if search.search_query.pageno > 1:
            return results

        raw_query = search.search_query.query
        try:
            solved = _solve(raw_query)
        except Exception:  # pylint: disable=broad-except
            # any failure means "no answer", never a broken search
            solved = None
        if solved is None:
            return results

        expression, result, extra = solved
        results.add(
            CalcAnswer(
                answer=f"{expression} = {result}",
                expression=expression,
                result=result,
                extra=extra,
                url=ANSWER_URL.format(query=quote_plus(raw_query.strip())),
            )
        )
        return results
