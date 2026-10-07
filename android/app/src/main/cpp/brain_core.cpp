// Firely, the app's own thinking AI: runs a small language model (with eyes) on the phone
// through llama.cpp. Plain C entry points, so the same code runs on Android (brain_jni.cpp)
// and on a computer through Python ctypes (engine/brain.py) for testing.

#include "llama.h"
#include "mtmd.h"
#include "mtmd-helper.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <mutex>
#include <string>
#include <vector>

namespace {

struct Brain {
    llama_model * model = nullptr;
    llama_context * ctx = nullptr;
    const llama_vocab * vocab = nullptr;
    mtmd_context * vision = nullptr;
    std::string tmpl;
    std::mutex lock;
};

void set_err(char * err, int len, const std::string & msg) {
    if (err && len > 0) std::snprintf(err, (size_t) len, "%s", msg.c_str());
}

// ChatML, used when the model's own template isn't one llama.cpp knows without Jinja.
const char * CHATML = "chatml";
const char * IMAGE_TAG = "<image>";  // where engine/brain.py puts each picture in a message

bool format_chat(Brain * b, const char ** roles, const char ** contents, int n_msgs, std::string & out) {
    std::vector<llama_chat_message> msgs;
    size_t chars = 0;
    for (int i = 0; i < n_msgs; i++) {
        msgs.push_back({roles[i], contents[i]});
        chars += std::strlen(contents[i]) + 32;
    }
    std::vector<char> buf(chars * 2 + 256);
    int32_t n = llama_chat_apply_template(b->tmpl.c_str(), msgs.data(), msgs.size(), true, buf.data(), (int32_t) buf.size());
    if (n < 0 && b->tmpl != CHATML) {
        b->tmpl = CHATML;
        n = llama_chat_apply_template(b->tmpl.c_str(), msgs.data(), msgs.size(), true, buf.data(), (int32_t) buf.size());
    }
    if (n < 0) return false;
    if ((size_t) n > buf.size()) {
        buf.resize((size_t) n + 1);
        n = llama_chat_apply_template(b->tmpl.c_str(), msgs.data(), msgs.size(), true, buf.data(), (int32_t) buf.size());
    }
    out.assign(buf.data(), (size_t) n);
    return true;
}

// Reads the conversation (and pictures) into the model. Sets n_past to the next position.
bool read_prompt(Brain * b, std::string prompt, const char ** images, int n_images, int max_tokens,
                 llama_pos & n_past, char * err, int errlen) {
    const int n_ctx = (int) llama_n_ctx(b->ctx);
    llama_memory_clear(llama_get_memory(b->ctx), true);

    if (n_images > 0) {
        if (!b->vision) {
            set_err(err, errlen, "looking at pictures isn't set up");
            return false;
        }
        const std::string marker = mtmd_default_marker();
        for (size_t p = 0; (p = prompt.find(IMAGE_TAG, p)) != std::string::npos; p += marker.size()) {
            prompt.replace(p, std::strlen(IMAGE_TAG), marker);
        }
        std::vector<mtmd_bitmap *> bitmaps;
        auto free_all = [&] { for (auto * bm : bitmaps) mtmd_bitmap_free(bm); };
        for (int i = 0; i < n_images; i++) {
            mtmd_helper_bitmap_wrapper w = mtmd_helper_bitmap_init_from_file(b->vision, images[i], false,
                                                                              mtmd_helper_init_opt_default());
            if (!w.bitmap) {
                free_all();
                set_err(err, errlen, std::string("couldn't open picture ") + images[i]);
                return false;
            }
            bitmaps.push_back(w.bitmap);
        }
        mtmd_input_text text = {prompt.c_str(), prompt.size(), true, true};
        mtmd_input_chunks * chunks = mtmd_input_chunks_init();
        int32_t rc = mtmd_tokenize(b->vision, chunks, &text, (const mtmd_bitmap **) bitmaps.data(), bitmaps.size());
        free_all();
        if (rc != 0) {
            mtmd_input_chunks_free(chunks);
            set_err(err, errlen, "the pictures don't match the request");
            return false;
        }
        if ((int) mtmd_helper_get_n_tokens(chunks) + max_tokens > n_ctx) {
            mtmd_input_chunks_free(chunks);
            set_err(err, errlen, "too long");
            return false;
        }
        rc = mtmd_helper_eval_chunks(b->vision, b->ctx, chunks, 0, 0, 512, true, &n_past);
        mtmd_input_chunks_free(chunks);
        if (rc != 0) {
            set_err(err, errlen, "the AI model couldn't look at the pictures");
            return false;
        }
        return true;
    }

    int32_t n_tok = -llama_tokenize(b->vocab, prompt.c_str(), (int32_t) prompt.size(), nullptr, 0, true, true);
    std::vector<llama_token> tokens((size_t) std::max(n_tok, 1));
    n_tok = llama_tokenize(b->vocab, prompt.c_str(), (int32_t) prompt.size(), tokens.data(), (int32_t) tokens.size(), true, true);
    if (n_tok < 0) {
        set_err(err, errlen, "couldn't read the conversation");
        return false;
    }
    if (n_tok + max_tokens > n_ctx) {
        set_err(err, errlen, "too long");
        return false;
    }
    for (int i = 0; i < n_tok; i += 512) {
        int32_t step = std::min(512, n_tok - i);
        if (llama_decode(b->ctx, llama_batch_get_one(tokens.data() + i, step)) != 0) {
            set_err(err, errlen, "the AI model couldn't read the request");
            return false;
        }
    }
    n_past = n_tok;
    return true;
}

}  // namespace

