# Native functions and strings

NOX 0.4 adds functions, named locals, and immutable UTF-8 strings to the existing
register DSL. These compile to native C functions and values. The executable
uses the C standard library; Python remains a build-time dependency only.

## Functions and locals

```text
fn factorial(n):
    if n == 0 goto base
    saved = n
    result = factorial(n - 1)
    return saved * result
    base:
    return 1

print factorial(10)
```

Declare functions at the top level with `fn name(parameters):` and an indented
body. Forward calls, recursion, and mutual recursion are supported. Each call
has its own parameters, named locals, temporaries, and explicit `rN` registers.
Registers start at integer zero. Arguments are passed by value; immutable
strings can be shared between calls. Calls return into a caller-owned register.

Use `return expression` for a result. Bare `return` and function fallthrough
return integer zero. Calling a function as a statement discards its result.
`halt` terminates the entire program, including when used inside a function.
Calls require the declared argument count. Nested functions and closures are
not supported.

Named variables belong to main or one function; assign before use. Functions
cannot capture names from another scope. Explicit linear memory remains shared
between functions. Labels are scoped to main or a function; branches cannot
cross function boundaries.

Named assignments, literals, parentheses, nested calls, and existing arithmetic,
bitwise, and comparison operators are available in expressions. Existing
register statements remain available. Single-quoted character literals retain
their integer codepoint meaning; use double-quoted strings for text. Comments
start with `#` or `;` outside quoted literals.

`--regs N` applies to parameters, named locals, explicit registers, and
temporaries in each scope. Named locals are allocated separately from explicit
registers, and temporaries are reused between statements. Exhaustion is a
located source error. The CLI default is 32 registers; increase it up to 256
for larger functions or expressions.

## Strings and builtins

| Expression | Result |
| --- | --- |
| `"Hello, " + name` | Concatenate two strings |
| `concat(a, b)` | Same operation as `a + b` |
| `len(text)` | UTF-8 byte length, including embedded NUL bytes |
| `str(value)` | Decimal integer text; existing strings are unchanged |
| `html.escape(text)` | Escape `&`, `<`, `>`, `"`, and `'` |
| `a == b` | Value equality; different value types compare unequal |

Strings support Unicode and escapes such as `\n`, `\t`, `\"`, `\\`, and `\0`.
Literals must encode as valid UTF-8. `len("é")` is 2, and `len("A\0B")` is 3.
Strings are immutable; output uses explicit byte lengths so NUL does not
truncate text.

`print expression` writes exact string bytes or decimal integer text without
an automatic newline. `putc` writes the low byte of an integer, and `read`
reads one byte or returns -1 at EOF.

Addition accepts two integers or two strings. Other arithmetic, integer
comparisons, shifts, branch tests, memory addresses, and `putc` require integer
values. `len` and `html.escape` require strings. Invalid types produce a runtime
diagnostic on stderr and exit code 4. Integer wraparound, division, shifts, and
memory bounds retain the original ISA semantics.

## Generating HTML

```text
fn heading(name):
    return "<h1>Hello, " + html.escape(name) + "</h1>"

print heading("María & <agents>") + "\n"
```

This emits `<h1>Hello, María &amp; &lt;agents&gt;</h1>`. `html.escape` handles
HTML text and quoted attribute values; it does not validate URLs, CSS, or
JavaScript. This is text generation, without a template engine or DOM.

The complete example produces a standalone document:

```bash
python -m nox compile examples/html.nox -o page.nxa --seed demo
python -m nox build page.nxa -o page --seed demo
./page > page.html
```

The final executable runs without Python on the target platform. Building it
still needs NOX and a C compiler. Native JSON and MCP stdio tool servers are
available in 0.5; see [`MCP.md`](MCP.md). HTTP and MCP clients remain future work.

## Limits and artifacts

Extended programs use tagged integer/string values and a native string arena.
Allocations are released at process exit, including runtime errors. The arena
defaults to 64 MiB including string bytes and allocation metadata. Overwritten
temporary strings remain allocated until exit, so long-running text-heavy
programs can reach the limit even if their latest result is small. The call
depth defaults to 256 active calls. Exceeding either limit reports a runtime
error and exits with code 4. These limits are not an operating-system sandbox.

New instructions serialize as artifact format v4. Pure register programs keep
v3 bytecode and unchanged encoding. JSON/MCP instructions use v5. The decoder
accepts v2 through v5; older NOX decoders cannot read newer artifacts. Obfuscation and its limits
remain unchanged; see [`SECURITY.md`](SECURITY.md).
