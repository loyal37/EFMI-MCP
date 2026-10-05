// Minimal JSON writer and reader (no external dependencies).
#pragma once
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <string>
#include <utility>
#include <vector>

class JsonW {
public:
	std::string s;
	JsonW() { stack_.reserve(16); }

	void begin_obj() { sep(); s += '{'; stack_.push_back(true); }
	void end_obj()   { s += '}'; stack_.pop_back(); }
	void begin_arr() { sep(); s += '['; stack_.push_back(true); }
	void end_arr()   { s += ']'; stack_.pop_back(); }

	void key(const char* k)
	{
		sep();
		s += '"'; s += k; s += "\":";
		pending_value_ = true;
	}

	void u(uint64_t v) { sep(); char b[32]; snprintf(b, sizeof(b), "%llu", (unsigned long long)v); s += b; }
	void i(int64_t v)  { sep(); char b[32]; snprintf(b, sizeof(b), "%lld", (long long)v); s += b; }
	void d(double v)   { sep(); char b[40]; snprintf(b, sizeof(b), "%.9g", v); s += b; }
	void b(bool v)     { sep(); s += v ? "true" : "false"; }
	void null()        { sep(); s += "null"; }
	void hex32(uint32_t v) { sep(); char b[16]; snprintf(b, sizeof(b), "\"%08x\"", v); s += b; }
	void hex64(uint64_t v) { sep(); char b[24]; snprintf(b, sizeof(b), "\"%016llx\"", (unsigned long long)v); s += b; }
	void ptr(const void* p) { sep(); char b[24]; snprintf(b, sizeof(b), "\"0x%llx\"", (unsigned long long)(uintptr_t)p); s += b; }

	void str(const char* v)
	{
		sep();
		s += '"';
		for (const unsigned char* p = (const unsigned char*)(v ? v : ""); *p; p++) {
			switch (*p) {
			case '"':  s += "\\\""; break;
			case '\\': s += "\\\\"; break;
			case '\n': s += "\\n"; break;
			case '\r': s += "\\r"; break;
			case '\t': s += "\\t"; break;
			default:
				if (*p < 0x20) { char b[8]; snprintf(b, sizeof(b), "\\u%04x", *p); s += b; }
				else s += (char)*p;
			}
		}
		s += '"';
	}
	void str(const std::string& v) { str(v.c_str()); }

	// Inserts a pre-formatted fragment as a value.
	void raw(const std::string& v) { sep(); s += v; }

	void ku(const char* k, uint64_t v) { key(k); u(v); }
	void ki(const char* k, int64_t v)  { key(k); i(v); }
	void kd(const char* k, double v)   { key(k); d(v); }
	void kb(const char* k, bool v)     { key(k); b(v); }
	void ks(const char* k, const char* v) { key(k); str(v); }
	void ks(const char* k, const std::string& v) { key(k); str(v); }
	void kh32(const char* k, uint32_t v) { key(k); hex32(v); }
	void kh64(const char* k, uint64_t v) { key(k); hex64(v); }

private:
	std::vector<bool> stack_;   // "next element is the first one" per open container
	bool pending_value_ = false;

	void sep()
	{
		if (pending_value_) { pending_value_ = false; return; }
		if (stack_.empty()) return;
		if (stack_.back()) stack_.back() = false;
		else s += ',';
	}
};

// ---------------------------------------------------------------- reader
struct JVal {
	enum Type { Null, Bool, Num, Str, Arr, Obj } type = Null;
	bool bval = false;
	double num = 0;
	std::string str;
	std::vector<JVal> arr;
	std::vector<std::pair<std::string, JVal>> obj;

	const JVal* get(const char* k) const
	{
		for (auto& kv : obj)
			if (kv.first == k) return &kv.second;
		return nullptr;
	}
	double num_or(const char* k, double def) const
	{
		const JVal* v = get(k);
		return (v && v->type == Num) ? v->num : def;
	}
	bool bool_or(const char* k, bool def) const
	{
		const JVal* v = get(k);
		if (!v) return def;
		if (v->type == Bool) return v->bval;
		if (v->type == Num) return v->num != 0;
		return def;
	}
	std::string str_or(const char* k, const char* def) const
	{
		const JVal* v = get(k);
		return (v && v->type == Str) ? v->str : std::string(def);
	}
};

