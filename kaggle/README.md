# Kaggle — הזרוע הענן של הלינקר

‏session חד-פעמי של Kaggle‏ (GPU + אינטרנט) נרשם כ-GitHub JIT runner‏ (job אחד בדיוק).
הוא מריץ **רק NER** על הספרים ששונו ומעלה handoff גולמי, ממופתֵח-תוכן ומוצמד
ל-snapshot/fingerprint/request. שלב `resolve` נפרד מוריד אותו לשרת Oracle, פותר את
ה-refs על CPU תחת ה-host lease, ורק אחריו מופעל ה-publisher.

ל-job של Kaggle תקרה של 90 דקות, אך מפיק ה-NER עוצר אחרי 60 דקות כדי להשאיר זמן
לאריזה ולהעלאה. אם לא סיים, נשמר checkpoint פר-ספר וגם פר-אצווה בתוך ספר גדול;
rerun של אותו workflow databaseId ממשיך ממנו ולא מתחיל מאפס. התאוששות מדויקת
ל-databaseId חדש יכולה לשחזר checkpoint מריצה כושלת קודמת, לאחר אימות זהות המקור
וההורה והצהרה מפורשת על fingerprint ה-engine שנרשם ב-checkpoint. כל batch משוחזר
עדיין נבדק מול הטקסט המנורמל וה-engine הנוכחי לפני השימוש.

## שיגור

- מה-repo: ‏Actions → ‏kaggle-relink → ‏Run workflow (אופציונלי dry_run).
- מקומית: ‏`scripts/dispatch_kaggle_relink.sh [--dry-run]`‏ (דורש gh עם admin + kaggle CLI מחובר).

הסדר חשוב וכבר ממומש בסקריפט: קודם נרשם ה-JIT ונכנס ה-job לתור (target=kaggle),
ואז נדחף הקרנל — ה-session עולה ישר לתוך העבודה הממתינה.

## מתי קגל ומתי השרת

| תרחיש | יעד |
|---|---|
| ‏relink סריאלי בתוך בנייה שבועית | NER בקגל; resolution+publish בשרת |
| ‏relink עצמאי / עומס כבד | NER בקגל; resolution בשרת |
| שיגור ידני כשקגל לא זמין | שרת (‏target=server) |
| ‏relink מלא של כל הקורפוס | אף אחד מהם ישירות — ראו למטה |

הכלים הבינאריים (gh, mongod/tools/mongosh, ‏Actions runner) מגיעים מה-output של
הקרנל החד-פעמי `otzaria/linker-tools-fetcher` (מוצמד דרך kernel_sources) —
הורדות נעוצות-sha256 שרצות פעם אחת ב-Kaggle. דאטה-סטים פרטיים גדולים נכשלים
בעיבוד של Kaggle; ‏kernel outputs לא. עדכון גרסה = עדכון URL+sha256 ב-fetcher
‏(kaggle/fetch_tools.py), הרצה מחדש שלו, ועדכון הפינים ב-bootstrap.sh.

## עובדות שנמדדו (07/2026)

- העומס הוא **resolution-bound, לא NER-bound**: ‏GPU T4 נתן ‏7.2M מילים/שעה מול ‏6.4M
  על ה-ARM — ההאצה זניחה כי צוואר-הבקבוק הוא פייתון/CPU‏ (4 ליבות בשני המקומות).
- לכן זמן ה-GPU מוקדש לזיהוי בלבד. השרת אינו טוען את מודלי ה-NER בשלב הפתרון,
  ו-Kaggle אינו מעלה Mongo או מריץ Django/model resolution.
- ‏relink מלא = ‏~421M מילים ≈ ‏58 שעות ≈ פי-2 ממכסת ה-GPU השבועית (30h) — לא ריאלי
  ב-session בודד (תקרה ~9h). לשינויי-מנוע שהם output-neutral יש מסלול מוסדר:
  ‏`--adopt-fingerprint OLD::NEW`‏ (אטסטציית מפעיל, ראו incremental.py). שינוי מנוע
  שמחייב באמת relink מלא ידרוש פריסה על sessions‏ (--max-books, טרם ממומש) או את השרת.

## שחזור בלי לשלם שוב על GPU

- כשל/timeout בזמן NER: הפעילו `Re-run failed jobs` על אותו run. attempt חדש מאתר
  checkpoint יחיד מן ה-attempt הקודם, מאמת את זהותו וממשיך ממנו.
- אם נדרש תיקון קוד ולכן אי אפשר rerun לאותו commit, שגרו recovery חדש עם אותו
  request/parent ועם `ner_checkpoint_source_run_id`,
  `ner_checkpoint_source_run_attempt` ו-`ner_checkpoint_source_engine_fingerprint`.
  ה-provisioner מתיר זאת רק כאשר המקור הוא הילד היחיד בעל אותה זהות והוא נכשל,
  וה-workflow מאמת מחדש את ה-attempt, ההורה וה-artifact המדויק.
- כשל בשלב `resolve` אחרי שה-handoff הגולמי עלה: אין להריץ NER שוב. שגרו
  `relink.yml` ישירות עם `target=kaggle`, ‏`recovery_mode=true`,
  ‏`raw_ner_source_run_id` ו-`raw_ner_source_run_attempt` של המפיק המקורי, יחד עם
  אותן קואורדינטות parent/request/pins. ה-workflow דורש source failed מדויק, commit
  זהה, parent failed מדויק ו-artifact יחיד; תוכן ה-handoff נבדק שוב לפני פתרון.

## מלכודות סביבת Kaggle (כולן מטופלות ב-bootstrap.sh)

- אף פייתון בתמונה לא יוצר venv תקין → מותקן python3.12-venv.
- ‏PYTHONPATH גלובלי מזהם venvs נקיים → unset.
- ‏sitecustomize.py שתול בפייתון-המערכת מיירט `google.cloud` ודורש kaggle_gcp → נמחק.
- אין mongod/mongosh/mongorestore/gh → מותקנים מ-tarballs נעוצי-גרסה.
- ‏runner כ-root → ‏RUNNER_ALLOW_RUNASROOT=1.
