"""A tiny, safe arithmetic grammar for metric definitions.

Stewards write metric expressions like `quantity * unit_price` in manifests. These are user
input, so they are never pasted into SQL. Instead we parse them into a small tree (AST) that
only allows:

    names (columns, or metrics in derived formulas)   numbers   + - * /   ( )   unary minus

Anything else (quotes, semicolons, function calls, keywords) is rejected. The query compiler
(M3) turns the tree into SQL itself, quoting each name safely.

Grammar:
    expr   := term (("+" | "-") term)*
    term   := factor (("*" | "/") factor)*
    factor := NUMBER | NAME | "(" expr ")" | "-" factor
    NAME   := [a-z_][a-z0-9_]*  optionally "." [a-z_][a-z0-9_]*   (source.metric)
"""

import re
from dataclasses import dataclass

_TOKEN = re.compile(r"\s*(?:(\d+(?:\.\d+)?)|([a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)?)|(.))")


@dataclass(frozen=True)
class Num:
    value: float


@dataclass(frozen=True)
class Name:
    name: str  # "quantity", or "returns.refund_value" in derived formulas


@dataclass(frozen=True)
class Neg:
    operand: "Node"


@dataclass(frozen=True)
class BinOp:
    op: str  # one of + - * /
    left: "Node"
    right: "Node"


Node = Num | Name | Neg | BinOp


class ExpressionError(ValueError):
    pass


def _tokenize(text: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    for number, name, other in _TOKEN.findall(text):
        if number:
            tokens.append(("num", number))
        elif name:
            tokens.append(("name", name))
        elif other.strip():
            if other not in "+-*/()":
                raise ExpressionError(f"character {other!r} is not allowed")
            tokens.append(("op", other))
    return tokens


class _Parser:
    def __init__(self, tokens: list[tuple[str, str]]) -> None:
        self.tokens, self.pos = tokens, 0

    def peek(self) -> tuple[str, str] | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def take(self) -> tuple[str, str]:
        tok = self.peek()
        if tok is None:
            raise ExpressionError("unexpected end of expression")
        self.pos += 1
        return tok

    def expr(self) -> Node:
        node = self.term()
        while (tok := self.peek()) and tok[1] in "+-" and tok[0] == "op":
            self.take()
            node = BinOp(tok[1], node, self.term())
        return node

    def term(self) -> Node:
        node = self.factor()
        while (tok := self.peek()) and tok[1] in "*/" and tok[0] == "op":
            self.take()
            node = BinOp(tok[1], node, self.factor())
        return node

    def factor(self) -> Node:
        kind, value = self.take()
        if kind == "num":
            return Num(float(value))
        if kind == "name":
            return Name(value)
        if value == "-":
            return Neg(self.factor())
        if value == "(":
            node = self.expr()
            if self.take() != ("op", ")"):
                raise ExpressionError("missing closing parenthesis")
            return node
        raise ExpressionError(f"unexpected {value!r}")


def parse(text: str) -> Node:
    if not text or not text.strip():
        raise ExpressionError("expression is empty")
    if len(text) > 500:
        raise ExpressionError("expression is too long")
    parser = _Parser(_tokenize(text))
    node = parser.expr()
    if parser.peek() is not None:
        raise ExpressionError(f"unexpected {parser.peek()[1]!r}")
    return node


def names(node: Node) -> set[str]:
    """Every name the expression refers to."""
    if isinstance(node, Name):
        return {node.name}
    if isinstance(node, Neg):
        return names(node.operand)
    if isinstance(node, BinOp):
        return names(node.left) | names(node.right)
    return set()
