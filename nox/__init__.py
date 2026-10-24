"""NOX — opaque metalanguage compiled to native code."""
from .backend_c import compile_c, emit_c
from .codec import (
    NoxIntegrityError,
    artifact_to_ir,
    decode,
    deserialize,
    encode,
    ir_to_artifact,
    serialize,
)
from .dsl import NoxSyntaxError, parse

__all__ = ["parse", "NoxSyntaxError", "serialize", "deserialize",
           "encode", "decode", "ir_to_artifact", "artifact_to_ir",
           "NoxIntegrityError", "emit_c", "compile_c"]
__version__ = "0.2.0"