extern "C" {

void * brain_load(const char * path, int n_threads, int n_ctx, char * err, int errlen) {
    static std::once_flag once;
    std::call_once(once, [] {
        auto quiet = [](ggml_log_level level, const char * text, void *) {
            if (level >= GGML_LOG_LEVEL_ERROR) std::fputs(text, stderr);
        };
        llama_log_set(quiet, nullptr);
        mtmd_helper_log_set(quiet, nullptr);
        llama_backend_init();
    });

    llama_model_params mp = llama_model_default_params();
    mp.n_gpu_layers = 0;
    llama_model * model = llama_model_load_from_file(path, mp);
    if (!model) {
        set_err(err, errlen, std::string("couldn't load the AI model from ") + path);
        return nullptr;
    }
    llama_context_params cp = llama_context_default_params();
    cp.n_ctx = (uint32_t) n_ctx;
    cp.n_batch = 512;
    cp.n_ubatch = 512;
    cp.n_threads = n_threads;
    cp.n_threads_batch = n_threads;
    cp.no_perf = true;
    // Half-size memory for the conversation (8-bit instead of 16-bit); needs flash attention.
    cp.flash_attn_type = LLAMA_FLASH_ATTN_TYPE_ENABLED;
    cp.type_k = GGML_TYPE_Q8_0;
    cp.type_v = GGML_TYPE_Q8_0;
    llama_context * ctx = llama_init_from_model(model, cp);
    if (!ctx) {  // older/odd hardware: fall back to the plain settings
        cp.flash_attn_type = LLAMA_FLASH_ATTN_TYPE_AUTO;
        cp.type_k = GGML_TYPE_F16;
        cp.type_v = GGML_TYPE_F16;
        ctx = llama_init_from_model(model, cp);
    }
    if (!ctx) {
        llama_model_free(model);
        set_err(err, errlen, "not enough memory to start the AI model");
        return nullptr;
    }
    auto * b = new Brain();
    b->model = model;
    b->ctx = ctx;
    b->vocab = llama_model_get_vocab(model);
    const char * t = llama_model_chat_template(model, nullptr);
    b->tmpl = t ? t : CHATML;
    return b;
}

// Adds eyes: the vision part of the model (mmproj file). Returns 1 on success.
int brain_load_vision(void * handle, const char * mmproj_path, int n_threads, char * err, int errlen) {
    auto * b = static_cast<Brain *>(handle);
    if (!b) return 0;
    std::lock_guard<std::mutex> guard(b->lock);
    if (b->vision) return 1;
    mtmd_context_params p = mtmd_context_params_default();
    p.use_gpu = false;
    p.n_threads = n_threads;
    p.print_timings = false;
    p.warmup = false;
    p.image_max_tokens = 256;  // small frames are enough to say what's in them, and much faster
    b->vision = mtmd_init_from_file(mmproj_path, b->model, p);
    if (!b->vision || !mtmd_support_vision(b->vision)) {
        set_err(err, errlen, std::string("couldn't load the vision model from ") + mmproj_path);
        if (b->vision) mtmd_free(b->vision);
        b->vision = nullptr;
        return 0;
    }
    return 1;
}

// Writes the model's answer to `out` (NUL-terminated). Returns its length, or -1 on error (see `err`).
// `grammar` (GBNF, may be empty) forces the answer into a fixed shape, e.g. valid JSON.
// `images` are picture files; each "<image>" in the messages marks where one goes.
int brain_generate(void * handle, const char ** roles, const char ** contents, int n_msgs,
                   const char ** images, int n_images, const char * grammar, int max_tokens,
                   float temperature, unsigned int seed, char * out, int outlen, char * err, int errlen) {
    auto * b = static_cast<Brain *>(handle);
    if (!b) {
        set_err(err, errlen, "the AI model isn't loaded");
        return -1;
    }
    std::lock_guard<std::mutex> guard(b->lock);

    std::string prompt;
    if (!format_chat(b, roles, contents, n_msgs, prompt)) {
        set_err(err, errlen, "couldn't format the conversation for the AI model");
        return -1;
    }
    llama_pos n_past = 0;
    if (!read_prompt(b, prompt, images, n_images, max_tokens, n_past, err, errlen)) return -1;

    // Pick a word normally, and only if the answer shape (grammar) forbids it, pick again among
    // the allowed words. Checking the shape for every word each step is much slower.
    llama_sampler * grmr = nullptr;
    if (grammar && grammar[0]) {
        grmr = llama_sampler_init_grammar(b->vocab, grammar, "root");
        if (!grmr) {
            set_err(err, errlen, "bad answer shape (grammar)");
            return -1;
        }
    }
    llama_sampler * smpl = llama_sampler_chain_init(llama_sampler_chain_default_params());
    if (temperature > 0.0f) {
        llama_sampler_chain_add(smpl, llama_sampler_init_top_k(40));
        llama_sampler_chain_add(smpl, llama_sampler_init_top_p(0.95f, 1));
        llama_sampler_chain_add(smpl, llama_sampler_init_temp(temperature));
        llama_sampler_chain_add(smpl, llama_sampler_init_dist(seed));
    } else {
        llama_sampler_chain_add(smpl, llama_sampler_init_greedy());
    }

    const int n_ctx = (int) llama_n_ctx(b->ctx);
    const int n_vocab = llama_vocab_n_tokens(b->vocab);
    std::vector<llama_token_data> cand((size_t) n_vocab);
    auto fill = [&](llama_token_data_array & arr) {
        const float * logits = llama_get_logits_ith(b->ctx, -1);
        for (llama_token t = 0; t < n_vocab; t++) cand[(size_t) t] = {t, logits[t], 0.0f};
        arr = {cand.data(), cand.size(), -1, false};
    };
    llama_batch next = llama_batch_init(1, 0, 1);

    std::string answer;
    char piece[256];
    int rc = 0;
    for (int i = 0; i < max_tokens; i++) {
        llama_token_data_array arr;
        fill(arr);
        llama_sampler_apply(smpl, &arr);
        llama_token tok = arr.data[arr.selected].id;
        if (grmr) {
            llama_token_data one = {tok, 1.0f, 0.0f};
            llama_token_data_array check = {&one, 1, -1, false};
            llama_sampler_apply(grmr, &check);
            if (check.data[0].logit == -INFINITY) {  // not allowed here: choose among allowed words
                fill(arr);
                llama_sampler_apply(grmr, &arr);
                llama_sampler_apply(smpl, &arr);
                tok = arr.data[arr.selected].id;
            }
            llama_sampler_accept(grmr, tok);
        }
        llama_sampler_accept(smpl, tok);
        if (llama_vocab_is_eog(b->vocab, tok)) break;
        int32_t len = llama_token_to_piece(b->vocab, tok, piece, sizeof(piece), 0, false);
        if (len > 0) answer.append(piece, (size_t) len);
        if (n_past + 1 >= n_ctx) break;
        next.n_tokens = 1;
        next.token[0] = tok;
        next.pos[0] = n_past++;
        next.n_seq_id[0] = 1;
        next.seq_id[0][0] = 0;
        next.logits[0] = 1;
        if (llama_decode(b->ctx, next) != 0) {
            set_err(err, errlen, "the AI model stopped while answering");
            rc = -1;
            break;
        }
    }
    llama_batch_free(next);
    if (grmr) llama_sampler_free(grmr);
    llama_sampler_free(smpl);
    if (rc < 0) return -1;
    if ((int) answer.size() >= outlen) answer.resize((size_t) outlen - 1);
    std::memcpy(out, answer.data(), answer.size());
    out[answer.size()] = '\0';
    return (int) answer.size();
}

int brain_context_size(void * handle) {
    auto * b = static_cast<Brain *>(handle);
    return b ? (int) llama_n_ctx(b->ctx) : 0;
}

void brain_free(void * handle) {
    auto * b = static_cast<Brain *>(handle);
    if (!b) return;
    if (b->vision) mtmd_free(b->vision);
    llama_free(b->ctx);
    llama_model_free(b->model);
    delete b;
}

}  // extern "C"
