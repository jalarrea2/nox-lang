"""Lower functions, named locals, and checked expressions into native NOX IR.

Python's AST is used only to recognize a deliberately small expression grammar.
Source is never evaluated or executed by the toolchain.
"""
import ast
import json
import keyword
import re
import unicodedata
from dataclasses import dataclass

from .dsl import NoxSyntaxError, _emit, _reg, _strip_comment
from .isa import I64_MAX, I64_MIN, validate_tool_metadata

_IDENT = r"[A-Za-z_]\w*"
_REGISTER = re.compile(r"r(\d+)\Z")
_LABEL = re.compile(rf"({_IDENT}):\Z")
_HEADER = re.compile(rf"fn\s+({_IDENT})\s*\((.*?)\)\s*:\Z")
_ASSIGN = re.compile(rf"(?:let\s+)?({_IDENT})\s*=\s*(?!=)(.+)\Z")
_STORE = re.compile(r"\[\s*(.+)\s*\]\s*=\s*(?!=)(.+)\Z")
_BUILTINS = {"len", "str", "concat", "html", "json", "mcp"}
_BINARY = {
    ast.Add: "ADD", ast.Sub: "SUB", ast.Mult: "MUL", ast.Div: "DIV", ast.Mod: "MOD",
    ast.BitAnd: "AND", ast.BitOr: "OR", ast.BitXor: "XOR", ast.LShift: "SHL", ast.RShift: "SHR",
}
_COMPARE = {ast.Lt: "LT", ast.Gt: "GT", ast.Eq: "EQ"}
_END = object()


@dataclass
class _LabelTarget:
    name: str
    line: int


@dataclass
class _FunctionTarget:
    name: str


@dataclass
class _Value:
    register: int
    temporary: bool = False


def _name(name, line):
    if not re.fullmatch(_IDENT, name) or keyword.iskeyword(name):
        raise NoxSyntaxError(f"invalid name {name!r}", line)
    name = unicodedata.normalize("NFKC", name)
    if keyword.iskeyword(name):
        raise NoxSyntaxError(f"invalid name {name!r}", line)
    if name in _BUILTINS:
        raise NoxSyntaxError(f"name {name!r} is reserved for a builtin", line)
    return name


def _without_literals(text):
    """Hide literal contents before scanning for explicit register tokens."""
    result, pos = [], 0
    while pos < len(text):
        char = text[pos]
        if char in "\"'":
            if char == "'" and pos + 2 < len(text) and text[pos + 2] == "'":
                pos += 3
            else:
                pos += 1
                while pos < len(text):
                    if text[pos] == "\\":
                        pos += 2
                    elif text[pos] == char:
                        pos += 1
                        break
                    else:
                        pos += 1
            result.append(" ")
        else:
            result.append(char)
            pos += 1
    return "".join(result)


def _expression_text(text):
    """Preserve legacy ordinal literals, including quote and backslash chars."""
    result, pos = [], 0
    while pos < len(text):
        char = text[pos]
        if char == "'" and pos + 2 < len(text) and text[pos + 2] == "'":
            result.append(str(ord(text[pos + 1])))
            pos += 3
        elif char in "\"'":
            start = pos
            pos += 1
            while pos < len(text):
                if text[pos] == "\\":
                    pos += 2
                elif text[pos] == char:
                    pos += 1
                    break
                else:
                    pos += 1
            result.append(text[start:pos])
        else:
            result.append(char)
            pos += 1
    return "".join(result)


