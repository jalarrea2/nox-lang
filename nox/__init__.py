"""NOX — opaque metalanguage compiled to native code."""
from .dsl import parse, NoxSyntaxError
from .codec import (serialize, deserialize, encode, decode,
                    ir_to_artifact, artifact_to_ir, NoxIntegrityError)
from .backend_c import emit_c, compile_c

__all__ = ["parse", "NoxSyntaxError", "serialize", "deserialize",
           "encode", "decode", "ir_to_artifact", "artifact_to_ir",
           "NoxIntegrityError", "emit_c", "compile_c"]
__version__ = "0.2.0"
