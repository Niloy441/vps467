# 🔐 Railway Environment Variables Setup

এই Project Railway-তে Deploy করার সময় নিচের **২টি Environment Variable** অবশ্যই Add করতে হবে।

---

## 🚀 Step 1 — FLASK_SECRET_KEY

### Railway → Variables → Add Variable

**Variable Name:**

```text
FLASK_SECRET_KEY
```

**Value:**

```text
${{secret(64, "abcdef0123456789")}}
```

---

## 🔑 Step 2 — ADMIN_SECRET_KEY

আবার **Add Variable** চাপুন।

**Variable Name:**

```text
ADMIN_SECRET_KEY
```

**Value:**

```text
${{secret(64, "abcdef0123456789")}}
```

---

## 📋 Quick Setup

| Variable Name | Value |
|---|---|
| `FLASK_SECRET_KEY` | `${{secret(64, "abcdef0123456789")}}` |
| `ADMIN_SECRET_KEY` | `${{secret(64, "abcdef0123456789")}}` |

---

## ⚙️ Railway Setup

1. Railway Project ওপেন করুন
2. আপনার Service নির্বাচন করুন
3. **Variables** এ যান
4. **Add Variable** চাপুন
5. উপরের প্রথম Variable যোগ করুন
6. আবার **Add Variable** চাপুন
7. দ্বিতীয় Variable যোগ করুন
8. **Apply Changes** চাপুন
9. তারপর **Deploy** করুন

---

## ⚠️ Security

**Secret Key কখনো GitHub source code-এর মধ্যে সরাসরি লিখবেন না।**

Environment Variables ব্যবহার করুন:

```text
FLASK_SECRET_KEY
ADMIN_SECRET_KEY
```

এতে Secret Key source code থেকে আলাদা থাকবে।

---

## ✅ Final Configuration

```env
FLASK_SECRET_KEY=${{secret(64, "abcdef0123456789")}}
ADMIN_SECRET_KEY=${{secret(64, "abcdef0123456789")}}
```

> **Note:** Railway-এর `secret()` syntax আপনার নির্দিষ্ট deployment context-এ supported কি না, Railway-এর current documentation দেখে নিশ্চিত করুন।
