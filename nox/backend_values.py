"""Lower extended IR to native C functions and bounded immutable byte strings.

The emitted program uses direct C calls and labels. Values have two tags, but
there is no bytecode dispatch or dependency on the Python compiler at runtime.
"""
from .isa import I64_MIN, analyze_functions

_TYPES = r"""
typedef struct nox_string {
    struct nox_string *next;
    size_t length;
    unsigned char bytes[];
} nox_string;

typedef struct {
    int is_string;
    int64_t integer;
    const nox_string *string;
} nox_value;

#ifndef NOX_STRING_LIMIT
#define NOX_STRING_LIMIT (64u * 1024u * 1024u)
#endif
#ifndef NOX_CALL_DEPTH
#define NOX_CALL_DEPTH 256u
#endif

static nox_string *nox_arena;
static size_t nox_arena_bytes;
static unsigned nox_depth;
"""

_VALUES = r"""
static void nox_cleanup(void) {
    while (nox_arena) {
        nox_string *next = nox_arena->next;
        free(nox_arena);
        nox_arena = next;
    }
    nox_arena_bytes = 0;
}

static void nox_fail(const char *message) {
    fprintf(stderr, "[nox] runtime error: %s\n", message);
    exit(4);
}

static nox_value nox_int(int64_t number) {
    nox_value value = {0};
    value.integer = number;
    return value;
}

static nox_value nox_string_handle(const nox_string *text) {
    nox_value value = {0};
    value.is_string = 1;
    value.string = text;
    return value;
}

static int64_t nox_integer(nox_value value, const char *operation) {
    if (value.is_string) {
        fprintf(stderr, "[nox] runtime error: %s requires an integer value\n", operation);
        exit(4);
    }
    return value.integer;
}

static const nox_string *nox_text(nox_value value, const char *operation) {
    if (!value.is_string) {
        fprintf(stderr, "[nox] runtime error: %s requires a string value\n", operation);
        exit(4);
    }
    return value.string;
}

static size_t nox_length_add(size_t a, size_t b) {
    if (b > SIZE_MAX - a) nox_fail("string length overflow");
    return a + b;
}

static nox_string *nox_allocate_string(size_t length) {
    size_t size = nox_length_add(sizeof(nox_string), length);
    if (size > (size_t)NOX_STRING_LIMIT ||
            nox_arena_bytes > (size_t)NOX_STRING_LIMIT - size) {
        nox_fail("string arena limit exceeded");
    }
    nox_string *text = malloc(size);
    if (!text) nox_fail("string allocation failed");
    text->length = length;
    text->next = nox_arena;
    nox_arena = text;
    nox_arena_bytes += size;
    return text;
}

static nox_value nox_string_copy(const unsigned char *bytes, size_t length) {
    nox_string *text = nox_allocate_string(length);
    if (length) memcpy(text->bytes, bytes, length);
    return nox_string_handle(text);
}

static nox_value nox_add(nox_value a, nox_value b) {
    if (!a.is_string && !b.is_string) {
        return nox_int(nox_i64((uint64_t)a.integer + (uint64_t)b.integer));
    }
    if (!a.is_string || !b.is_string) nox_fail("ADD requires two integers or two strings");
    nox_string *text = nox_allocate_string(nox_length_add(a.string->length, b.string->length));
    if (a.string->length) memcpy(text->bytes, a.string->bytes, a.string->length);
    if (b.string->length) {
        memcpy(text->bytes + a.string->length, b.string->bytes, b.string->length);
    }
    return nox_string_handle(text);
}

static nox_value nox_equal(nox_value a, nox_value b) {
    if (a.is_string != b.is_string) return nox_int(0);
    if (!a.is_string) return nox_int(a.integer == b.integer);
    return nox_int(a.string->length == b.string->length &&
                  (!a.string->length ||
                   memcmp(a.string->bytes, b.string->bytes, a.string->length) == 0));
}

static nox_value nox_to_string(nox_value value) {
    if (value.is_string) return value;
    char buffer[32];
    int length = snprintf(buffer, sizeof buffer, "%lld", (long long)value.integer);
    if (length < 0 || (size_t)length >= sizeof buffer) nox_fail("integer formatting failed");
    return nox_string_copy((const unsigned char *)buffer, (size_t)length);
}

static const char *nox_escape_byte(unsigned char byte, size_t *length) {
    switch (byte) {
        case '&': *length = 5; return "&amp;";
        case '<': *length = 4; return "&lt;";
        case '>': *length = 4; return "&gt;";
        case '"': *length = 6; return "&quot;";
        case '\'': *length = 5; return "&#39;";
        default: *length = 1; return NULL;
    }
}

static nox_value nox_html_escape(nox_value value) {
    const nox_string *source = nox_text(value, "HESC");
    size_t length = 0;
    for (size_t i = 0; i < source->length; ++i) {
        size_t count;
        nox_escape_byte(source->bytes[i], &count);
        length = nox_length_add(length, count);
    }
    nox_string *text = nox_allocate_string(length);
    size_t position = 0;
    for (size_t i = 0; i < source->length; ++i) {
        size_t count;
        const char *replacement = nox_escape_byte(source->bytes[i], &count);
        if (replacement) memcpy(text->bytes + position, replacement, count);
        else text->bytes[position] = source->bytes[i];
        position += count;
    }
    return nox_string_handle(text);
}

static void nox_print_to(nox_value value, FILE *stream) {
    if (value.is_string) {
        if (value.string->length) fwrite(value.string->bytes, 1, value.string->length, stream);
    } else {
        fprintf(stream, "%lld", (long long)value.integer);
    }
}

static void nox_print(nox_value value) {
    nox_print_to(value, stdout);
}
"""