class _Scope:
    def __init__(self, name, parameters, line, statements, nregs):
        self.name = name
        self.parameter_names = parameters
        self.line = line
        self.statements = statements
        self.nregs = nregs
        self.reserved = set()
        self.locals = {}
        self.defined = set(parameters)
        self.temps = set()
        self.ir = []
        self.labels = {}
        for parameter in parameters:
            self._reserve(parameter, line)
        for lineno, text in statements:
            if _LABEL.fullmatch(text) or re.match(r"goto\s+", text):
                continue
            code = re.split(r"\s+goto\s+", _without_literals(text), maxsplit=1)[0]
            for match in re.finditer(r"(?<!\w)r(\d+)(?!\w)", code):
                self._reserve("r" + match.group(1), lineno)
        self.parameters = tuple(self.bind(parameter, line) for parameter in parameters)
        if len(set(self.parameters)) != len(self.parameters):
            raise NoxSyntaxError("function parameters must use distinct registers", line)
        for lineno, text in statements:
            assignment = _ASSIGN.fullmatch(text)
            read = re.fullmatch(rf"read\s+({_IDENT})", text)
            if assignment or read:
                self.bind((assignment or read).group(1), lineno)

    def _reserve(self, name, line):
        match = _REGISTER.fullmatch(name)
        if match:
            register = _reg(match.group(1), line)
            if register >= self.nregs:
                raise NoxSyntaxError(
                    f"register r{register} out of range (expected r0..r{self.nregs - 1})", line,
                )
            self.reserved.add(register)

    def allocate(self, line, temporary=False):
        occupied = self.reserved | set(self.locals.values()) | self.temps
        for register in range(self.nregs):
            if register not in occupied:
                if temporary:
                    self.temps.add(register)
                return register
        raise NoxSyntaxError(f"register file exhausted (configured {self.nregs} registers)", line)

    def bind(self, name, line):
        match = _REGISTER.fullmatch(name)
        if match:
            return _reg(match.group(1), line)
        name = _name(name, line)
        if name not in self.locals:
            self.locals[name] = self.allocate(line)
        return self.locals[name]

    def lookup(self, name, line):
        match = _REGISTER.fullmatch(name)
        if match:
            register = _reg(match.group(1), line)
            if register >= self.nregs:
                raise NoxSyntaxError(f"register r{register} out of range", line)
            return register
        name = unicodedata.normalize("NFKC", name)
        if name not in self.locals or name not in self.defined:
            raise NoxSyntaxError(f"unknown local variable {name!r}", line)
        return self.locals[name]

    def release(self, value, keeping=None):
        if value.temporary and value.register != keeping:
            self.temps.discard(value.register)

    def destination(self, requested, values, line):
        if requested is not None:
            return _Value(requested)
        for value in values:
            if value.temporary:
                return value
        return _Value(self.allocate(line, temporary=True), True)


def _discover(lines, nregs):
    main = []
    declarations = []
    names = set()
    current = None
    body_indent = None
    for lineno, original in enumerate(lines, 1):
        text = _strip_comment(original).strip()
        if not text:
            continue
        indentation = len(original[:len(original) - len(original.lstrip(" \t"))].expandtabs(4))
        header = _HEADER.fullmatch(text)
        if header:
            if indentation:
                raise NoxSyntaxError("function declarations must be top-level", lineno)
            if current is not None and not current[3]:
                raise NoxSyntaxError("function body must contain an indented statement", current[2])
            name = _name(header.group(1), lineno)
            if _REGISTER.fullmatch(name):
                raise NoxSyntaxError("function name cannot be a register", lineno)
            if name in names:
                raise NoxSyntaxError(f"duplicate function {name!r}", lineno)
            names.add(name)
            parameters = []
            if header.group(2).strip():
                for parameter in header.group(2).split(","):
                    parameter = _name(parameter.strip(), lineno)
                    if parameter in parameters:
                        raise NoxSyntaxError(f"duplicate parameter {parameter!r}", lineno)
                    parameters.append(parameter)
            current = (name, parameters, lineno, [])
            declarations.append(current)
            body_indent = None
        elif current is not None and indentation:
            if body_indent is None:
                body_indent = indentation
            if indentation != body_indent:
                raise NoxSyntaxError("unexpected indentation inside function", lineno)
            current[3].append((lineno, text))
        else:
            if current is not None and not current[3]:
                raise NoxSyntaxError("function body must contain an indented statement", current[2])
            current = None
            main.append((lineno, text))
    if current is not None and not current[3]:
        raise NoxSyntaxError("function body must contain an indented statement", current[2])
    scopes = [_Scope(None, [], 1, main, nregs)]
    scopes.extend(_Scope(*declaration, nregs) for declaration in declarations)
    return scopes


