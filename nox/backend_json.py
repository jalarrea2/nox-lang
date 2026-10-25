"""Inline pinned yyjson and bounded native JSON operations into generated C."""
from pathlib import Path

_JSON = r"""
#ifndef NOX_JSON_INPUT_LIMIT
#define NOX_JSON_INPUT_LIMIT (1024u * 1024u)
#endif
#ifndef NOX_JSON_DEPTH_LIMIT
#define NOX_JSON_DEPTH_LIMIT 128u
#endif

static yyjson_doc *nox_json_pending_doc;
static yyjson_mut_doc *nox_json_pending_mut_doc;
static char *nox_json_pending_buffer;
static int nox_json_cleanup_registered;

static void nox_json_cleanup(void) {
    if (nox_json_pending_doc) yyjson_doc_free(nox_json_pending_doc);
    if (nox_json_pending_mut_doc) yyjson_mut_doc_free(nox_json_pending_mut_doc);
    free(nox_json_pending_buffer);
    nox_json_pending_doc = NULL;
    nox_json_pending_mut_doc = NULL;
    nox_json_pending_buffer = NULL;
}

static void nox_json_prepare(void) {
    if (!nox_json_cleanup_registered) {
        if (atexit(nox_json_cleanup)) nox_fail("cannot register JSON cleanup");
        nox_json_cleanup_registered = 1;
    }
}

typedef struct {
    yyjson_val *container;
    yyjson_arr_iter array;
    yyjson_obj_iter object;
} nox_json_cursor;

static void nox_json_cursor_init(nox_json_cursor *cursor, yyjson_val *value) {
    cursor->container = value;
    if (yyjson_is_arr(value)) yyjson_arr_iter_init(value, &cursor->array);
    else yyjson_obj_iter_init(value, &cursor->object);
}

static int nox_json_depth_ok(yyjson_val *root) {
    nox_json_cursor cursors[NOX_JSON_DEPTH_LIMIT ? NOX_JSON_DEPTH_LIMIT : 1];
    size_t depth = 0;
    if (!yyjson_is_ctn(root)) return 1;
    if (!NOX_JSON_DEPTH_LIMIT) return 0;
    nox_json_cursor_init(&cursors[depth++], root);
    while (depth) {
        nox_json_cursor *cursor = &cursors[depth - 1];
        yyjson_val *child;
        if (yyjson_is_arr(cursor->container)) child = yyjson_arr_iter_next(&cursor->array);
        else {
            yyjson_val *key = yyjson_obj_iter_next(&cursor->object);
            child = key ? yyjson_obj_iter_get_val(key) : NULL;
        }
        if (!child) { --depth; continue; }
        if (yyjson_is_ctn(child)) {
            if (depth >= NOX_JSON_DEPTH_LIMIT) return 0;
            nox_json_cursor_init(&cursors[depth++], child);
        }
    }
    return 1;
}

/* Documents returned here are caller-owned. Duplicate keys remain visible. */
static yyjson_doc *nox_json_read(const char *bytes, size_t length) {
    if (length > (size_t)NOX_JSON_INPUT_LIMIT) return NULL;
    yyjson_doc *doc = yyjson_read(bytes, length, 0);
    if (doc && !nox_json_depth_ok(yyjson_doc_get_root(doc))) {
        yyjson_doc_free(doc);
        return NULL;
    }
    return doc;
}

static int nox_json_key_compare(const void *a, const void *b) {
    yyjson_val *left = *(yyjson_val *const *)a;
    yyjson_val *right = *(yyjson_val *const *)b;
    size_t alen = yyjson_get_len(left), blen = yyjson_get_len(right);
    size_t common = alen < blen ? alen : blen;
    int order = common ? memcmp(yyjson_get_str(left), yyjson_get_str(right), common) : 0;
    return order ? order : (alen > blen) - (alen < blen);
}

static int nox_json_unique_keys(yyjson_val *value) {
    if (yyjson_is_obj(value)) {
        size_t count = yyjson_obj_size(value), used = 0;
        if (count > SIZE_MAX / sizeof(yyjson_val *)) return 0;
        yyjson_val **keys = count ? malloc(count * sizeof(*keys)) : NULL;
        if (count && !keys) return 0;
        yyjson_obj_iter iterator;
        yyjson_obj_iter_init(value, &iterator);
        yyjson_val *key;
        while ((key = yyjson_obj_iter_next(&iterator))) keys[used++] = key;
        if (count > 1) qsort(keys, count, sizeof(*keys), nox_json_key_compare);
        for (size_t i = 1; i < count; ++i) {
            if (!nox_json_key_compare(&keys[i - 1], &keys[i])) { free(keys); return 0; }
        }
        free(keys);
        yyjson_obj_iter_init(value, &iterator);
        while ((key = yyjson_obj_iter_next(&iterator))) {
            if (!nox_json_unique_keys(yyjson_obj_iter_get_val(key))) return 0;
        }
    } else if (yyjson_is_arr(value)) {
        yyjson_arr_iter iterator;
        yyjson_arr_iter_init(value, &iterator);
        yyjson_val *child;
        while ((child = yyjson_arr_iter_next(&iterator))) {
            if (!nox_json_unique_keys(child)) return 0;
        }
    }
    return 1;
}

static nox_value nox_json_copy_buffer(char *buffer, size_t length) {
    if (!buffer) nox_fail("JSON serialization failed");
    nox_json_prepare();
    nox_json_pending_buffer = buffer;
    nox_value result = nox_string_copy((const unsigned char *)buffer, length);
    free(buffer);
    nox_json_pending_buffer = NULL;
    return result;
}

static nox_value nox_json_fragment(yyjson_val *value) {
    size_t length = 0;
    char *buffer = yyjson_val_write(value, 0, &length);
    return nox_json_copy_buffer(buffer, length);
}

static yyjson_doc *nox_json_require(nox_value value, const char *operation) {
    const nox_string *text = nox_text(value, operation);
    nox_json_prepare();
    yyjson_doc *doc = nox_json_read((const char *)text->bytes, text->length);
    if (!doc) nox_fail("invalid JSON, input too large, or nesting limit exceeded");
    nox_json_pending_doc = doc;
    if (!nox_json_unique_keys(yyjson_doc_get_root(doc))) nox_fail("duplicate JSON object key");
    return doc;
}

static void nox_json_release(yyjson_doc *doc) {
    yyjson_doc_free(doc);
    nox_json_pending_doc = NULL;
}

static nox_value nox_json_encode(nox_value value) {
    nox_json_prepare();
    yyjson_mut_doc *doc = yyjson_mut_doc_new(NULL);
    if (!doc) nox_fail("JSON allocation failed");
    nox_json_pending_mut_doc = doc;
    yyjson_mut_val *root = value.is_string
        ? yyjson_mut_strncpy(doc, (const char *)value.string->bytes, value.string->length)
        : yyjson_mut_sint(doc, value.integer);
    if (!root) nox_fail("JSON allocation failed");
    yyjson_mut_doc_set_root(doc, root);
    size_t length = 0;
    char *buffer = yyjson_mut_write(doc, 0, &length);
    yyjson_mut_doc_free(doc);
    nox_json_pending_mut_doc = NULL;
    return nox_json_copy_buffer(buffer, length);
}

static nox_value nox_json_get(nox_value value, nox_value key_value) {
    const nox_string *key = nox_text(key_value, "JGET");
    yyjson_doc *doc = nox_json_require(value, "JGET");
    yyjson_val *root = yyjson_doc_get_root(doc);
    if (!yyjson_is_obj(root)) nox_fail("JGET requires a JSON object");
    yyjson_val *field = yyjson_obj_getn(root, (const char *)key->bytes, key->length);
    if (!field) nox_fail("JSON object key not found");
    nox_value result = nox_json_fragment(field);
    nox_json_release(doc);
    return result;
}

static nox_value nox_json_at(nox_value value, nox_value index_value) {
    int64_t index = nox_integer(index_value, "JAT");
    yyjson_doc *doc = nox_json_require(value, "JAT");
    yyjson_val *root = yyjson_doc_get_root(doc);
    if (!yyjson_is_arr(root)) nox_fail("JAT requires a JSON array");
    if (index < 0 || (uint64_t)index >= yyjson_arr_size(root)) {
        nox_fail("JSON array index out of bounds");
    }
    nox_value result = nox_json_fragment(yyjson_arr_get(root, (size_t)index));
    nox_json_release(doc);
    return result;
}

static nox_value nox_json_string(nox_value value) {
    yyjson_doc *doc = nox_json_require(value, "JSTR");
    yyjson_val *root = yyjson_doc_get_root(doc);
    if (!yyjson_is_str(root)) nox_fail("JSTR requires a JSON string");
    nox_value result = nox_string_copy((const unsigned char *)yyjson_get_str(root),
                                       yyjson_get_len(root));
    nox_json_release(doc);
    return result;
}

static nox_value nox_json_integer(nox_value value) {
    yyjson_doc *doc = nox_json_require(value, "JINT");
    yyjson_val *root = yyjson_doc_get_root(doc);
    int64_t number;
    if (yyjson_is_sint(root)) number = yyjson_get_sint(root);
    else if (yyjson_is_uint(root) && yyjson_get_uint(root) <= INT64_MAX) {
        number = (int64_t)yyjson_get_uint(root);
    } else nox_fail("JINT requires a JSON signed i64 integer");
    nox_json_release(doc);
    return nox_int(number);
}

static nox_value nox_json_valid(nox_value value) {
    const nox_string *text = nox_text(value, "JVALID");
    yyjson_doc *doc = nox_json_read((const char *)text->bytes, text->length);
    if (!doc) return nox_int(0);
    int valid = nox_json_unique_keys(yyjson_doc_get_root(doc));
    yyjson_doc_free(doc);
    return nox_int(valid);
}
"""


def json_support():
    """Return standalone yyjson source and checked NOX JSON value helpers."""
    vendor = Path(__file__).parent / "_vendor" / "yyjson"
    header = (vendor / "yyjson.h").read_text(encoding="utf-8")
    source = (vendor / "yyjson.c").read_text(encoding="utf-8")
    source = source.replace('#include "yyjson.h"', '/* yyjson.h inlined above. */')
    return header + "\n" + source + "\n" + _JSON
