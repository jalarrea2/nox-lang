# Vendored yyjson

NOX vendors the unmodified upstream yyjson 0.13.0 header, implementation, and
MIT license for its native JSON and MCP runtime. The generated executable uses
C code only; yyjson introduces no Python runtime dependency.

- Upstream: <https://github.com/ibireme/yyjson>
- Release: <https://github.com/ibireme/yyjson/releases/tag/0.13.0>
- Tag: `0.13.0`
- Pinned commit: `6447536015f3d600f3d65323b10976103b337ca7`
- Annotated tag object: `138b4a7e757426bbbaa11ca37f257ce8d8489007`
- License: MIT; the complete upstream text is preserved in `LICENSE`.

## Files and integrity

The following SHA-256 hashes identify the exact upstream bytes:

| Local file | Upstream path | Bytes | SHA-256 |
| --- | --- | ---: | --- |
| `yyjson.h` | `src/yyjson.h` | 330896 | `ef803cda5c06b8962face6dfa39c3284b3bcf3e73f4d7317664690cc779679ca` |
| `yyjson.c` | `src/yyjson.c` | 424765 | `d2d58ef0a3b2267862dc363832c4f3185fd375aea933d5d3419d1a940d6ece2e` |
| `LICENSE` | `LICENSE` | 1084 | `45e384d3d52c73cba3a64d6e6c25d47cd738cd8a55c30629e3201046eda62947` |

Each file was fetched from its commit-pinned URL:

```text
https://raw.githubusercontent.com/ibireme/yyjson/6447536015f3d600f3d65323b10976103b337ca7/src/yyjson.h
https://raw.githubusercontent.com/ibireme/yyjson/6447536015f3d600f3d65323b10976103b337ca7/src/yyjson.c
https://raw.githubusercontent.com/ibireme/yyjson/6447536015f3d600f3d65323b10976103b337ca7/LICENSE
```

## Integration and updates

The checked-in upstream files must remain byte-for-byte unchanged. NOX's C
emitter reads the header and implementation from this directory and removes
the local `#include "yyjson.h"` only from the generated combined C translation
unit. Runtime policy and resource limits belong in the NOX wrapper.

When updating yyjson, select an official release, pin the full commit, retain
the upstream license and copyright notices, and update all three recorded
hashes. Run the JSON, MCP, native sanitizer, and installed-package checks after
the update.
