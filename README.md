# IQ AI Analyzer — نسخة سحابية مجانية

تحليل واتجاه ومدة + سجل أداء حقيقي. **تحليل فقط، لا يوجد تداول آلي.**

```
GitHub Actions (كل 5 دقائق)  ──►  جلب شموع IQ Option ▸ تسوية التوقعات ▸ توقع جديد
GitHub Actions (كل 6 ساعات)  ──►  تدريب النماذج ▸ رفعها إلى Release "models-latest"
فرع data (sqlite + latest.json) ──► واجهة الويب على GitHub Pages
                                └─► (اختياري) Google Sheets عبر Apps Script
```

## الإعداد (10 دقائق)
1. أنشئ مستودعًا **Public** (ضروري: الدقائق مجانية بلا حد للمستودعات العامة فقط) وارفع محتويات هذا المجلد.
   **لا ترفع ملف `.iq_credentials` أبدًا.**
2. Settings ▸ Secrets and variables ▸ Actions:
   - Secrets: `IQ_EMAIL` ، `IQ_PASSWORD`
   - (اختياري) Variable: `PAIRS` مثل `EURUSD` أو `EURUSD,EURUSD-OTC` (في عطلة الأسبوع استخدم OTC)
3. Settings ▸ Pages ▸ Source: *Deploy from a branch* ▸ `main` ▸ `/docs`.
4. تبويب Actions ▸ شغّل **train** يدويًا مرة واحدة، ثم **collect**.
5. افتح `https://USERNAME.github.io/REPO/` (أول مرة قد يطلب منك `user/repo`).

## Google Sheets (اختياري)
أنشئ Google Sheet ▸ Extensions ▸ Apps Script ▸ الصق `apps_script/Code.gs` وغيّر `TOKEN` ▸ Deploy كـ Web app (Anyone) ▸
ضع الرابط في Secret `SHEETS_WEBHOOK_URL` ونفس التوكن في `SHEETS_TOKEN`. تظهر أوراق: `predictions` و`model_metrics` و`summary`.

## ملاحظات
- أقل فاصل لـ cron في GitHub هو 5 دقائق وقد يتأخر أحيانًا؛ الكود يسد الفجوات تلقائيًا من المنصة.
- GitHub يوقف الـ workflows المجدولة بعد 60 يومًا بلا نشاط في المستودع؛ فعّلها يدويًا من تبويب Actions عند الحاجة.
- اللوحة تعرض مجال الثقة 95% لنسبة الإصابة ونقطة التعادل حسب عائد المنصة — اعتمد عليها قبل أي قرار.
