# LeechBox

[English](#english) | [فارسی](#فارسی)

## English

LeechBox is a personal file-transfer dashboard for links supported by Real-Debrid, direct links, and uploads from your device. Files can be stored on the server or in Google Drive. The dashboard supports English and Persian and lets the administrator change their password from the panel.

### Stack

- Python and Flask, served with Gunicorn
- HTML, CSS, and JavaScript for the interface
- SQLite for transfer status and folder information
- Docker and Caddy for running the application and serving files
- Google Drive API and Real-Debrid API

This repository contains application source code. User files, the database, the administrator password, tokens, and OAuth credentials are not included.

### Deployment notes

This snapshot was extracted from a custom deployment. `compose.yaml` is an older example and does not, by itself, reproduce the current server setup. The current deployment also requires separate Real-Debrid token and Google OAuth client credential files, persistent storage mounted at `/data`, and a Caddy configuration.

The application currently hard-codes the `leech.cs2.cat` origin and its OAuth callback. To use another domain, update the allowed origin, OAuth callback, and Caddy configuration together. Validate the Caddy configuration and back up the existing database and files before deployment.

The `Public` Drive folder and shared server folders are accessible to anyone with their links. When Google OAuth is in testing mode, the Drive connection may need to be renewed after seven days.

New passwords set through the dashboard are stored as hashes. Existing legacy plaintext passwords remain unchanged until they are updated through the panel. Changing the password signs out older sessions.

### Sensitive files

Do not commit tokens, passwords, keys, databases, user files, or OAuth credentials. A `.gitignore` is included to reduce the risk of adding them accidentally.

---

## فارسی

لیچ‌باکس یک پنل شخصی برای دریافت لینک‌های پشتیبانی‌شده توسط Real-Debrid، لینک مستقیم و آپلود از دستگاه است. فایل‌ها می‌توانند روی سرور یا Google Drive ذخیره شوند. پنل از زبان‌های فارسی و انگلیسی پشتیبانی می‌کند و مدیر می‌تواند رمز عبور خود را داخل پنل تغییر دهد.

### اجزا

- Python و Flask همراه با Gunicorn
- HTML، CSS و JavaScript برای رابط کاربری
- SQLite برای وضعیت انتقال‌ها و پوشه‌ها
- Docker و Caddy برای اجرای پنل و ارائه فایل‌ها
- Google Drive API و Real-Debrid API

این مخزن شامل کد برنامه است؛ فایل‌های کاربران، پایگاه داده، رمز مدیریت، توکن‌ها و اطلاعات OAuth در آن قرار ندارند.

### تنظیمات استقرار

این نسخه از یک نصب اختصاصی استخراج شده است. `compose.yaml` یک نمونه قدیمی است و به‌تنهایی تنظیمات کامل نصب فعلی را بازسازی نمی‌کند. استقرار فعلی همچنین به فایل‌های جداگانه توکن Real-Debrid و شناسه و رمز محرمانه OAuth گوگل، فضای ماندگار برای `/data` و تنظیمات Caddy نیاز دارد.

برنامه در حال حاضر دامنه `leech.cs2.cat` و مسیر بازگشت OAuth همان دامنه را در کد مشخص کرده است. برای استفاده از دامنه دیگر باید مبدأ مجاز، مسیر بازگشت OAuth و پیکربندی Caddy را با هم تغییر داد. پیش از استقرار، تنظیمات Caddy را اعتبارسنجی کنید و از پایگاه داده و فایل‌های موجود نسخه پشتیبان بگیرید.

پوشه `Public` در Drive و پوشه‌های اشتراکی روی سرور برای هر کسی که لینک آن‌ها را داشته باشد قابل دسترسی هستند. در حالت آزمایشی Google OAuth، ممکن است لازم باشد اتصال Drive پس از هفت روز دوباره برقرار شود.

رمزهای جدیدی که در پنل تنظیم می‌شوند به‌صورت هش ذخیره می‌شوند؛ رمز قدیمی موجود تا زمان تغییر در پنل به همان شکل قبلی باقی می‌ماند. تغییر رمز، نشست‌های قبلی را از اعتبار خارج می‌کند.

### فایل‌های حساس

توکن‌ها، رمزها، کلیدها، پایگاه‌های داده، فایل‌های کاربران و اعتبارنامه‌های OAuth را به مخزن اضافه نکنید. فایل `.gitignore` برای کاهش خطر اضافه‌شدن ناخواسته آن‌ها قرار داده شده است.
