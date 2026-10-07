// Java/Kotlin entry points for brain_core.cpp (see Brain.kt).

#include <jni.h>

#include <string>
#include <vector>

extern "C" {
void * brain_load(const char * path, int n_threads, int n_ctx, char * err, int errlen);
int brain_load_vision(void * handle, const char * mmproj_path, int n_threads, char * err, int errlen);
int brain_generate(void * handle, const char ** roles, const char ** contents, int n_msgs,
                   const char ** images, int n_images, const char * grammar, int max_tokens,
                   float temperature, unsigned int seed, char * out, int outlen, char * err, int errlen);
void brain_free(void * handle);
}

namespace {

thread_local std::string last_error;

std::string str(JNIEnv * env, jstring s) {
    if (!s) return "";
    const char * c = env->GetStringUTFChars(s, nullptr);
    std::string out(c);
    env->ReleaseStringUTFChars(s, c);
    return out;
}

std::vector<std::string> strs(JNIEnv * env, jobjectArray a) {
    std::vector<std::string> out;
    if (!a) return out;
    jsize n = env->GetArrayLength(a);
    for (jsize i = 0; i < n; i++) {
        auto s = (jstring) env->GetObjectArrayElement(a, i);
        out.push_back(str(env, s));
        env->DeleteLocalRef(s);
    }
    return out;
}

// Java strings use "modified UTF-8"; building them from bytes keeps Hindi, Punjabi and emoji intact.
jstring utf8(JNIEnv * env, const std::string & s) {
    jbyteArray bytes = env->NewByteArray((jsize) s.size());
    env->SetByteArrayRegion(bytes, 0, (jsize) s.size(), (const jbyte *) s.data());
    jclass cls = env->FindClass("java/lang/String");
    jmethodID ctor = env->GetMethodID(cls, "<init>", "([BLjava/lang/String;)V");
    jstring enc = env->NewStringUTF("UTF-8");
    auto out = (jstring) env->NewObject(cls, ctor, bytes, enc);
    env->DeleteLocalRef(bytes);
    env->DeleteLocalRef(enc);
    return out;
}

}  // namespace

extern "C" JNIEXPORT jlong JNICALL
Java_com_lovedeep_aivideoeditor_Brain_nativeLoad(JNIEnv * env, jclass, jstring path, jint threads, jint ctx) {
    char err[512] = {0};
    void * h = brain_load(str(env, path).c_str(), threads, ctx, err, sizeof(err));
    if (!h) last_error = err;
    return (jlong) h;
}

extern "C" JNIEXPORT jboolean JNICALL
Java_com_lovedeep_aivideoeditor_Brain_nativeLoadVision(JNIEnv * env, jclass, jlong h, jstring path, jint threads) {
    char err[512] = {0};
    int ok = brain_load_vision((void *) h, str(env, path).c_str(), threads, err, sizeof(err));
    if (!ok) last_error = err;
    return (jboolean) (ok != 0);
}

extern "C" JNIEXPORT jstring JNICALL
Java_com_lovedeep_aivideoeditor_Brain_nativeGenerate(JNIEnv * env, jclass, jlong h, jobjectArray roles,
                                                     jobjectArray contents, jobjectArray images, jstring grammar,
                                                     jint max_tokens, jfloat temperature, jint seed) {
    std::vector<std::string> r = strs(env, roles), c = strs(env, contents), im = strs(env, images);
    std::vector<const char *> rp, cp, ip;
    for (auto & s : r) rp.push_back(s.c_str());
    for (auto & s : c) cp.push_back(s.c_str());
    for (auto & s : im) ip.push_back(s.c_str());
    std::string g = str(env, grammar);
    std::vector<char> out(1 << 16);
    char err[512] = {0};
    int n = brain_generate((void *) h, rp.data(), cp.data(), (int) rp.size(), ip.data(), (int) ip.size(),
                           g.c_str(), max_tokens, temperature, (unsigned int) seed, out.data(), (int) out.size(),
                           err, sizeof(err));
    if (n < 0) {
        last_error = err;
        return nullptr;
    }
    return utf8(env, std::string(out.data(), (size_t) n));
}

extern "C" JNIEXPORT jstring JNICALL
Java_com_lovedeep_aivideoeditor_Brain_nativeError(JNIEnv * env, jclass) {
    return utf8(env, last_error);
}

extern "C" JNIEXPORT void JNICALL
Java_com_lovedeep_aivideoeditor_Brain_nativeFree(JNIEnv *, jclass, jlong h) {
    brain_free((void *) h);
}