class _Compiler:
    def __init__(self, scope, functions):
        self.scope = scope
        self.functions = functions
        self.line = scope.line
        self.expression_source = ""
        self.tools = set()

    def emit(self, *instruction):
        self.scope.ir.append(instruction)

    def parse_expression(self, expression):
        self.expression_source = _expression_text(expression.strip())
        try:
            return ast.parse(self.expression_source, mode="eval").body
        except RecursionError:
            raise NoxSyntaxError("expression too complex", self.line) from None
        except (SyntaxError, ValueError):
            raise NoxSyntaxError("invalid expression", self.line) from None

    def literal(self, value, requested):
        if type(value) is int:
            if not I64_MIN <= value <= I64_MAX:
                raise NoxSyntaxError("immediate out of i64 range", self.line)
            opcode = "IMM"
        elif type(value) is str:
            try:
                value.encode("utf-8")
            except UnicodeEncodeError:
                raise NoxSyntaxError(
                    "string literal must contain valid Unicode", self.line,
                ) from None
            opcode = "STR"
        else:
            raise NoxSyntaxError("only i64 and string literals are supported", self.line)
        result = self.scope.destination(requested, [], self.line)
        self.emit(opcode, result.register, value)
        return result

    def expression(self, node, requested=None):
        if isinstance(node, ast.Name):
            value = _Value(self.scope.lookup(node.id, self.line))
            if requested is not None and requested != value.register:
                self.emit("MOV", requested, value.register)
                return _Value(requested)
            return value
        if isinstance(node, ast.Constant):
            value = node.value
            segment = ast.get_source_segment(self.expression_source, node) or ""
            if type(value) is str and segment.startswith("'"):
                if len(value) != 1:
                    raise NoxSyntaxError(
                        "single-quoted literals must contain one character", self.line,
                    )
                value = ord(value)
            return self.literal(value, requested)
        if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
            left = self.expression(node.left)
            right = self.expression(node.right)
            result = self.scope.destination(requested, [left, right], self.line)
            self.emit(_BINARY[type(node.op)], result.register, left.register, right.register)
            self.scope.release(left, result.register)
            self.scope.release(right, result.register)
            return result
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            if isinstance(node.operand, ast.Constant) and type(node.operand.value) is int:
                sign = -1 if isinstance(node.op, ast.USub) else 1
                return self.literal(sign * node.operand.value, requested)
            value = self.expression(node.operand)
            if isinstance(node.op, ast.UAdd):
                result = self.scope.destination(requested, [value], self.line)
                if result.register != value.register:
                    self.emit("MOV", result.register, value.register)
                self.scope.release(value, result.register)
                return result
            zero = self.literal(0, None)
            result = self.scope.destination(requested, [value, zero], self.line)
            self.emit("SUB", result.register, zero.register, value.register)
            self.scope.release(value, result.register)
            self.scope.release(zero, result.register)
            return result
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            left = self.expression(node.left)
            right = self.expression(node.comparators[0])
            comparison = type(node.ops[0])
            negate = comparison in (ast.NotEq, ast.LtE, ast.GtE)
            opcode = _COMPARE.get(comparison, {ast.NotEq: "EQ", ast.LtE: "GT", ast.GtE: "LT"}.get(
                comparison,
            ))
            if opcode is None:
                raise NoxSyntaxError("unsupported comparison", self.line)
            result = self.scope.destination(requested, [left, right], self.line)
            self.emit(opcode, result.register, left.register, right.register)
            self.scope.release(left, result.register)
            self.scope.release(right, result.register)
            if negate:
                zero = self.literal(0, None)
                self.emit("EQ", result.register, result.register, zero.register)
                self.scope.release(zero, result.register)
            return result
        if isinstance(node, ast.List) and len(node.elts) == 1:
            address = self.expression(node.elts[0])
            result = self.scope.destination(requested, [address], self.line)
            self.emit("LD", result.register, address.register)
            self.scope.release(address, result.register)
            return result
        if isinstance(node, ast.Call):
            return self.call(node, requested)
        raise NoxSyntaxError(f"unsupported expression: {type(node).__name__}", self.line)

    def call(self, node, requested):
        if node.keywords or any(isinstance(argument, ast.Starred) for argument in node.args):
            raise NoxSyntaxError("calls accept positional arguments only", self.line)
        if isinstance(node.func, ast.Name):
            name = node.func.id
        elif isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
            name = node.func.value.id + "." + node.func.attr
            if node.func.value.id == "mcp":
                raise NoxSyntaxError(
                    "MCP operations require a standalone main-scope statement", self.line,
                )
            if node.func.value.id not in ("html", "json"):
                raise NoxSyntaxError("unsupported function call", self.line)
        else:
            raise NoxSyntaxError("unsupported function call", self.line)
        builtins = {
            "len": (1, "SLEN"), "str": (1, "TOSTR"),
            "html.escape": (1, "HESC"), "concat": (2, "ADD"),
            "json.stringify": (1, "JENC"), "json.get": (2, "JGET"),
            "json.at": (2, "JAT"), "json.string": (1, "JSTR"),
            "json.int": (1, "JINT"), "json.valid": (1, "JVALID"),
        }
        if name in builtins:
            arity, opcode = builtins[name]
        elif name in self.functions:
            arity, opcode = len(self.functions[name].parameters), "CALL"
        else:
            raise NoxSyntaxError(f"unknown function {name!r}", self.line)
        if len(node.args) != arity:
            raise NoxSyntaxError(f"function {name!r} expects {arity} arguments", self.line)
        values = [self.expression(argument) for argument in node.args]
        result = self.scope.destination(requested, values, self.line)
        registers = tuple(value.register for value in values)
        if opcode == "CALL":
            self.emit(opcode, result.register, _FunctionTarget(name), registers)
        else:
            self.emit(opcode, result.register, *registers)
        for value in values:
            self.scope.release(value, result.register)
        return result

    def evaluate(self, expression, requested=None):
        self.expression_source = expression.strip()
        return self.expression(self.parse_expression(expression), requested)

    def mcp_statement(self, node):
        if self.scope.name is not None:
            raise NoxSyntaxError("MCP operations are only valid in main scope", self.line)
        if node.keywords or any(isinstance(argument, ast.Starred) for argument in node.args):
            raise NoxSyntaxError("MCP operations accept positional arguments only", self.line)
        operation = node.func.attr
        if operation == "serve_stdio":
            if node.args:
                raise NoxSyntaxError("mcp.serve_stdio expects 0 arguments", self.line)
            self.emit("MCPSERVE")
            return
        if operation != "tool":
            raise NoxSyntaxError(f"unknown MCP operation {operation!r}", self.line)
        if len(node.args) not in (2, 3):
            raise NoxSyntaxError("mcp.tool expects 2 or 3 arguments", self.line)
        name_node, reference = node.args[:2]
        description_node = node.args[2] if len(node.args) == 3 else None
        if (not isinstance(name_node, ast.Constant) or type(name_node.value) is not str
                or (description_node is not None and (not isinstance(description_node, ast.Constant)
                    or type(description_node.value) is not str))):
            raise NoxSyntaxError("MCP tool name and description must be string literals", self.line)
        if not isinstance(reference, ast.Name) or reference.id not in self.functions:
            raise NoxSyntaxError("MCP tool reference must name a declared function", self.line)
        name = name_node.value
        description = description_node.value if description_node is not None else ""
        names = json.dumps(self.functions[reference.id].parameter_names,
                           ensure_ascii=False, separators=(",", ":"))
        try:
            description.encode("utf-8", errors="strict")
            validate_tool_metadata(name, names, description,
                                   len(self.functions[reference.id].parameters))
        except (ValueError, UnicodeEncodeError) as error:
            raise NoxSyntaxError(str(error), self.line) from None
        if name in self.tools:
            raise NoxSyntaxError(f"duplicate MCP tool name {name!r}", self.line)
        self.tools.add(name)
        self.emit("MCPTOOL", name, _FunctionTarget(reference.id), names, description)

    def statement(self, text):
        scope = self.scope
        label = _LABEL.fullmatch(text)
        if label:
            name = label.group(1)
            if name in scope.labels:
                raise NoxSyntaxError(f"duplicate label {name!r}", self.line)
            scope.labels[name] = len(scope.ir)
            return
        conditional = re.fullmatch(rf"if\s+(.+?)\s+goto\s+({_IDENT})", text)
        if conditional:
            expression, target = conditional.groups()
            value = self.evaluate(expression)
            self.emit("JNZ", value.register, _LabelTarget(target, self.line))
            scope.release(value)
            return
        try:
            instruction = _emit(text, self.line, lambda name, line: _LabelTarget(name, line))
        except NoxSyntaxError:
            instruction = None
        if instruction is not None:
            self.emit(*instruction)
            return
        for keyword_, opcode in (("print", "OUTN"), ("putc", "OUT")):
            if text.startswith(keyword_ + " "):
                value = self.evaluate(text[len(keyword_):].strip())
                self.emit(opcode, value.register)
                scope.release(value)
                return
        read = re.fullmatch(rf"read\s+({_IDENT})", text)
        if read:
            name = read.group(1)
            self.emit("IN", scope.bind(name, self.line))
            scope.defined.add(unicodedata.normalize("NFKC", name))
            return
        if text == "return" or text.startswith("return "):
            if scope.name is None:
                raise NoxSyntaxError("return is only valid inside a function", self.line)
            value = self.evaluate(text[6:].strip() or "0")
            self.emit("RET", value.register)
            scope.release(value)
            return
        assignment = _ASSIGN.fullmatch(text)
        if assignment:
            name = assignment.group(1)
            self.evaluate(assignment.group(2), scope.bind(name, self.line))
            scope.defined.add(unicodedata.normalize("NFKC", name))
            return
        store = _STORE.fullmatch(text)
        if store:
            address = self.evaluate(store.group(1))
            value = self.evaluate(store.group(2))
            self.emit("ST", address.register, value.register)
            scope.release(address)
            scope.release(value)
            return
        self.expression_source = text
        try:
            node = self.parse_expression(text)
        except NoxSyntaxError:
            raise NoxSyntaxError("unrecognized statement", self.line) from None
        if isinstance(node, ast.Call):
            if (isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "mcp"):
                self.mcp_statement(node)
                return
            scope.release(self.expression(node))
            return
        raise NoxSyntaxError("unrecognized statement", self.line)

    def compile(self):
        for lineno, text in self.scope.statements:
            self.line = lineno
            self.scope.temps.clear()
            try:
                self.statement(text)
            except RecursionError:
                raise NoxSyntaxError("expression too complex", lineno) from None


