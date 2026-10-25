"""Emit a bounded native POSIX MCP stdio server using the bundled JSON parser."""


def mcp_support():
    """Return C helpers injected after native values and yyjson support.

    The enclosing translation unit must define _POSIX_C_SOURCE=200809L before
    its first system header. Tool calls fork native callbacks; Python is used
    only while compiling the enclosing NOX program.
    """
    return r'''
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <poll.h>
#include <signal.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#ifndef NOX_MCP_TIMEOUT_MS
#define NOX_MCP_TIMEOUT_MS 5000
#endif
#ifndef NOX_MCP_FRAME_LIMIT
#define NOX_MCP_FRAME_LIMIT (1024u * 1024u)
#endif
#ifndef NOX_MCP_OUTPUT_LIMIT
#define NOX_MCP_OUTPUT_LIMIT (1024u * 1024u)
#endif

typedef nox_value (*nox_mcp_callback)(const nox_value *arguments);
typedef struct {
    const nox_string *name;
    const nox_string *description;
    nox_mcp_callback callback;
    yyjson_doc *parameter_document;
    yyjson_val *parameters;
    size_t count;
} nox_mcp_tool;

static nox_mcp_tool nox_mcp_tools[64];
static size_t nox_mcp_tool_count;
static int nox_mcp_cleanup_registered;

static void nox_mcp_cleanup(void) {
    for (size_t i = 0; i < nox_mcp_tool_count; ++i) {
        yyjson_doc_free(nox_mcp_tools[i].parameter_document);
    }
    nox_mcp_tool_count = 0;
}

static int nox_mcp_bytes_equal(const char *a, size_t an, const char *b, size_t bn) {
    return an == bn && (!an || memcmp(a, b, an) == 0);
}

static int nox_mcp_is(yyjson_val *value, const char *text) {
    return yyjson_is_str(value) && nox_mcp_bytes_equal(
        yyjson_get_str(value), yyjson_get_len(value), text, strlen(text));
}

typedef struct { const char *bytes; size_t length; } nox_mcp_key;

static int nox_mcp_compare_keys(const void *left, const void *right) {
    const nox_mcp_key *a = left, *b = right;
    size_t common = a->length < b->length ? a->length : b->length;
    int compared = common ? memcmp(a->bytes, b->bytes, common) : 0;
    if (compared) return compared;
    return (a->length > b->length) - (a->length < b->length);
}

static int nox_mcp_unique(yyjson_val *object) {
    if (!yyjson_is_obj(object)) return 0;
    size_t count = yyjson_obj_size(object);
    if (count < 2) return 1;
    if (count > SIZE_MAX / sizeof(nox_mcp_key)) return 0;
    nox_mcp_key *keys = malloc(count * sizeof(*keys));
    if (!keys) nox_fail("MCP key validation allocation failed");
    yyjson_obj_iter iterator = yyjson_obj_iter_with(object);
    yyjson_val *key;
    size_t position = 0;
    while ((key = yyjson_obj_iter_next(&iterator))) {
        keys[position++] = (nox_mcp_key){yyjson_get_str(key), yyjson_get_len(key)};
    }
    qsort(keys, count, sizeof(*keys), nox_mcp_compare_keys);
    int unique = 1;
    for (size_t i = 1; i < count; ++i) {
        if (nox_mcp_compare_keys(keys + i - 1, keys + i) == 0) { unique = 0; break; }
    }
    free(keys);
    return unique;
}

static yyjson_mut_val *nox_mcp_need(yyjson_mut_val *value) {
    if (!value) nox_fail("MCP response allocation failed");
    return value;
}

static yyjson_mut_doc *nox_mcp_document(void) {
    yyjson_mut_doc *document = yyjson_mut_doc_new(NULL);
    if (!document) nox_fail("MCP response allocation failed");
    return document;
}

static void nox_mcp_add(yyjson_mut_doc *document, yyjson_mut_val *object,
                        const char *name, yyjson_mut_val *value) {
    if (!yyjson_mut_obj_add(object, nox_mcp_need(yyjson_mut_str(document, name)),
                            nox_mcp_need(value))) nox_fail("MCP response allocation failed");
}

static void nox_mcp_append(yyjson_mut_val *array, yyjson_mut_val *value) {
    if (!yyjson_mut_arr_append(array, nox_mcp_need(value))) {
        nox_fail("MCP response allocation failed");
    }
}

static yyjson_mut_val *nox_mcp_string(yyjson_mut_doc *document,
                                      const char *text, size_t length) {
    return nox_mcp_need(yyjson_mut_strncpy(document, text, length));
}

static yyjson_mut_val *nox_mcp_tool_result(yyjson_mut_doc *document,
                                           const char *text, size_t length, int error) {
    yyjson_mut_val *result = nox_mcp_need(yyjson_mut_obj(document));
    yyjson_mut_val *content = nox_mcp_need(yyjson_mut_arr(document));
    yyjson_mut_val *item = nox_mcp_need(yyjson_mut_obj(document));
    nox_mcp_add(document, item, "type", yyjson_mut_str(document, "text"));
    nox_mcp_add(document, item, "text", nox_mcp_string(document, text, length));
    nox_mcp_append(content, item);
    nox_mcp_add(document, result, "content", content);
    nox_mcp_add(document, result, "isError", yyjson_mut_bool(document, error != 0));
    return result;
}

static void nox_mcp_reply(yyjson_mut_doc *document, yyjson_val *id,
                          yyjson_mut_val *result, int code, const char *message,
                          int tool_response) {
    yyjson_mut_val *envelope = nox_mcp_need(yyjson_mut_obj(document));
    yyjson_mut_doc_set_root(document, envelope);
    nox_mcp_add(document, envelope, "jsonrpc", yyjson_mut_str(document, "2.0"));
    nox_mcp_add(document, envelope, "id", id ? yyjson_val_mut_copy(document, id)
                                            : yyjson_mut_null(document));
    if (code) {
        yyjson_mut_val *error = nox_mcp_need(yyjson_mut_obj(document));
        nox_mcp_add(document, error, "code", yyjson_mut_sint(document, code));
        nox_mcp_add(document, error, "message", yyjson_mut_str(document, message));
        nox_mcp_add(document, envelope, "error", error);
    } else nox_mcp_add(document, envelope, "result", result);
    size_t length = 0;
    char *encoded = yyjson_mut_write(document, 0, &length);
    if (!encoded) nox_fail("MCP response encoding failed");
    if (length > NOX_MCP_FRAME_LIMIT) {
        free(encoded);
        yyjson_mut_doc_free(document);
        if (code == -32603) {
            fputs("{\"jsonrpc\":\"2.0\",\"id\":null,\"error\":{\"code\":-32603,"
                  "\"message\":\"Response exceeds limit\"}}\n", stdout);
            fflush(stdout);
            return;
        }
        yyjson_mut_doc *fallback = nox_mcp_document();
        if (tool_response) {
            const char *text = "Tool output exceeds response limit";
            nox_mcp_reply(fallback, id,
                nox_mcp_tool_result(fallback, text, strlen(text), 1), 0, NULL, 0);
        } else nox_mcp_reply(fallback, id, NULL, -32603, "Response exceeds limit", 0);
        return;
    }
    if (fwrite(encoded, 1, length, stdout) != length || fputc('\n', stdout) == EOF ||
            fflush(stdout) == EOF) {
        free(encoded);
        yyjson_mut_doc_free(document);
        nox_fail("MCP transport write failed");
    }
    free(encoded);
    yyjson_mut_doc_free(document);
}

static void nox_mcp_error(yyjson_val *id, int code, const char *message) {
    nox_mcp_reply(nox_mcp_document(), id, NULL, code, message, 0);
}

static void nox_mcp_register(nox_value name_value, nox_mcp_callback callback,
                             nox_value parameter_names, nox_value description_value) {
    const nox_string *name = nox_text(name_value, "MCPTOOL");
    const nox_string *description = nox_text(description_value, "MCPTOOL");
    const nox_string *names = nox_text(parameter_names, "MCPTOOL");
    if (!name->length || name->length > 128 || !callback ||
            memchr(description->bytes, 0, description->length)) {
        nox_fail("invalid MCP tool registration");
    }
    for (size_t i = 0; i < name->length; ++i) {
        unsigned char c = name->bytes[i];
        if (!((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
              (c >= '0' && c <= '9') || c == '_' || c == '.' || c == '-')) {
            nox_fail("invalid MCP tool name");
        }
    }
    if (nox_mcp_tool_count == 64) nox_fail("MCP tool limit exceeded (64)");
    for (size_t i = 0; i < nox_mcp_tool_count; ++i) {
        const nox_string *other = nox_mcp_tools[i].name;
        if (nox_mcp_bytes_equal((const char *)name->bytes, name->length,
                                 (const char *)other->bytes, other->length)) {
            nox_fail("duplicate MCP tool name");
        }
    }
    yyjson_doc *document = nox_json_read((const char *)names->bytes, names->length);
    yyjson_val *parameters = document ? yyjson_doc_get_root(document) : NULL;
    if (!yyjson_is_arr(parameters) || yyjson_arr_size(parameters) > 256) {
        yyjson_doc_free(document);
        nox_fail("invalid MCP parameter metadata");
    }
    size_t count = yyjson_arr_size(parameters);
    for (size_t i = 0; i < count; ++i) {
        yyjson_val *parameter = yyjson_arr_get(parameters, i);
        if (!yyjson_is_str(parameter) ||
                memchr(yyjson_get_str(parameter), 0, yyjson_get_len(parameter))) {
            yyjson_doc_free(document);
            nox_fail("invalid MCP parameter name");
        }
        for (size_t j = 0; j < i; ++j) {
            yyjson_val *other = yyjson_arr_get(parameters, j);
            if (nox_mcp_bytes_equal(yyjson_get_str(parameter), yyjson_get_len(parameter),
                                     yyjson_get_str(other), yyjson_get_len(other))) {
                yyjson_doc_free(document);
                nox_fail("duplicate MCP parameter name");
            }
        }
    }
    if (!nox_mcp_cleanup_registered) {
        if (atexit(nox_mcp_cleanup)) {
            yyjson_doc_free(document);
            nox_fail("cannot register MCP cleanup");
        }
        nox_mcp_cleanup_registered = 1;
    }
    nox_mcp_tools[nox_mcp_tool_count++] = (nox_mcp_tool){
        name, description, callback, document, parameters, count
    };
}

static yyjson_mut_val *nox_mcp_schema(yyjson_mut_doc *document, nox_mcp_tool *tool) {
    yyjson_mut_val *schema = nox_mcp_need(yyjson_mut_obj(document));
    yyjson_mut_val *properties = nox_mcp_need(yyjson_mut_obj(document));
    yyjson_mut_val *required = nox_mcp_need(yyjson_mut_arr(document));
    nox_mcp_add(document, schema, "type", yyjson_mut_str(document, "object"));
    for (size_t i = 0; i < tool->count; ++i) {
        yyjson_val *name = yyjson_arr_get(tool->parameters, i);
        yyjson_mut_val *property = nox_mcp_need(yyjson_mut_obj(document));
        yyjson_mut_val *types = nox_mcp_need(yyjson_mut_arr(document));
        nox_mcp_append(types, yyjson_mut_str(document, "integer"));
        nox_mcp_append(types, yyjson_mut_str(document, "string"));
        nox_mcp_add(document, property, "type", types);
        nox_mcp_add(document, property, "minimum", yyjson_mut_sint(document, INT64_MIN));
        nox_mcp_add(document, property, "maximum", yyjson_mut_sint(document, INT64_MAX));
        if (!yyjson_mut_obj_add(properties,
                nox_mcp_string(document, yyjson_get_str(name), yyjson_get_len(name)), property)) {
            nox_fail("MCP schema allocation failed");
        }
        nox_mcp_append(required,
            nox_mcp_string(document, yyjson_get_str(name), yyjson_get_len(name)));
    }
    nox_mcp_add(document, schema, "properties", properties);
    nox_mcp_add(document, schema, "required", required);
    nox_mcp_add(document, schema, "additionalProperties", yyjson_mut_bool(document, false));
    return schema;
}

static yyjson_mut_val *nox_mcp_list(yyjson_mut_doc *document) {
    yyjson_mut_val *result = nox_mcp_need(yyjson_mut_obj(document));
    yyjson_mut_val *tools = nox_mcp_need(yyjson_mut_arr(document));
    for (size_t i = 0; i < nox_mcp_tool_count; ++i) {
        nox_mcp_tool *tool = nox_mcp_tools + i;
        yyjson_mut_val *item = nox_mcp_need(yyjson_mut_obj(document));
        nox_mcp_add(document, item, "name", nox_mcp_string(document,
            (const char *)tool->name->bytes, tool->name->length));
        nox_mcp_add(document, item, "description", nox_mcp_string(document,
            (const char *)tool->description->bytes, tool->description->length));
        nox_mcp_add(document, item, "inputSchema", nox_mcp_schema(document, tool));
        nox_mcp_append(tools, item);
    }
    nox_mcp_add(document, result, "tools", tools);
    return result;
}

static int nox_mcp_integer_argument(yyjson_val *value) {
    return yyjson_is_sint(value) || (yyjson_is_uint(value) && yyjson_get_uint(value) <= INT64_MAX);
}

static int nox_mcp_arguments(nox_mcp_tool *tool, yyjson_val *arguments) {
    if (!arguments) return tool->count == 0;
    if (!nox_mcp_unique(arguments) || yyjson_obj_size(arguments) != tool->count) return 0;
    for (size_t i = 0; i < tool->count; ++i) {
        yyjson_val *name = yyjson_arr_get(tool->parameters, i);
        yyjson_val *value = yyjson_obj_getn(arguments, yyjson_get_str(name), yyjson_get_len(name));
        if (!yyjson_is_str(value) && !nox_mcp_integer_argument(value)) return 0;
    }
    return 1;
}

static int64_t nox_mcp_milliseconds(void) {
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now)) nox_fail("MCP clock failed");
    return (int64_t)now.tv_sec * 1000 + now.tv_nsec / 1000000;
}

static void nox_mcp_reap(pid_t child, int kill_child) {
    if (kill_child) kill(child, SIGKILL);
    while (waitpid(child, NULL, 0) < 0 && errno == EINTR) { }
}

static void nox_mcp_child(int output, nox_mcp_tool *tool, yyjson_val *arguments) {
    int nullfd = open("/dev/null", O_RDWR);
    if (nullfd < 0 || dup2(nullfd, STDIN_FILENO) < 0 ||
            dup2(nullfd, STDOUT_FILENO) < 0) _exit(4);
    if (nullfd > STDERR_FILENO && nullfd != output) close(nullfd);
    nox_value values[256] = {0};
    for (size_t i = 0; i < tool->count; ++i) {
        yyjson_val *name = yyjson_arr_get(tool->parameters, i);
        yyjson_val *value = yyjson_obj_getn(arguments, yyjson_get_str(name), yyjson_get_len(name));
        if (yyjson_is_str(value)) {
            values[i] = nox_string_copy((const unsigned char *)yyjson_get_str(value),
                                         yyjson_get_len(value));
        } else values[i] = nox_int(yyjson_get_sint(value));
    }
    nox_value returned = tool->callback(values);
    nox_value encoded_value = nox_json_encode(returned);
    const nox_string *encoded = nox_text(encoded_value, "MCP tool result");
    if (encoded->length > NOX_MCP_OUTPUT_LIMIT) _exit(5);
    size_t sent = 0;
    while (sent < encoded->length) {
        ssize_t count = write(output, encoded->bytes + sent, encoded->length - sent);
        if (count < 0 && errno == EINTR) continue;
        if (count <= 0) _exit(4);
        sent += (size_t)count;
    }
    close(output);
    fflush(stderr);
    nox_cleanup();
    _exit(0);
}

static yyjson_mut_val *nox_mcp_invoke(yyjson_mut_doc *response, nox_mcp_tool *tool,
                                      yyjson_val *arguments) {
    int descriptors[2];
    const char *failure = NULL;
    if (pipe(descriptors)) {
        const char *text = "Cannot create tool result pipe";
        return nox_mcp_tool_result(response, text, strlen(text), 1);
    }
    fflush(NULL);
    pid_t child = fork();
    if (child == 0) {
        close(descriptors[0]);
        nox_mcp_child(descriptors[1], tool, arguments);
    }
    close(descriptors[1]);
    if (child < 0) {
        close(descriptors[0]);
        const char *text = "Cannot start tool process";
        return nox_mcp_tool_result(response, text, strlen(text), 1);
    }
    char *bytes = malloc((size_t)NOX_MCP_OUTPUT_LIMIT + 1);
    if (!bytes) {
        close(descriptors[0]);
        nox_mcp_reap(child, 1);
        nox_fail("MCP tool output allocation failed");
    }
    size_t used = 0;
    int64_t deadline = nox_mcp_milliseconds() + NOX_MCP_TIMEOUT_MS;
    for (;;) {
        int64_t remaining = deadline - nox_mcp_milliseconds();
        if (remaining <= 0) { failure = "Tool execution timed out"; break; }
        struct pollfd descriptor = {descriptors[0], POLLIN | POLLHUP, 0};
        int ready = poll(&descriptor, 1, remaining > INT_MAX ? INT_MAX : (int)remaining);
        if (ready < 0 && errno == EINTR) continue;
        if (ready < 0) { failure = "Tool result read failed"; break; }
        if (!ready) { failure = "Tool execution timed out"; break; }
        ssize_t count = read(descriptors[0], bytes + used,
                               (size_t)NOX_MCP_OUTPUT_LIMIT + 1 - used);
        if (count < 0 && errno == EINTR) continue;
        if (count < 0) { failure = "Tool result read failed"; break; }
        if (!count) break;
        used += (size_t)count;
        if (used > NOX_MCP_OUTPUT_LIMIT) { failure = "Tool output exceeds limit"; break; }
    }
    close(descriptors[0]);
    int status = 0;
    if (!failure) {
        for (;;) {
            pid_t waited = waitpid(child, &status, WNOHANG);
            if (waited == child) break;
            if (waited < 0 && errno == EINTR) continue;
            if (waited < 0) { failure = "Cannot collect tool process"; break; }
            if (nox_mcp_milliseconds() >= deadline) {
                failure = "Tool execution timed out"; break;
            }
            poll(NULL, 0, 1);
        }
    }
    if (failure) {
        nox_mcp_reap(child, 1);
        free(bytes);
        return nox_mcp_tool_result(response, failure, strlen(failure), 1);
    }
    if (!WIFEXITED(status) || WEXITSTATUS(status) != 0 || !used) {
        free(bytes);
        const char *text = WIFEXITED(status) && WEXITSTATUS(status) == 5
                             ? "Tool output exceeds limit" : "Tool execution failed";
        return nox_mcp_tool_result(response, text, strlen(text), 1);
    }
    yyjson_doc *returned = nox_json_read(bytes, used);
    yyjson_val *value = returned ? yyjson_doc_get_root(returned) : NULL;
    yyjson_mut_val *result;
    if (yyjson_is_str(value)) {
        result = nox_mcp_tool_result(response, yyjson_get_str(value), yyjson_get_len(value), 0);
    } else if (nox_mcp_integer_argument(value)) {
        char integer[32];
        int length = snprintf(integer, sizeof integer, "%lld", (long long)yyjson_get_sint(value));
        result = nox_mcp_tool_result(response, integer, (size_t)length, 0);
    } else {
        const char *text = "Tool returned an invalid result";
        result = nox_mcp_tool_result(response, text, strlen(text), 1);
    }
    yyjson_doc_free(returned);
    free(bytes);
    return result;
}

static yyjson_mut_val *nox_mcp_initialize(yyjson_mut_doc *document, yyjson_val *requested) {
    const char *version = "2025-11-25";
    const char *supported[] = {"2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"};
    for (size_t i = 0; i < sizeof supported / sizeof supported[0]; ++i) {
        if (nox_mcp_is(requested, supported[i])) version = supported[i];
    }
    yyjson_mut_val *result = nox_mcp_need(yyjson_mut_obj(document));
    yyjson_mut_val *capabilities = nox_mcp_need(yyjson_mut_obj(document));
    yyjson_mut_val *tools = nox_mcp_need(yyjson_mut_obj(document));
    yyjson_mut_val *info = nox_mcp_need(yyjson_mut_obj(document));
    nox_mcp_add(document, tools, "listChanged", yyjson_mut_bool(document, false));
    nox_mcp_add(document, capabilities, "tools", tools);
    nox_mcp_add(document, info, "name", yyjson_mut_str(document, "nox"));
    nox_mcp_add(document, info, "version", yyjson_mut_str(document, "0.5.0"));
    nox_mcp_add(document, result, "protocolVersion", yyjson_mut_str(document, version));
    nox_mcp_add(document, result, "capabilities", capabilities);
    nox_mcp_add(document, result, "serverInfo", info);
    return result;
}

static void nox_mcp_message(const char *bytes, size_t length, int *phase) {
    /* The shared strict parser preserves duplicates for envelope/argument
       validation while enforcing the 1MiB and depth128 input bounds. */
    yyjson_doc *request = nox_json_read(bytes, length);
    if (!request) { nox_mcp_error(NULL, -32700, "Parse error"); return; }
    yyjson_val *root = yyjson_doc_get_root(request);
    if (!nox_mcp_unique(root)) {
        nox_mcp_error(NULL, -32600, "Invalid request");
        yyjson_doc_free(request);
        return;
    }
    yyjson_val *id = yyjson_obj_get(root, "id");
    yyjson_val *method = yyjson_obj_get(root, "method");
    yyjson_val *parameters = yyjson_obj_get(root, "params");
    if (!nox_mcp_is(yyjson_obj_get(root, "jsonrpc"), "2.0") || !yyjson_is_str(method) ||
            (id && !yyjson_is_str(id) && !yyjson_is_int(id)) ||
            (parameters && !yyjson_is_obj(parameters) && !yyjson_is_arr(parameters)) ||
            yyjson_obj_get(root, "result") || yyjson_obj_get(root, "error")) {
        nox_mcp_error((id && (yyjson_is_str(id) || yyjson_is_int(id))) ? id : NULL,
                        -32600, "Invalid request");
        yyjson_doc_free(request);
        return;
    }
    if (!id) {
        if (*phase == 1 && nox_mcp_is(method, "notifications/initialized") &&
                (!parameters || nox_mcp_unique(parameters))) *phase = 2;
        yyjson_doc_free(request);
        return;
    }
    int valid_parameters = !parameters || nox_mcp_unique(parameters);
    if (nox_mcp_is(method, "initialize")) {
        yyjson_val *client = yyjson_obj_get(parameters, "clientInfo");
        yyjson_val *version = yyjson_obj_get(parameters, "protocolVersion");
        if (*phase != 0) nox_mcp_error(id, -32600, "Already initialized");
        else if (!valid_parameters || !yyjson_is_str(version) || !nox_mcp_unique(client) ||
                !yyjson_is_str(yyjson_obj_get(client, "name")) ||
                !yyjson_is_str(yyjson_obj_get(client, "version")) ||
                !nox_mcp_unique(yyjson_obj_get(parameters, "capabilities"))) {
            nox_mcp_error(id, -32602, "Invalid initialize parameters");
        } else {
            yyjson_mut_doc *response = nox_mcp_document();
            nox_mcp_reply(response, id, nox_mcp_initialize(response, version), 0, NULL, 0);
            *phase = 1;
        }
    } else if (nox_mcp_is(method, "ping")) {
        if (!valid_parameters) nox_mcp_error(id, -32602, "Invalid parameters");
        else {
            yyjson_mut_doc *response = nox_mcp_document();
            nox_mcp_reply(response, id, yyjson_mut_obj(response), 0, NULL, 0);
        }
    } else if (nox_mcp_is(method, "tools/list") || nox_mcp_is(method, "tools/call")) {
        if (*phase != 2) nox_mcp_error(id, -32002, "Server is not initialized");
        else if (!valid_parameters) nox_mcp_error(id, -32602, "Invalid parameters");
        else if (nox_mcp_is(method, "tools/list")) {
            yyjson_val *cursor = yyjson_obj_get(parameters, "cursor");
            if (cursor && (!yyjson_is_str(cursor) || yyjson_get_len(cursor))) {
                nox_mcp_error(id, -32602, "Pagination cursor is not supported");
            } else {
                yyjson_mut_doc *response = nox_mcp_document();
                nox_mcp_reply(response, id, nox_mcp_list(response), 0, NULL, 0);
            }
        } else {
            yyjson_val *name = yyjson_obj_get(parameters, "name");
            yyjson_val *arguments = yyjson_obj_get(parameters, "arguments");
            nox_mcp_tool *tool = NULL;
            for (size_t i = 0; i < nox_mcp_tool_count; ++i) {
                if (yyjson_is_str(name) && nox_mcp_bytes_equal(
                    yyjson_get_str(name), yyjson_get_len(name),
                    (const char *)nox_mcp_tools[i].name->bytes, nox_mcp_tools[i].name->length)) {
                    tool = nox_mcp_tools + i; break;
                }
            }
            if (!tool || !nox_mcp_arguments(tool, arguments)) {
                nox_mcp_error(id, -32602, "Unknown tool or invalid arguments");
            } else {
                yyjson_mut_doc *response = nox_mcp_document();
                nox_mcp_reply(response, id, nox_mcp_invoke(response, tool, arguments),
                                 0, NULL, 1);
            }
        }
    } else nox_mcp_error(id, -32601, "Method not found");
    yyjson_doc_free(request);
}

static void nox_mcp_serve(void) {
    char *frame = malloc((size_t)NOX_MCP_FRAME_LIMIT + 1);
    if (!frame) nox_fail("MCP frame allocation failed");
    size_t used = 0;
    int oversized = 0, phase = 0;
    for (;;) {
        int byte = fgetc(stdin);
        if (byte == EOF && !used && !oversized) break;
        if (byte == '\n' || byte == EOF) {
            if (oversized) nox_mcp_error(NULL, -32700, "Frame exceeds limit");
            else nox_mcp_message(frame, used, &phase);
            used = 0;
            oversized = 0;
            if (byte == EOF) break;
        } else if (used < NOX_MCP_FRAME_LIMIT) frame[used++] = (char)byte;
        else oversized = 1;
    }
    free(frame);
    nox_mcp_cleanup();
}
'''