_BIN = {"SUB": "-", "MUL": "*", "AND": "&", "OR": "|", "XOR": "^"}


def _integer(register, operation):
    return f'nox_integer(r[{register}], "{operation}")'


def _statement(instruction, index, mcp_mode=False):
    op = instruction[0]
    args = instruction[1:]
    if op == "IMM":
        value = "INT64_MIN" if args[1] == I64_MIN else f"{args[1]}LL"
        return f"r[{args[0]}] = nox_int({value});"
    if op == "STR":
        length = len(args[1].encode("utf-8"))
        return f"r[{args[0]}] = nox_string_copy(nox_literal_{index}, {length});"
    if op == "MOV":
        return f"r[{args[0]}] = r[{args[1]}];"
    if op in ("ADD", "EQ"):
        helper = "nox_add" if op == "ADD" else "nox_equal"
        return f"r[{args[0]}] = {helper}(r[{args[1]}], r[{args[2]}]);"
    if op in _BIN:
        a, b = (_integer(register, op) for register in args[1:])
        return f"r[{args[0]}] = nox_int(nox_i64((uint64_t){a} {_BIN[op]} (uint64_t){b}));"
    if op in ("SHL", "SHR", "DIV", "MOD"):
        a, b = (_integer(register, op) for register in args[1:])
        return f"r[{args[0]}] = nox_int(nox_{op.lower()}({a}, {b}));"
    if op in ("LT", "GT"):
        a, b = (_integer(register, op) for register in args[1:])
        operator = "<" if op == "LT" else ">"
        return f"r[{args[0]}] = nox_int({a} {operator} {b});"
    if op == "LD":
        return f"r[{args[0]}] = mem[nox_addr({_integer(args[1], op)})];"
    if op == "ST":
        return f"mem[nox_addr({_integer(args[0], op)})] = r[{args[1]}];"
    if op == "JMP":
        return f"goto I{args[0]};"
    if op in ("JZ", "JNZ"):
        operator = "==" if op == "JZ" else "!="
        return f"if ({_integer(args[0], op)} {operator} 0) goto I{args[1]};"
    if op == "IN":
        if mcp_mode:
            return f"r[{args[0]}] = nox_int(-1);"
        return f"c = getchar(); r[{args[0]}] = nox_int(c == EOF ? -1 : c);"
    if op == "OUT":
        if mcp_mode:
            return f"fputc((int)((uint64_t){_integer(args[0], op)} & 0xFF), stderr);"
        return f"putchar((int)((uint64_t){_integer(args[0], op)} & 0xFF));"
    if op == "OUTN":
        if mcp_mode:
            return f"nox_print_to(r[{args[0]}], stderr);"
        return f"nox_print(r[{args[0]}]);"
    if op == "HALT":
        return "exit(0);"
    if op == "CALL":
        parameters = ", ".join(f"r[{register}]" for register in args[2])
        return f"r[{args[0]}] = nox_function_{args[1]}({parameters});"
    if op == "RET":
        return f"--nox_depth; return r[{args[0]}];"
    if op == "SLEN":
        return f'r[{args[0]}] = nox_int((int64_t)nox_text(r[{args[1]}], "SLEN")->length);'
    if op in ("TOSTR", "HESC"):
        helper = "nox_to_string" if op == "TOSTR" else "nox_html_escape"
        return f"r[{args[0]}] = {helper}(r[{args[1]}]);"
    json_helpers = {"JENC": "nox_json_encode", "JGET": "nox_json_get", "JAT": "nox_json_at",
                    "JSTR": "nox_json_string", "JINT": "nox_json_integer",
                    "JVALID": "nox_json_valid"}
    if op in json_helpers:
        parameters = ", ".join(f"r[{register}]" for register in args[1:])
        return f"r[{args[0]}] = {json_helpers[op]}({parameters});"
    if op == "MCPTOOL":
        values = []
        for position in (0, 2, 3):
            length = len(args[position].encode("utf-8"))
            values.append(f"nox_string_copy(nox_mcp_literal_{index}_{position}, {length})")
        return (f"nox_mcp_register({values[0]}, nox_mcp_adapter_{index}, "
                f"{values[1]}, {values[2]});")
    if op == "MCPSERVE":
        return "nox_mcp_serve(); exit(0);"
    raise ValueError(f"opcode not supported by value back-end: {op}")


