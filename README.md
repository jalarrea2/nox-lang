# NOX — metalenguaje opaco compilado a nativo

NOX es un lenguaje **incomprensible para un humano pero ejecutable**: el código
fuente final (`.nxa`) es una cadena de glifos Braille sin estructura visible, y
sólo con la **seed** correcta se puede decodificar y compilar. El binario
resultante es **nativo y eficiente** (`clang -O3`), indistinguible de C escrito a
mano — toda la ofuscación se resuelve en tiempo de compilación, cero overhead en
runtime.

## Por qué es "meta"

No hay keywords fijas. El diccionario `byte → glifo` y el keystream de cifrado se
**derivan de la seed por programa**: dos programas con seeds distintas producen
alfabetos y cifrados distintos. No se puede aprender el lenguaje leyéndolo.

## Pipeline

```
 DSL legible (.nox)
        │  python -m nox compile … --seed K
        ▼
 artefacto opaco (.nxa)   ← lo que se distribuye; ruido sin la seed
        │  python -m nox build/run … --seed K
        ▼
 decode(seed) → IR → C → clang -O3
        ▼
 binario nativo
```

- **Capa 0 (portador):** bloque Braille U+2800–U+28FF (exactamente 256 glifos).
- **Capa 1 (meta):** permutación byte↔glifo + XOR keystream, ambos `f(seed)`.
- **Capa 2 (cómputo):** máquina de registros i64, memoria lineal, I/O. Turing-completa.
- **Capa 3 (runtime):** emisión a C + `clang -O3` (fallback gcc/cc).

## Uso

```bash
python -m nox compile examples/fib.nox -o fib.nxa --seed CLAVE   # DSL → artefacto
python -m nox build   fib.nxa          -o fib     --seed CLAVE   # artefacto → binario
python -m nox run     fib.nxa                     --seed CLAVE   # compila a tmp y ejecuta
python -m nox emit    fib.nxa                     --seed CLAVE   # ver el C generado
python -m nox disasm  fib.nxa                     --seed CLAVE   # ver el IR (debug)
```

Seed incorrecta → el header mágico no valida → se rechaza el artefacto.

## DSL

```
etiqueta:                 # label
rN = <int>                # IMM (acepta -5, 0x1F, 'A')
rN = rM                   # MOV
rN = rA + rB              # ADD  (+ - * / %)
rN = rA < rB              # LT   (<  >  ==)
rN = rA & rB              # AND  (& | ^ << >>)
rN = [rA]                 # LD  mem[rA]
[rA] = rB                 # ST
goto etiqueta             # JMP
if rN == 0 goto etiqueta  # JZ
if rN != 0 goto etiqueta  # JNZ
read rN                   # IN   (-1 en EOF)
print rN                  # OUTN (entero)
putc rN                   # OUT  (byte)
halt                      # HALT
```

## ISA

`IMM MOV ADD SUB MUL DIV MOD LT EQ GT AND OR XOR SHL SHR LD ST JMP JZ JNZ IN OUT OUTN HALT`
(32 registros i64, memoria lineal de 65536 celdas i64; ambos configurables).

## Estructura

```
nox/
  isa.py        opcodes + firmas
  dsl.py        parser DSL → IR
  codec.py      IR ↔ bytecode ↔ artefacto (cifrado/portador por seed)
  backend_c.py  IR → C → clang -O3
  __main__.py   CLI
examples/       fib.nox, cat.nox
```

## Roadmap

- **Back-end LLVM IR / WASM** (`llc -O3`) — más control y target web.
- Tipos `f64`, llamadas/stack frames, arrays nombrados en el DSL.
- Firmar el artefacto (HMAC) para detectar manipulación, no sólo seed errónea.
- Port del toolchain a Rust (binario único distribuible).

> Nota de seguridad: la ofuscación por seed es *disuasión*, no criptografía de
> grado militar — el keystream es SHA-256-CTR y protege el diccionario, pero
> quien tenga la seed reconstruye el IR completo (`disasm`). Para
> confidencialidad real, trata la seed como secreto y combina con firma HMAC.