def compile_source(lines, nregs):
    """Compile extended source while preserving flat, serializable tuple IR."""
    scopes = _discover(lines, nregs)
    functions = {scope.name: scope for scope in scopes[1:]}
    for scope in scopes:
        _Compiler(scope, functions).compile()
    if functions:
        scopes[0].ir.append(("JMP", _END))
    offsets, targets, size = {}, {}, 0
    for scope in scopes:
        if scope.name is not None:
            targets[scope.name] = size
            size += 1
        offsets[id(scope)] = size
        size += len(scope.ir) + (scope.name is not None)
    result = []
    for scope in scopes:
        if scope.name is not None:
            result.append(("FUNC", scope.name, scope.parameters))
        for instruction in scope.ir:
            operands = []
            for operand in instruction[1:]:
                if isinstance(operand, _LabelTarget):
                    if operand.name not in scope.labels:
                        raise NoxSyntaxError(f"unknown label {operand.name!r}", operand.line)
                    operand = offsets[id(scope)] + scope.labels[operand.name]
                elif isinstance(operand, _FunctionTarget):
                    operand = targets[operand.name]
                elif operand is _END:
                    operand = size
                operands.append(operand)
            result.append((instruction[0], *operands))
        if scope.name is not None:
            result.append(("ENDFUNC",))
    return result