def _function_signature(index, params):
    arguments = ", ".join(f"nox_value argument_{i}" for i in range(len(params))) or "void"
    return f"static nox_value nox_function_{index}({arguments})"


def emit_values_c(ir, nregs, memsize, arithmetic_helpers):
    """Emit extended validated IR as a standalone C11 program."""
    owners, functions = analyze_functions(ir)
    has_mcp = any(instruction[0] in ("MCPTOOL", "MCPSERVE") for instruction in ir)
    mcp_mode = any(instruction[0] == "MCPSERVE" for instruction in ir)
    has_json = has_mcp or any(instruction[0] in ("JENC", "JGET", "JAT", "JSTR", "JINT", "JVALID")
                             for instruction in ir)
    lines = ["#define _POSIX_C_SOURCE 200809L"] if has_mcp else []
    lines += ["#include <stdint.h>", "#include <stdio.h>", "#include <stdlib.h>",
             "#include <string.h>", _TYPES, f"static nox_value mem[{memsize}];",
             arithmetic_helpers, _VALUES]
    if has_json:
        from .backend_json import json_support

        lines.append(json_support())
    if has_mcp:
        from .backend_mcp import mcp_support

        lines.append(mcp_support())
    for index, instruction in enumerate(ir):
        if instruction[0] == "STR":
            data = instruction[2].encode("utf-8") + b"\x00"
            lines.append(f"static const unsigned char nox_literal_{index}[] = {{")
            for offset in range(0, len(data), 16):
                chunk = ", ".join(f"0x{byte:02x}" for byte in data[offset:offset + 16])
                lines.append("    " + chunk + ",")
            lines.append("};")
        if instruction[0] == "MCPTOOL":
            for position in (0, 2, 3):
                data = instruction[position + 1].encode("utf-8") + b"\x00"
                symbol = f"nox_mcp_literal_{index}_{position}"
                lines.append(f"static const unsigned char {symbol}[] = {{")
                for offset in range(0, len(data), 16):
                    chunk = ", ".join(f"0x{byte:02x}" for byte in data[offset:offset + 16])
                    lines.append("    " + chunk + ",")
                lines.append("};")
    for index, function in functions.items():
        lines.append(_function_signature(index, function["params"]) + ";")
    for index, instruction in enumerate(ir):
        if instruction[0] == "MCPTOOL":
            parameters = functions[instruction[2]]["params"]
            arguments = ", ".join(f"arguments[{i}]" for i in range(len(parameters)))
            lines.append(f"static nox_value nox_mcp_adapter_{index}(const nox_value *arguments) {{")
            lines.append("    (void)arguments;")
            lines.append(f"    return nox_function_{instruction[2]}({arguments});")
            lines.append("}")
    for index, function in functions.items():
        lines.append(_function_signature(index, function["params"]) + " {")
        lines += ["    if (nox_depth >= NOX_CALL_DEPTH) nox_fail(\"call depth limit exceeded\");",
                  "    ++nox_depth;", f"    nox_value r[{nregs}] = {{0}};",
                  "    int c;", "    (void)c;", "    (void)r;"]
        for argument, register in enumerate(function["params"]):
            lines.append(f"    r[{register}] = argument_{argument};")
        for position in range(index + 1, function["end"]):
            lines += [f"I{position}:;", "    " + _statement(ir[position], position, mcp_mode)]
        lines += [f"I{function['end']}:;", "    --nox_depth;", "    return nox_int(0);", "}"]
    lines += ["int main(void) {",
              '    if (atexit(nox_cleanup)) nox_fail("cannot register string cleanup");',
              f"    nox_value r[{nregs}] = {{0}};", "    int c;", "    (void)c;", "    (void)r;"]
    for index, instruction in enumerate(ir):
        if owners[index] is None:
            lines += [f"I{index}:;", "    " + _statement(instruction, index, mcp_mode)]
    lines += [f"I{len(ir)}:;", "    return 0;", "}"]
    return "\n".join(lines) + "\n"