class JsonR {
public:
	static bool parse(const char* text, size_t len, JVal* out)
	{
		JsonR r(text, len);
		r.ws();
		if (!r.value(out, 0)) return false;
		r.ws();
		return r.p_ == r.e_;
	}

private:
	const char* p_;
	const char* e_;
	JsonR(const char* t, size_t n) : p_(t), e_(t + n) {}

	void ws() { while (p_ < e_ && (*p_ == ' ' || *p_ == '\t' || *p_ == '\n' || *p_ == '\r')) p_++; }

	bool lit(const char* w)
	{
		size_t n = strlen(w);
		if ((size_t)(e_ - p_) < n || memcmp(p_, w, n)) return false;
		p_ += n;
		return true;
	}

	bool string(std::string* out)
	{
		if (p_ >= e_ || *p_ != '"') return false;
		p_++;
		while (p_ < e_ && *p_ != '"') {
			char c = *p_++;
			if (c != '\\') { *out += c; continue; }
			if (p_ >= e_) return false;
			char x = *p_++;
			switch (x) {
			case 'n': *out += '\n'; break;
			case 't': *out += '\t'; break;
			case 'r': *out += '\r'; break;
			case 'b': *out += '\b'; break;
			case 'f': *out += '\f'; break;
			case 'u': {
				if (e_ - p_ < 4) return false;
				char h[5] = { p_[0], p_[1], p_[2], p_[3], 0 };
				unsigned cp = (unsigned)strtoul(h, nullptr, 16);
				p_ += 4;
				if (cp < 0x80) *out += (char)cp;
				else if (cp < 0x800) { *out += (char)(0xC0 | (cp >> 6)); *out += (char)(0x80 | (cp & 0x3F)); }
				else { *out += (char)(0xE0 | (cp >> 12)); *out += (char)(0x80 | ((cp >> 6) & 0x3F)); *out += (char)(0x80 | (cp & 0x3F)); }
				break;
			}
			default: *out += x;
			}
		}
		if (p_ >= e_) return false;
		p_++;
		return true;
	}

	bool value(JVal* v, int depth)
	{
		if (depth > 32 || p_ >= e_) return false;
		char c = *p_;
		if (c == '{') {
			p_++;
			v->type = JVal::Obj;
			ws();
			if (p_ < e_ && *p_ == '}') { p_++; return true; }
			for (;;) {
				ws();
				std::string k;
				if (!string(&k)) return false;
				ws();
				if (p_ >= e_ || *p_ != ':') return false;
				p_++;
				ws();
				JVal child;
				if (!value(&child, depth + 1)) return false;
				v->obj.emplace_back(std::move(k), std::move(child));
				ws();
				if (p_ < e_ && *p_ == ',') { p_++; continue; }
				if (p_ < e_ && *p_ == '}') { p_++; return true; }
				return false;
			}
		}
		if (c == '[') {
			p_++;
			v->type = JVal::Arr;
			ws();
			if (p_ < e_ && *p_ == ']') { p_++; return true; }
			for (;;) {
				ws();
				JVal child;
				if (!value(&child, depth + 1)) return false;
				v->arr.push_back(std::move(child));
				ws();
				if (p_ < e_ && *p_ == ',') { p_++; continue; }
				if (p_ < e_ && *p_ == ']') { p_++; return true; }
				return false;
			}
		}
		if (c == '"') { v->type = JVal::Str; return string(&v->str); }
		if (lit("true"))  { v->type = JVal::Bool; v->bval = true; return true; }
		if (lit("false")) { v->type = JVal::Bool; v->bval = false; return true; }
		if (lit("null"))  { v->type = JVal::Null; return true; }
		const char* start = p_;
		while (p_ < e_ && (strchr("+-.eE", *p_) || (*p_ >= '0' && *p_ <= '9'))) p_++;
		if (p_ == start) return false;
		std::string tmp(start, p_);
		v->type = JVal::Num;
		v->num = strtod(tmp.c_str(), nullptr);
		return true;
	}
};
